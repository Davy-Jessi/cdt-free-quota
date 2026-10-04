#!/usr/bin/env python3
"""smoke-verify-deploy.py —— 证明 `scripts/verify-deploy.py` 的比对**真的会报警**。

**为什么需要它。**

`verify-deploy.py` 是「回读线上、与清单比对」的那道闸。它存在的理由是一次
真实事故：文档写着 3 个触发器、线上只有 2 个，而**没有任何东西会告诉你**。

那么就必须回答一个问题：**这个验证器本身会不会也悄悄失灵？**
一个「不会报警的验证器」和没有验证器是一样的 —— 而且更糟，因为它让人以为
已经有了。本仓库恰好在这一点上栽过（CI 全绿，而代码是坏的）。

所以这里**不测「它不抛异常」**，而是测「**喂给它漂移过的线上状态，它必须报出
对应的那一条**」——每一个判据都配一个反向用例。

**它不需要云凭据、也不需要装 FC SDK。** 判据逻辑被抽成 `verify_deploy.compare()`
纯函数，这里用固定 fixture 直接调它。
（实测：CI 不装第三方包是刻意的选择 —— 供应链面越小越好。）
"""
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_module(name, rel):
    path = ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vd = load_module("verify_deploy", "scripts/verify-deploy.py")

PASS, FAIL = [], []


def check(desc, cond, detail=""):
    (PASS if cond else FAIL).append(desc)
    print("  %s %s%s" % ("✔" if cond else "✖", desc,
                         ("\n      %s" % detail) if (detail and not cond) else ""))


def fmt(fails):
    return " | ".join(f["msg"] for f in fails)


