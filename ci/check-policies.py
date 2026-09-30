#!/usr/bin/env python3
"""断言 policies/ 里没有把「会烧钱的动作」放行。

为什么需要：`policies/cdtguard-runtime-policy.json` 是这套方案的安全边界。
读者很容易为了让某个功能跑起来而往里加 Allow —— 加的时候不会有人拦。
这个脚本把它变成一条可执行的检查。

判据有两条，缺一不可：
  1. 高危 action **不得出现在 Allow 里**
  2. 高危 action **必须被显式 Deny**

只查第 1 条不够：被删掉一条 Deny 也是安全退化，而它不会触发第 1 条。
"""
import json
import pathlib
import sys

# 这些 action 一旦放行，就等于把「误建即烧钱」的口子打开了。
# 每一条都对应 pitfalls.md 里的一个计费陷阱。
HIGH_RISK = {
    # 共享带宽包：不足 1 小时按 1 小时；按增强 95 至少持有 1 个自然月
    "vpc:CreateCommonBandwidthPackage",
    "vpc:ModifyCommonBandwidthPackageSpec",
    "vpc:ModifyCommonBandwidthPackageAttribute",
    "vpc:AddCommonBandwidthPackageIp",
    "vpc:AddCommonBandwidthPackageIps",
    "vpc:ModifyCommonBandwidthPackageIpBandwidth",
    # EIP：闲置也收保有费；改峰值按当日最大值计
    "vpc:AllocateEipAddress",
    "vpc:AllocateEipAddressPro",
    "vpc:ModifyEipAddressAttribute",
    # ECS 侧的等价动作
    "ecs:ModifyInstanceNetworkSpec",
    "ecs:AllocatePublicIpAddress",
    # BSS：包年包月下单/支付/续费（合同期内不支持降配和释放）
    "bss:ModifyInstance",
    "bss:CreateOrder",
    "bss:PayOrder",
    "bss:RenewInstance",
    "bss:ModifyInstanceAutoRenewAttribute",
}


def actions_of(stmt):
    a = stmt.get("Action", [])
    return [a] if isinstance(a, str) else a


def main():
    policy = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                          else "policies/cdtguard-runtime-policy.json")
    if not policy.exists():
        print("X 找不到 %s" % policy)
        return 1
    doc = json.loads(policy.read_text(encoding="utf-8"))

    allowed, denied = set(), set()
    for st in doc.get("Statement", []):
        eff = st.get("Effect")
        if eff == "Allow":
            allowed |= set(actions_of(st))
        elif eff == "Deny":
            denied |= set(actions_of(st))

    bad = sorted(HIGH_RISK & allowed)
    missing = sorted(HIGH_RISK - denied)

    print("  策略: %s" % policy)
    print("  Allow %d 个 action / Deny %d 个 action" % (len(allowed), len(denied)))
    if bad:
        print("  X 这些高危 action 出现在 Allow 里: %s" % bad)
    if missing:
        print("  X 这些高危 action 没有被显式 Deny: %s" % missing)
    if bad or missing:
        return 1
    print("  OK %d 个高危 action 既未放行、也都被显式 Deny" % len(HIGH_RISK))
    return 0


if __name__ == "__main__":
    sys.exit(main())
