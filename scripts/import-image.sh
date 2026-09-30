#!/bin/sh
# 把构建好的 raw 镜像上传 OSS 并导入为 ECS 自定义镜像。
#
#   用法: ./import-image.sh <disk.raw> [镜像名]
#   例:   ./import-image.sh disk.raw AlpineCDT
#
# 依赖（两个官方工具，都在 PATH 里）：
#   aliyun   —— Alibaba Cloud CLI
#   ossutil  —— OSS 命令行工具（上传大文件比 CLI 的自带 OSS 命令稳）
#
# 需要环境变量（见 ../.env.example）：
#   ALIYUN_AK / ALIYUN_SK / OSS_BUCKET
#   REGION_ID                默认 cn-hongkong
#
# ⚠️ 本脚本**未经端到端实测**。它是对一份已验证过的**手工步骤**的机械化重写，
#    其中两个坑是从真实报错里换来的：
#      · Platform 必须写 `Others Linux`，**不是 `Others`**
#      · RoleName 必须传，否则报 `NoSetRoletoECSServiceAcount`
#        ⚠️ 官方在**同一个接口**里两种拼法不一致：errorCodes 表写 `...Acount`（无末位 c，
#           这是 API 实际返回的 Code），而 OSSBucket 参数说明写 `...Account`。
#           排查时**两种都 grep**，别只认一个。
set -e

RAW="${1:?用法: import-image.sh <disk.raw> [镜像名]}"
IMG="${2:-AlpineCDT}"
REGION="${REGION_ID:-cn-hongkong}"
OBJ="$(basename "$RAW")"
[ -f "$RAW" ] || { echo "X 找不到 $RAW"; exit 1; }

for v in ALIYUN_AK ALIYUN_SK OSS_BUCKET; do
  eval "val=\$$v"
  [ -n "$val" ] || { echo "X 缺少环境变量 $v（见 ../.env.example）"; exit 1; }
done
for c in aliyun ossutil; do
  command -v "$c" >/dev/null 2>&1 || { echo "X 缺少 $c"; exit 1; }
done

say() { printf '\n== %s ==\n' "$1"; }

say "STEP 0 —— 输入"
echo "  $RAW  $(wc -c < "$RAW") 字节  sha256[:16]=$(sha256sum "$RAW" | cut -c1-16)"
echo "  目标: $REGION / 桶 $OSS_BUCKET / 对象 $OBJ / 镜像名 $IMG"

# ── 1) 确保镜像导入用的 RAM 角色存在 ────────────────────────────────
# ⚠️ 角色名**必须**是 AliyunECSImageImportDefaultRole —— 换名字导入会失败。
#    这是官方规定，不是本方案的偏好。
say "STEP 1 —— 确保镜像导入服务角色存在"
if aliyun ram GetRole --RoleName AliyunECSImageImportDefaultRole >/dev/null 2>&1; then
  echo "  已存在"
else
  echo "  不存在，创建中"
  aliyun ram CreateRole --RoleName AliyunECSImageImportDefaultRole \
    --AssumeRolePolicyDocument '{"Statement":[{"Action":"sts:AssumeRole","Effect":"Allow","Principal":{"Service":["ecs.aliyuncs.com"]}}],"Version":"1"}'
  aliyun ram AttachPolicyToRole --PolicyType System \
    --PolicyName AliyunECSImageImportRolePolicy \
    --RoleName AliyunECSImageImportDefaultRole
  echo "  已创建并挂策略"
fi

