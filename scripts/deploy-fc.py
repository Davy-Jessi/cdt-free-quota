# 部署 cdt-keeper 到函数计算 FC 3.0。
#   用法: python3 deploy-fc.py <index.js 路径> [--dry] [--allow-unverified]
#
# 为什么需要这个脚本（而不是控制台点一下或直接调 UpdateFunction）：
#
#   1) environment_variables 是**整体覆盖**语义 —— 漏一个键 = 删一个键。
#      所以必须 Get -> 原样合并 -> Update，**绝不硬编码清单**。
#      漏掉 ALIYUN_SK 就是 FC 全线静默失效（护闸还在跑，但拉起链路断了）。
#
#   2) UpdateFunction 之后**热实例可能仍跑旧代码**。必须调用一次并断言返回的
#      `ver` 等于源码里的 `const VER`，否则会在「以为部署成功、实际跑旧代码」
#      的状态下收工。（这个坑见本仓库 docs/pitfalls.md 第 26 条。）
#
#   3) 调用校验前先确认实例是 Running —— 否则可能触发 StartInstance 副作用，
#      把一台本该停机的实例拉起来继续烧流量。
#
# 凭据：从环境变量读（ALIYUN_AK / ALIYUN_SK / ALIYUN_UID / ECS_INSTANCE_ID）。
#       这是**部署期**身份，权限可以比运行时角色宽 —— 用完即摘。
import base64, hashlib, io, json, os, re, sys, time, zipfile
import urllib.request
from alibabacloud_fc20230330.client import Client
from alibabacloud_tea_openapi import models as oa
from alibabacloud_fc20230330 import models as fc


def require_env(name):
    v = os.environ.get(name)
    if not v:
        sys.exit("缺少环境变量 %s（部署期凭据，不是运行时角色）" % name)
    return v


AK = require_env("ALIYUN_AK")
SK = require_env("ALIYUN_SK")
UID = require_env("ALIYUN_UID")
IID = require_env("ECS_INSTANCE_ID")
REGION = os.environ.get("REGION_ID", "cn-hongkong")
FN = os.environ.get("FC_FUNCTION_NAME", "cdt-keeper")

# env 复核只检查这几个「缺了会静默断链」的键。不写死全部键名 ——
# 其余键原样合并即可，清单写死反而会在别人改了函数后误报。
REQUIRED_ENV = ["ALIYUN_AK", "ALIYUN_SK", "ECS_INSTANCE_ID", "OSS_BUCKET", "PUSH_TOKEN", "REGION_ID"]

if len(sys.argv) < 2:
    sys.exit("用法: python3 deploy-fc.py <index.js 路径> [--dry] [--allow-unverified]")
js_path = sys.argv[1]
dry = "--dry" in sys.argv
src = open(js_path, "rb").read()
m = re.search("const VER = '([^']+)'", src.decode())
if not m:
    sys.exit("源文件里找不到 `const VER = '...'` —— 无法做部署后校验，中止")
EXPECT_VER = m.group(1)
print("源文件: %s  %d 字节  sha256=%s" % (js_path, len(src), hashlib.sha256(src).hexdigest()))
print("期望 VER: %s" % EXPECT_VER)

c = Client(oa.Config(access_key_id=AK, access_key_secret=SK,
                     endpoint="%s.%s.fc.aliyuncs.com" % (UID, REGION)))

# ── 1) 读回现有配置 ──────────────────────────────────────────────────
cur = c.get_function(FN, fc.GetFunctionRequest()).body
env = dict(cur.environment_variables or {})
print("读回现有 env: %d 个键" % len(env))
missing = [k for k in REQUIRED_ENV if not env.get(k)]
if missing:
    print("✖ 缺少必需环境变量: %s —— 中止（写回会打断链路）" % missing)
    sys.exit(1)
print("  必需键齐全 ✔")

# ── 2) 打 zip ────────────────────────────────────────────────────────
# 固定时间戳（1980-01-01）让 zip 可复现：同样的源码产出同样的字节。
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    zi = zipfile.ZipInfo("index.js", date_time=(1980, 1, 1, 0, 0, 0))
    zi.compress_type = zipfile.ZIP_DEFLATED
    z.writestr(zi, src)
zbytes = buf.getvalue()
print("zip: %d 字节 (原 %s)" % (len(zbytes), cur.code_size))

if dry:
    print("[--dry] 到此为止，未做任何写操作")
    sys.exit(0)

