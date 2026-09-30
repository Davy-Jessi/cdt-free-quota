#!/usr/bin/env python3
"""读 OSS 上的**共享状态对象**并把关键字段打出来。

    python3 scripts/check-state.py          # 在 ECS 实例上跑（用实例 RAM 角色取凭据）

**为什么需要它。** 守护把跨实例重建要用的状态写在一个 OSS 对象里，
FC 兜底也读同一个对象。日志只告诉你守护**打算**做什么，
这个对象才是它**实际写出去**的东西 —— 排查「停机了但 FC 不拉起」
「跨月怎么没重置」这类问题时，第一手材料在这里。

它从 `/root/cdtguard.py` 里读桶名 / 端点 / 对象键，所以**不用你手填**，
也不会因为改了配置而失配。

⚠️ 凭据来自**实例元数据服务**（RAM 角色），不读任何文件、不需要 AK/SK。
   所以它只能在那台挂了角色的 ECS 上跑 —— 这正是它的使用场景。

**字段含义**（详见 cdtguard.py 的模块 docstring）：

| 字段 | 含义 |
|---|---|
| `month` | 这份状态属于哪个月（FC 的停机锁绑定它，跨月自锁就出在这） |
| `halted` | **护闸确实执行过停机**的持久标志。不是「eff 越过了阈值」—— 两者在跨月保持期里会分叉 |
| `eff` / `eff_hybrid` | 守护认定的已用量。`eff`（**字节**）是最终判据；`eff_hybrid` 是混合口径值，**单位是 GB** |
| `threshold_gb` | 当时的停机阈值。护闸按实测地域取（非内地 200 / 内地 20） |
| `ts` | 这份状态的写入时刻。**它不推进 = 护闸没在写 = 出事了** |
"""
import base64
import datetime
import hashlib
import hmac
import json
import re
import sys
import urllib.request

META = "http://100.100.100.200/latest/meta-data/"
GUARD = "/root/cdtguard.py"
GB = 10 ** 9          # 十进制 GB —— 额度按 GB 计，阈值比较也是十进制

# ⚠️ 必须用**空 ProxyHandler** —— urllib 默认会读 http_proxy/HTTP_PROXY，
#    在挂了代理的机器上会把元数据请求发到代理去，直接超时。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def meta(path):
    return _opener.open(META + path, timeout=10).read().decode().strip()


def guard_const(name):
    """从守护文件里取一个字符串常量。取不到就返回 None。"""
    try:
        src = open(GUARD, encoding="utf-8").read()
    except OSError:
        return None
    # 取值形态有两种，**两种都要认**：
    #   · 裸字面量        X = "…"
    #   · 回落式          X = os.environ.get("X", "…")
    # 只认第一种的话，对着仓库里那份（全用回落式）会一个常量都读不到。
    m = re.search(r'^%s\s*=\s*(?:os\.environ\.get\([^,]+,\s*)?"([^"]+)"'
                  % re.escape(name), src, re.M)
    return m.group(1) if m else None


def main():
    bucket = guard_const("OSS_BUCKET")
    endpoint = guard_const("OSS_EP")
    key = guard_const("OSS_KEY")
    missing = [n for n, v in (("OSS_BUCKET", bucket), ("OSS_EP", endpoint),
                              ("OSS_KEY", key)) if not v]
    if missing:
        print("X 从 %s 里读不到：%s" % (GUARD, "、".join(missing)))
        print("  守护装在哪、常量叫什么，都要和本脚本的假设一致。")
        return 1

    try:
        role = meta("ram/security-credentials/")
        c = json.loads(_opener.open(
            META + "ram/security-credentials/" + role, timeout=10).read().decode())
        if "AccessKeyId" not in c:
            # 无角色/无权限时元数据返回的是**错误对象**，不是凭据
            print("X 元数据返回的不是凭据：%s" % str(c)[:160])
            print("  通常表示这台实例没挂 RAM 角色，或角色被摘掉了。")
            return 1
    except Exception as e:
        print("X 取不到实例 RAM 角色凭据：%s" % e)
        print("  本脚本只能在那台挂了角色的 ECS 上跑。")
        return 1

    ak, sk, tok = c["AccessKeyId"], c["AccessKeySecret"], c["SecurityToken"]
    date = datetime.datetime.now(
        datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    canon = "GET\n\n\n%s\nx-oss-security-token:%s\n/%s/%s" % (date, tok, bucket, key)
    sig = base64.b64encode(
        hmac.new(sk.encode(), canon.encode(), hashlib.sha1).digest()).decode()

    url = "https://%s.%s/%s" % (bucket, endpoint, key)
    req = urllib.request.Request(url, headers={
        "Date": date,
        "Authorization": "OSS %s:%s" % (ak, sig),
        "x-oss-security-token": tok})
    try:
        raw = _opener.open(req, timeout=15).read().decode()
    except urllib.error.URLError as e:
        print("X 连不上 OSS（%s）：%s" % (endpoint, e))
        return 1
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("X OSS 上还没有这个对象（%s）—— 守护可能从没成功写过。" % key)
            print("  这不是「一切正常」，是**状态存储没工作**：")
            print("  FC 的拉起熔断与「护闸失联」告警都依赖它。")
        else:
            print("X 读 OSS 失败：HTTP %s" % e.code)
        return 1
    try:
        d = json.loads(raw)
    except ValueError:
        print("X 对象不是 JSON（可能被中间层改写过，或对象是截断的）：%s" % raw[:160])
        return 1
    if not isinstance(d, dict):
        print("X 对象结构不对（顶层应当是对象，实际是 %s）" % type(d).__name__)
        return 1

    print("对象：oss://%s/%s" % (bucket, key))
    print("字段：%s" % " ".join(sorted(d.keys())))
    print()
    print("  month        = %s" % d.get("month"))
    print("  halted       = %s%s" % (
        d.get("halted"),
        "   ← 护闸确实停过机（FC 会拒绝拉起）" if d.get("halted") else ""))
    print("  threshold_gb = %s GB" % d.get("threshold_gb"))
    eff = d.get("eff")
    print("  eff          = %s" % ("%.3f GB" % (eff / GB) if eff else eff))
    hy = d.get("eff_hybrid")
    print("  eff_hybrid   = %s" % ("%s GB（本字段已是 GB）" % hy if hy is not None else hy))
    print("  ts           = %s" % d.get("ts"))
    print()
    # ⚠️ 这个对象**混用单位** —— 别一律按字节换算：
    #      eff / acc / bw_bytes / cdt_floor / rx / tx / last_rx / last_tx → **字节**
    #      eff_hybrid / bw_base_gb / threshold_gb                          → **已经是 GB**
    #    字段名带 `_gb` 的是 GB，但 `eff_hybrid` **没有** _gb 后缀却也是 GB ——
    #    历史遗留。一律当字节处理会把 49.8 GB 显示成 0.000 GB（本脚本第一版就这么错过）。
    print("⚠️ 单位提醒：本对象混用单位 —— `eff`/`acc`/`bw_bytes` 是**字节**，")
    print("   `eff_hybrid`/`bw_base_gb`/`threshold_gb` **已经是 GB**。")
    print()
    print("⚠️ 判据：**看 ts 有没有在推进**（护闸每约 2 秒写本地、每约 60 秒写 OSS）。")
    print("   ts 停在过去 = 护闸没在写 = 它可能已经死了，而 rc-service 仍显示 started。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