# ── 2) 上传 raw 到 OSS ──────────────────────────────────────────────
# 用 virtual-hosted 风格（ossutil 默认即是），canonical resource 会包含 bucket。
#
# ⚠️ 凭据必须**显式传给 ossutil** —— 它**不读** ALIYUN_AK / ALIYUN_SK 这两个环境变量。
#    【官方】ossutil 的凭据入口只有三处：配置文件（`ossutil config`）、
#    命令行 `-i/--access-key-id` + `-k/--access-key-secret`（另有 `-t/--sts-token`）、
#    以及 RAM 角色模式（`--mode RamRoleArn` / `EcsRamRole`）。
#    早期版本这里只写了 `--region`，读者照做会在这一步认证失败。
#
# ⚠️ 官方同时提醒：**命令行传密钥可能被日志系统记录**。
#    在意的话就用 `ossutil config` 写一份配置文件（默认 ~/.ossutilconfig），
#    然后把下面这行改成 `ossutil -c <你的配置文件> cp ...`。
say "STEP 2 —— 上传到 OSS"
ossutil cp -f "$RAW" "oss://$OSS_BUCKET/$OBJ" \
  --region "$REGION" -i "$ALIYUN_AK" -k "$ALIYUN_SK"
echo "  已上传 oss://$OSS_BUCKET/$OBJ"

# ── 3) ImportImage ──────────────────────────────────────────────────
say "STEP 3 —— 导入为自定义镜像"
# ⚠️ 三个必须写对的地方：
#    Platform='Others Linux'（不是 'Others'）
#    RoleName 必传（否则报 NoSetRoletoECSServiceAcount，官方另有 ...Account 的拼法）
#    DiskImageSize 是**GiB 整数**，要 >= 实际磁盘大小
aliyun ecs ImportImage \
  --RegionId "$REGION" \
  --ImageName "$IMG" \
  --Platform 'Others Linux' \
  --Architecture x86_64 \
  --OSType linux \
  --RoleName AliyunECSImageImportDefaultRole \
  --DiskDeviceMapping.1.OSSBucket "$OSS_BUCKET" \
  --DiskDeviceMapping.1.OSSObject "$OBJ" \
  --DiskDeviceMapping.1.DiskImageSize 1 \
  --DiskDeviceMapping.1.Format RAW

# ── 4) 等镜像就绪 ───────────────────────────────────────────────────
# 导入是异步的，刚下发时状态是 Importing。等它变成 Available 再往下走 ——
# 拿一个还在导入中的镜像去 RunInstances 会失败。
say "STEP 4 —— 等待镜像就绪"
# ⚠️ 镜像状态枚举比只判两个值要多。官方**响应**里文档化的是
#    `UnAvailable`（注意是大写 A，不是 `Unavailable`）/ `Available` /
#    `Creating` / `CreateFailed`；请求参数的 Status 枚举另含 Waiting / Deprecated。
#    代码里两种大小写都判，多判无害。
#    早期版本只判 Available 与 Unavailable —— 于是 CreateFailed 会被一直当成
#    "还在导入"，直到 20 分钟超时才报"超时"，真实原因（导入失败）反被掩盖。
i=0
while [ "$i" -lt 60 ]; do
  ST=$(aliyun ecs DescribeImages --RegionId "$REGION" --ImageName "$IMG" \
        --output cols=Status rows=Images.Image 2>/dev/null | tail -1 | tr -d ' \r')
  echo "  [$i] 状态: ${ST:-?}"
  [ "$ST" = "Available" ] && break
  case "$ST" in
    Unavailable|UnAvailable|CreateFailed|Deprecated)
      echo "X 导入失败（状态 $ST）—— 去控制台看该镜像的失败原因"; exit 1 ;;
  esac
  i=$((i + 1))
  sleep 20
done
[ "$ST" = "Available" ] || { echo "X 超时（20 分钟）仍未就绪，去控制台看导入进度"; exit 1; }

IMGID=$(aliyun ecs DescribeImages --RegionId "$REGION" --ImageName "$IMG" \
        --output cols=ImageId rows=Images.Image 2>/dev/null | tail -1 | tr -d ' \r')

say "完成"
echo "  ImageId = $IMGID"
echo
echo "  下一步把它填进 RunInstances 的 --ImageId。"
echo "  ⚠️ 系统盘大小要 >= 镜像大小（本例 1 GiB）—— 公共镜像都要 ≥20 GB，"
echo "     用公共镜像会报 InvalidSystemDiskSize.LessThanImageSize。"
echo
echo "  清理（可选）: ossutil rm oss://$OSS_BUCKET/$OBJ"