# ── 3) UpdateFunction（env 原样合并）────────────────────────────────
inp = fc.UpdateFunctionInput(
    code=fc.InputCodeLocation(zip_file=base64.b64encode(zbytes).decode()),
    environment_variables=env,         # <- 原样写回，不增不减
    handler=cur.handler, runtime=cur.runtime,
    timeout=cur.timeout, memory_size=cur.memory_size,
)
r = c.update_function(FN, fc.UpdateFunctionRequest(body=inp))
print("UpdateFunction 已下发, RequestId =",
      r.body.request_id if hasattr(r.body, "request_id") else "?")

# ── 4) 复核 env 没被改动 ────────────────────────────────────────────
time.sleep(3)
after = dict(c.get_function(FN, fc.GetFunctionRequest()).body.environment_variables or {})
if sorted(after.keys()) != sorted(env.keys()):
    print("✖ 环境变量键集变了！前: %s 后: %s" % (sorted(env.keys()), sorted(after.keys())))
    sys.exit(1)
diff = [k for k in env if env[k] != after.get(k)]
if diff:
    print("✖ 环境变量值变了: %s" % diff)
    sys.exit(1)
print("env 复核通过: %d 个键，与写回前逐一致 ✔" % len(after))

# ── 5) 先确认实例是 Running ────────────────────────────────────────
import hmac as _h


def pct(x):
    import urllib.parse
    return urllib.parse.quote(str(x), safe="").replace("+", "%20").replace("*", "%2A")