# 与线上实测一致的一份「正常」视图。
# ⚠️ 这份 fixture 要跟着 inventory.json 走 —— 它对不上时下面的「全绿」用例会红，
#    那正是我们想要的信号（说明清单改了而 fixture 没跟上）。
def good_view():
    return {
        "runtime": "nodejs18",
        "handler": "index.handler",
        "env_keys": {"ALIYUN_AK", "ALIYUN_SK", "ECS_INSTANCE_ID", "ECS_NAME", "EIP",
                     "OSS_BUCKET", "OSS_ENDPOINT", "PUSH_TOKEN", "REGION_ID",
                     "TRAFFIC_THRESHOLD_GB", "ZONE", "TZ"},
        "triggers": {
            "daily-0005-cst": {
                "type": "timer",
                "config_raw": json.dumps(
                    {"payload": "{}", "cronExpression": "0 5 16 * * *", "enable": True}),
            },
            "ecs-statechange": {
                "type": "eventbridge",
                "config_raw": json.dumps({"triggerEnable": True}),
            },
            "guard-liveness-8min": {
                "type": "timer",
                "config_raw": json.dumps(
                    {"payload": json.dumps({"liveness": 1}),
                     "cronExpression": "0 */8 * * * *", "enable": True}),
            },
        },
        "ver": None,          # 由下面按源码里的 VER 填
        "ver_error": None,
    }


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    inv = json.loads((ROOT / "inventory.json").read_text(encoding="utf-8"))
    ver = None
    import re as _re
    m = _re.search(r"const VER = '([^']+)'",
                   (ROOT / "scripts/fc-keeper/index.js").read_text(encoding="utf-8"))
    if m:
        ver = m.group(1)

    print("== 1/2 正常状态必须判通过 ==")
    v = good_view()
    v["ver"] = ver
    f = vd.compare(inv, v)
    check("完全一致的线上状态 → 无失败项", not f, fmt(f))

    # ── 逐条反向用例：每个判据都要能被触发 ────────────────────────────
    print()
    print("== 2/2 每条判据的反向用例（必须报出对应那一条）==")

    cases = []

    # ① 触发器缺失 —— **这就是当初的真实事故**
    v = good_view(); v["ver"] = ver
    del v["triggers"]["guard-liveness-8min"]
    cases.append(("触发器缺失（当初的真实事故）", v,
                  "触发器**不存在**: guard-liveness-8min"))

    # ⑥ 多出一个未声明的触发器
    v = good_view(); v["ver"] = ver
    v["triggers"]["ghost-trigger"] = {
        "type": "timer",
        "config_raw": json.dumps({"cronExpression": "0 0 0 * * *", "enable": True})}
    cases.append(("线上多出一个未声明的触发器", v,
                  "触发器**未在清单里声明**: ghost-trigger"))

    # ② 类型不符
    v = good_view(); v["ver"] = ver
    v["triggers"]["guard-liveness-8min"]["type"] = "eventbridge"
    cases.append(("触发器类型不符", v, "guard-liveness-8min 类型不符"))

    # ③ cron 写错（功能看起来还在）
    v = good_view(); v["ver"] = ver
    v["triggers"]["guard-liveness-8min"]["config_raw"] = json.dumps(
        {"payload": json.dumps({"liveness": 1}),
         "cronExpression": "0 0 * * * *", "enable": True})
    cases.append(("cron 写错（周期不对但功能还在）", v,
                  "guard-liveness-8min 的 cron 不符"))

    # ④a 存活巡检被停用（**与不存在同一种失效**）
    v = good_view(); v["ver"] = ver
    v["triggers"]["guard-liveness-8min"]["config_raw"] = json.dumps(
        {"payload": json.dumps({"liveness": 1}),
         "cronExpression": "0 */8 * * * *", "enable": False})
    cases.append(("存活巡检存在但被停用", v,
                  "guard-liveness-8min 的启用状态不符"))

    # ④b eventbridge 用的是另一个键名 —— 读不到必须算失败，不能静默放过
    v = good_view(); v["ver"] = ver
    v["triggers"]["ecs-statechange"]["config_raw"] = json.dumps({"somethingElse": True})
    cases.append(("eventbridge 触发器读不到启用状态（键名不对）", v,
                  "ecs-statechange 的 triggerConfig 里没有 'triggerEnable' 字段"))

    # ④c eventbridge 被停用
    v = good_view(); v["ver"] = ver
    v["triggers"]["ecs-statechange"]["config_raw"] = json.dumps({"triggerEnable": False})
    cases.append(("eventbridge 触发器被停用", v,
                  "ecs-statechange 的启用状态不符"))

    # ⑤ 存活巡检漏了 payload（⇒ 每 8 分钟刷一条）
    v = good_view(); v["ver"] = ver
    v["triggers"]["guard-liveness-8min"]["config_raw"] = json.dumps(
        {"payload": "{}", "cronExpression": "0 */8 * * * *", "enable": True})
    cases.append(("存活巡检的 payload 漏了 liveness", v,
                  "guard-liveness-8min 的 payload 里没有 liveness"))

    # ⑤b payload 是坏 JSON
    v = good_view(); v["ver"] = ver
    v["triggers"]["guard-liveness-8min"]["config_raw"] = json.dumps(
        {"payload": "{not json", "cronExpression": "0 */8 * * * *", "enable": True})
    cases.append(("存活巡检的 payload 不是合法 JSON", v,
                  "guard-liveness-8min 的 payload 里没有 liveness"))

    # ⑦ runtime 漂移
    v = good_view(); v["ver"] = ver
    v["runtime"] = "nodejs20"
    cases.append(("runtime 变了", v, "runtime 不符"))

    # ⑦ handler 漂移
    v = good_view(); v["ver"] = ver
    v["handler"] = "main.handler"
    cases.append(("handler 变了", v, "handler 不符"))

    # ⑧ env 缺键（整体覆盖语义下少一个 = 删一个）
    v = good_view(); v["ver"] = ver
    v["env_keys"].discard("ALIYUN_SK")
    cases.append(("env 少了一个键（如 ALIYUN_SK）", v, "线上缺这些 env 键"))

    # ⑨ ver 不符 —— 热实例仍跑旧代码
    v = good_view(); v["ver"] = "v0-stale-code"
    cases.append(("ver 不符（线上跑的不是这份代码）", v, "ver 不符"))

    # ⑨b ver **读不到** —— 「不校验 ≠ 校验通过」
    v = good_view(); v["ver"] = None
    v["ver_error"] = "connection reset"
    cases.append(("ver 读不到（不校验 ≠ 校验通过）", v, "无法回读 ver"))

    ok = True
    for desc, view, want_sub in cases:
        f = vd.compare(inv, view)
        got = fmt(f)
        hit = any(want_sub in x["msg"] for x in f)
        check("%s → 报出「%s」" % (desc, want_sub), hit,
              "实际: %s" % (got or "(无失败项 —— **假通过**)"))
        ok = ok and hit

    # 保险：至少要有这么多条反向用例，否则是「用例被删了」而不是「都过了」
    print()
    check("反向用例数量 ≥ 13（防止用例被悄悄删空）", len(cases) >= 13,
          "实际 %d 条" % len(cases))
    ok = ok and len(cases) >= 13

    print()
    print("通过 %d，失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("!! verify-deploy.py 的比对存在**假通过**分支 ——")
        print("   那意味着「以为有验证、其实没有」，比没有验证更糟。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
