#!/bin/sh
# 创建共享带宽包（示例脚本 —— 演示本目录 README 里那套模式）
#
#   用法: ./create-cbwp.sh [带宽峰值 Mbps]
#   例:   ./create-cbwp.sh 200
#
# 为什么挑这个资源做示例：它是整条链路上**最容易烧钱**的一个。
#
#   · 按带宽计费时**不足 1 小时按 1 小时** —— 建了 23 秒删掉，仍收满 1 小时（¥14 量级）
#     **删包不能止损**
#   · 按增强型 95 计费**至少持有 1 个自然月，期间无法删除**
#   · 计费取**当日最大带宽峰值** —— 当天把峰值调高，全天按高的收
#   · **没有 `DryRun`** —— 「先试一下」这条路不存在
#
# 所以这个脚本把三件事做进流程里：
#   1) **计费方式写死在脚本里**，不接受参数覆盖（防止有人「顺手改成按带宽」）
#   2) 创建前先按 Tag 判重（幂等，且不靠名字 —— 名字没有唯一性约束）
#   3) 创建后**回读**确认计费方式真的是按主流量（不信「我传对了」）
#
# ⚠️ 本脚本**未经端到端实测** —— 端到端验证它意味着真建一个共享带宽包，
#    而建了就可能被收满 1 小时（见上面第一条），代价太高。
#    它是对一套手工步骤的机械化重写。首次运行请先读完整输出再确认。
#
# 依赖: aliyun CLI
# 环境: REGION_ID（默认 cn-hongkong）
set -e

BANDWIDTH="${1:-200}"
REGION="${REGION_ID:-cn-hongkong}"

# ── 计费方式与线路：写死，不给参数 ────────────────────────────────────
# 这两条是整个方案能不能用上免费额度的前提。
#   PayByDominantTraffic（按主流量）是唯一能吃到 CDT 免费额度的计费方式；
#   固定带宽模式是预付费，「不涉及 CDT 计费，也无法使用免费额度」。
#   ISP 必须是 BGP；选 BGP_PRO（精品线路）一分额度都用不上。
CHARGE=PayByDominantTraffic
ISP=BGP
TAG_KEY=cdt-purpose
TAG_VAL=cbwp

command -v aliyun >/dev/null 2>&1 || { echo "X 缺少 aliyun CLI"; exit 1; }

say() { printf '\n== %s ==\n' "$1"; }

say "STEP 0 —— 意图确认"
cat <<NOTE
  将要创建：
    地域      $REGION
    带宽峰值  ${BANDWIDTH} Mbps
    计费方式  $CHARGE   （脚本写死，不接受覆盖）
    线路      $ISP       （同上）
    标记      $TAG_KEY=$TAG_VAL

  ⚠️ 这是一个**会开始计费**的操作，而且：
     · 共享带宽包**没有 DryRun**，没有「先试一下」
     · 按主流量计费**不足 1 小时按 1 小时**
     · 计费按**当日最大带宽峰值** —— 之后调高峰值，当天全天按高的收
     · 若不小心建成按带宽或按增强 95 计费，成本会高得多（后者至少锁 1 个自然月）
NOTE

# ⚠️ 参数名是**单数** `--BandwidthPackageId` —— 官方 API 定义里只有这一个 ID 参数，
#    没有 `CommonBandwidthPackageIds`。写复数时 aliyun CLI **直接硬报错**
#    （`"--CommonBandwidthPackageIds" is not a valid parameter or flag`）——
#    实测确认，不会静默忽略。所以这条的错误表现是「跑不起来」而非「跑错了」。
say "STEP 1 —— 按 Tag 判重"
# 用 Tag 而不是 Name：Name 只是自由文本，没有唯一性约束，
# 按名字判重会命中「上一次那个」，然后把参数不对的资源接管下来。
EXIST=$(aliyun vpc DescribeCommonBandwidthPackages --RegionId "$REGION" \
        --Tag.1.Key "$TAG_KEY" --Tag.1.Value "$TAG_VAL" \
        --output cols=InstanceId rows=CommonBandwidthPackages.CommonBandwidthPackage 2>/dev/null | tail -1 | tr -d ' \r' || true)

if [ -n "$EXIST" ]; then
  echo "  已存在: $EXIST"
  say "STEP 2 —— 核对现有资源的计费方式"
  aliyun vpc DescribeCommonBandwidthPackages --RegionId "$REGION" \
    --BandwidthPackageId "$EXIST" \
    --output cols=InternetChargeType rows=CommonBandwidthPackages.CommonBandwidthPackage | sed 's/^/    InternetChargeType: /'
  echo
  echo "  预期是 $CHARGE。若不是，说明它不能用免费额度 —— 要手工处置（注意："
  echo "  包年包月合同期内不支持降配和释放，按增强 95 至少锁一月）。"
  echo "  不作任何改动，退出。"
  exit 0
fi
echo "  不存在，继续创建"

say "STEP 3 —— 创建"
OUT=$(aliyun vpc CreateCommonBandwidthPackage --RegionId "$REGION" \
      --Bandwidth "$BANDWIDTH" \
      --InternetChargeType "$CHARGE" \
      --ISP "$ISP" \
      --Name "cdt-cbwp" \
      --Tag.1.Key "$TAG_KEY" --Tag.1.Value "$TAG_VAL")
echo "$OUT"
NEWID=$(printf '%s' "$OUT" | tr -d ' \n' | sed -n 's/.*"BandwidthPackageId":"\([^"]*\)".*/\1/p')
[ -n "$NEWID" ] || { echo "X 没拿到 BandwidthPackageId，创建可能失败"; exit 1; }
echo "  已创建: $NEWID"

say "STEP 4 —— 回读校验（不信「我传对了」）"
sleep 3
GOT=$(aliyun vpc DescribeCommonBandwidthPackages --RegionId "$REGION" \
      --BandwidthPackageId "$NEWID" \
      --output cols=InternetChargeType rows=CommonBandwidthPackages.CommonBandwidthPackage | tail -1 | tr -d ' \r')
echo "    InternetChargeType = $GOT"
if [ "$GOT" != "$CHARGE" ]; then
  echo "  X 实际计费方式是 $GOT，不是 $CHARGE —— **它吃不到免费额度**。"
  echo "    请立刻处置（注意最小计费单位：不足 1 小时按 1 小时）。"
  exit 1
fi
echo "  OK 计费方式正确"

say "完成"
echo "  $NEWID  ($REGION, ${BANDWIDTH}Mbps, $CHARGE/$ISP)"
echo
echo "  下一步: 把 EIP 加进来"
echo "    aliyun vpc AddCommonBandwidthPackageIp --RegionId $REGION \\"
echo "      --BandwidthPackageId $NEWID --IpInstanceId <eip-xxx>"
echo
echo "  ⚠️ 之后不要再改这个包的带宽峰值 —— 计费取当日最大值，"
echo "     当天调高会全天按高的收。"