def rpc(action, version, params):
    import uuid, datetime as dt
    p = {"AccessKeyId": AK, "Action": action, "Format": "JSON",
         "SignatureMethod": "HMAC-SHA1", "SignatureNonce": str(uuid.uuid4()),
         "SignatureVersion": "1.0", "Version": version,
         "Timestamp": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    p.update(params)
    cqs = "&".join("%s=%s" % (pct(k), pct(p[k])) for k in sorted(p))
    sig = base64.b64encode(_h.new((SK + "&").encode(),
          ("GET&%2F&" + pct(cqs)).encode(), hashlib.sha1).digest()).decode()
    with urllib.request.urlopen("https://ecs.aliyuncs.com/?Signature=%s&%s" % (pct(sig), cqs),
                                timeout=20) as resp:
        return json.loads(resp.read().decode())


# ⚠️ 必须用 .get() 链：实例查不到时 `DescribeInstances` 返回的是**空数组**
#    （不是报错），直接下标会抛 KeyError/IndexError —— 而下面那段本来就是
#    为「查不到 / 不是 Running」准备的告警分支，那样就永远走不到。
_ins = (rpc("DescribeInstances", "2014-05-26",
            {"RegionId": REGION, "InstanceIds": json.dumps([IID])})
        .get("Instances", {}).get("Instance", []))
if not _ins:
    print("⚠ 查不到实例 %s —— 无法做调用校验（ID 过期？地域填错？）" % IID)
    print("  跳过校验，避免触发 StartInstance 副作用。")
    sys.exit(0)
st = _ins[0].get("Status")
print("实例当前状态: %s" % st)
# ⚠️⚠️ 实例不是 Running 时**不能悄悄 exit 0** —— 那等于「部署校验通过」但实际
#     一次校验都没做。这恰恰是 v3 那个 bug 能溜过去的同类问题：
#     报告成功，而成功不成立。
#     两种选择都给：显式加 --allow-unverified 表示「我知道这次没校验」。
ALLOW_UNVERIFIED = "--allow-unverified" in sys.argv
if st != "Running":
    print("")
    print("✖ 实例不是 Running（当前 %s）—— **无法做调用校验**。" % st)
    print("  为什么跳过：直接调用会触发 StartInstance 副作用，")
    print("              可能把一台本该停机的实例拉起来继续烧流量。")
    print("")
    if ALLOW_UNVERIFIED:
        print("  已指定 --allow-unverified ⇒ 接受「本次未校验」并按成功退出。")
        print("  ⚠️ 这意味着：**代码是否真的部署上去了，本次没有被验证。**")
        sys.exit(0)
    print("  但**不校验 ≠ 校验通过**。所以这里按失败退出。")
    print("  若你确实接受不校验：加 --allow-unverified 重跑。")
    sys.exit(1)

# ── 6) 调用并断言 ver（热实例可能仍跑旧码，故重试）──────────────────
# ⚠️ 探针的 payload 决定了它**走到哪条分支** —— 这很关键：
#    旧探针是 `{"probe": "deploy-verify"}`，没有 `triggerTime`，
#    于是 evIsTimer=false、source='event'，**永远不会进入 isLiveness 分支**。
#    实测：那个探针在 v3（有 ReferenceError 的那版）上照样返回 ver=v3 并判「通过」。
#    也就是说：**它只证明「哪一版被加载」，不证明「那一版能跑」。**
#    现在同时发一个带 triggerTime + payload={"liveness":1} 的探针，
#    让存活巡检那条最容易坏的分支也真的被执行一次。
VER_PROBE = {"probe": "deploy-verify"}
LIVENESS_PROBE = {"triggerTime": "2026-01-01T00:00:00Z",
                  "triggerName": "guard-liveness-8min",
                  "payload": json.dumps({"liveness": 1})}
ok = False
for attempt in range(1, 6):
    resp = c.invoke_function(FN, fc.InvokeFunctionRequest(
        body=json.dumps(VER_PROBE)))
    b = resp.body
    if isinstance(b, str):
        raw = b
    elif hasattr(b, "read"):
        raw = b.read().decode()
    else:
        raw = b.decode()
    try:
        got = json.loads(raw)
    except Exception:
        got = {}
    ver = got.get("ver")
    print("  第 %d 次调用: ver=%s action=%s" % (attempt, ver, got.get("action")))
    if ver == EXPECT_VER:
        ok = True
        break
    time.sleep(4)

# ── 6.5) 再打一次「存活巡检形态」的探针：验那条分支真的能跑 ──────────
# 判据：**只要没有 error 字段**就算过（各场景的 action 取决于当时的 OSS 状态，
#       部署期不该假定它是什么）；有 error 就说明那条分支抛了。
if ok:
    resp2 = c.invoke_function(FN, fc.InvokeFunctionRequest(
        body=json.dumps(LIVENESS_PROBE)))
    b2 = resp2.body
    if isinstance(b2, str):
        raw2 = b2
    elif hasattr(b2, "read"):
        raw2 = b2.read().decode()
    else:
        raw2 = b2.decode()
    try:
        got2 = json.loads(raw2)
    except Exception:
        got2 = {}
    print("  存活巡检探针: action=%s evIsLiveness=%s"
          % (got2.get("action"), got2.get("evIsLiveness")))
    if got2.get("error"):
        print("  ✖ 存活巡检分支抛错：%s" % got2.get("error"))
        print("     ⚠️ 这条分支是「护闸已死」唯一的外部观测手段，")
        print("        它坏掉时是**静默**的（被外层 catch 兜住），必须当成失败。")
        ok = False
    elif not got2.get("evIsLiveness"):
        print("  ✖ 探针没有进入存活巡检分支（evIsLiveness 非真）——")
        print("     说明事件形状与线上定时器不一致，这次校验没覆盖到那条分支。")
        ok = False

# ── 7) 回读**部署事实**：触发器 / env 键 / runtime，与 inventory.json 比对 ──
# 🔴 **这一步补的是一个真实事故。** `provision/README.md` §5.3 写着要 3 个触发器、
#    还专门警告 `guard-liveness-8min`「别漏掉」，而线上**只有 2 个** ——
#    那个触发器从来没被创建过，而**没有任何一步会去比对**。
#
#    为什么放在这里而不是只放 CI：**CI 没有云凭据**，读不到线上。
#    部署脚本是唯一「既跑在凭据下、又必然被执行」的位置 ——
#    所以「回读触发器清单」必须挂在这里才不会又被漏掉。
#
#    ⚠️ 判据是「**清单 ↔ 线上**」，不是「数量对不对」。数量相等不等于集合相等：
#       删一个、加一个，数量照样对。
#    ⚠️ 失败即让部署判失败 —— 这是刻意的：一个「悄悄跳过」的验证等于没有验证。
print()
print("── 7) 回读部署事实（触发器 / env / runtime）──")
_vd = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify-deploy.py")
if not os.path.exists(_vd):
    print("✖ 找不到 scripts/verify-deploy.py —— 无法回读触发器清单")
    print("  ⚠️ 这一步正是当初漏掉 guard-liveness-8min 的地方，不能跳过。")
    sys.exit(1)
else:
    import subprocess
    _r = subprocess.run([sys.executable, _vd],
                        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _r.returncode != 0:
        print("✖ 部署事实回读失败（退出码 %d）—— 见上面列出的具体差异。" % _r.returncode)
        print("  ⚠️ 代码已经更新上去了，但**线上状态与清单不符**。")
        print("     别为了让这一步变绿而删掉 inventory.json 里的声明。")
        ok = False

print()
print("部署校验: %s" % ("✔ 通过（新代码已在跑，且存活巡检分支执行无错，部署事实已回读）"
                        if ok else "✖ 失败（见上面的具体原因）"))
sys.exit(0 if ok else 1)
