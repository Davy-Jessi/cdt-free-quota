# 资源创建：依赖顺序与逐步命令

> ⚠️ **先读这一条**：本文件给出的是**依赖顺序**与**每步要跑的命令**。
> 标 **【未实测】** 的命令是**按官方 API 定义写的、没有逐条在干净账号上跑过**
> —— 因为它们大多是**创建操作**，跑一遍就是真花钱。
> 首次执行请**逐步来**，每步做完核对输出再往下。
>
> ⚠️ **本目录下没有任何脚本被端到端实测过** —— 端到端跑一遍就是真花钱。
> `create-cbwp.sh` 写得最细（它管的是最烧钱的那个资源），但它同样没被完整跑通过，
> 首次运行请逐段读输出再确认。

---

## 〇、变量清单（**先读这节**）

下面每一步的命令里都用到这些变量。**名字与 `.env.example` 对齐**，值是你要自己拿的：

| 变量 | 从哪来 | 备注 |
|---|---|---|
| `REGION` / `ZONE` | `aliyun ecs DescribeZones --RegionId cn-hongkong` | `.env.example` 里叫 `REGION_ID`；本方案端点**写死香港**，换地域要改 `cdtguard.py` 的 `BW_EP`/`OSS_EP`/`ZONE` |
| `ACCOUNT_UID` | 控制台「账号管理 → 安全设置」，或 `aliyun sts GetCallerIdentity` | `.env.example` 里叫 `ALIYUN_UID` |
| `VPC_ID` | §4.1 `CreateVpc` 的返回 | 用 `grep -o '"VpcId":"[^"]*"'` 回读 |
| `VSWITCH_ID` | §4.2 `CreateVSwitch` 的返回 | 同理回读 |
| `SG` | §4.3 已示范回读 | |
| `IMAGE_ID` | 依赖表第 **2** 行 `import-image.sh` 的输出 | |
| `INSTANCE_ID` | §4.5 `RunInstances` 的返回 | `.env.example` 里叫 `ECS_INSTANCE_ID` |
| `INST_NAME` | 你自己取（**不要用个人信息**） | `.env.example` 里叫 `ECS_NAME`。**只给 `--InstanceName` 用** |
| `HOSTNAME_SAFE` | 你自己取，**必须是合规主机名** | ⚠️ **与 `INST_NAME` 分开取**。主机名只允许字母/数字/连字符，不能有下划线和点。见 §4.5 表里 `--HostName` 那行 |
| `EIP_ID` | §4.6 `AllocateEipAddress` 的返回（`AllocationId`） | |
| `EIP` | 同上返回里的 `EipAddress` | **与 `EIP_ID` 是两个不同的值** |
| `CBWP_ID` | 依赖表第 **4b** 行 `create-cbwp.sh` 的输出 | 它同时要填进 `cdtguard.py` 的 `BW_ID` |
| `OSS_BUCKET` | 你自己定（**全局唯一、别嵌账号 UID**） | 要填进 `cdtguard.py` 与 `policies/` |

> ⚠️ **次序坑**：`0a` 的 RAM 策略里嵌了 `$OSS_BUCKET`（§3.1），比 `1c` 建桶**更早** ——
> 所以**先把桶名定下来**，再跑 0a。
>
> **每条创建命令后面都跟着一段回读**（`VAR=$(...)`）—— 别凭记忆猜 ID，
> 创建接口都会把 ID 返回给你，当场存进变量最省事。两种写法都能用：
>
> - `| grep -o '"VpcId":"[^"]*"' | cut -d'"' -f4` —— 适合**顶层字段**（CreateVpc 等）
> - `--output cols=<字段名> rows=<数据路径>` —— 适合**嵌套字段**。
>   ⚠️ `cols` 收的是**字段名**（如 `InstanceId`），不是路径；
>   `rows` 才是路径，且**它是 `--output` 的子参数、不带 `--`**。
>   写成 `--rows=...` 会被 CLI 直接拒绝，整条命令空手而归。
>
> ⚠️ **下面所有命令都假设你在仓库根目录执行**（`cdt-free-quota/`）。
> 少数几处写成了 `cd scripts/provision` 的相对路径，那里会单独标明。

---

## 一、核心模式：`describe` 再 `create`

**不要维护 state 文件。** 每次创建前先查「我要的东西在不在」：

```
对每个资源：
  1) describe（按 Tag 过滤）
  2) 已存在 → 核对参数是否与预期一致 → 跳过
  3) 不存在 → create
```

### 为什么用 Tag 而不是名字

**资源名不是唯一的。** 共享带宽包的 `Name` 只是 0–128 字符的自由字段，
**没有任何唯一性约束**。按名字判重会在第二次运行时命中**上一次那个**，
然后把参数不对的资源接管下来 —— 按带宽计费的包一旦接管错，就是实打实的钱。

**所以**：每次创建都带 `Tag`（例如 `cdt-role=guard`、`cdt-purpose=cbwp`），
每次判重都按 `Tag` 查。下面每条 create 命令都带了 Tag（这四类 VPC/VSwitch/安全组/EIP
的创建接口都支持 `Tag.N.Key`/`Tag.N.Value`，**已核对官方参数表**）。

⚠️ 但要说清**哪些资源的判重是保命的**，否则你会把力气用错地方：

| 资源 | 按名字判重的后果 | 判重靠 Tag 的必要性 |
|---|---|---|
| **共享带宽包 / EIP** | 🔴 **要花钱**。接管错的那个 = 建了个持续计费的包 | **必须** |
| **ECS 实例** | 🔴 认错实例 = 该停的没停（流量继续烧）/ 不该停的停了 | **必须** |
| VPC / VSwitch / 安全组 | 基本无害 —— 不单独计费，重名只会让 `describe` 多返回一条 | 为一致性 + 幂等，仍然带上 |

结论：**Tag 是这几类资源的统一判据**；但真正会让你亏钱的是前两类，审计时先看它们。

### `ClientToken` 能防什么、不能防什么

所有创建接口都支持 `ClientToken`，它防的是**「同一次请求被重发」**。

**它防不了「进程重启后再跑一遍」** —— 没有按 `ClientToken` 查的 describe 接口。
跨 run 的幂等只能靠 Tag + describe。

---

## 二、依赖顺序（照这个顺序，无环）

| # | 步骤 | 方法 |
|---|---|---|
| 0a | RAM 策略 | 见 §3.1 |
| 0b | RAM 角色 ×3 | 见 §3.2 |
| 0c | VPC | 见 §4.1 |
| 1a | VSwitch | 见 §4.2 |
| 1b | 安全组 + 规则 | 见 §4.3 |
| 1c | OSS Bucket | 见 §4.4 |
| 2 | 自定义镜像 | [`../build-image.sh`](../build-image.sh) → [`../import-image.sh`](../import-image.sh) |
| 3 | ECS | 见 §4.5 |
| 4a | EIP | 见 §4.6 |
| 4b | 共享带宽包 | [`create-cbwp.sh`](create-cbwp.sh) |
| 5a | EIP 加入共享带宽包 | 见 §4.7 |
| 5b | EIP 绑定 ECS | 见 §4.8 |
| 6 | FC 函数 | 见 §5.1 |
| 7 | EventBridge 规则 | 见 §5.2 |
| 8a | FC 触发器（timer ×2） | 见 §5.3 |
| 8b | FC 触发器（eventbridge） | 见 §5.3 |
| 9 | 部署守护 | [`../deploy-cdtguard.sh`](../deploy-cdtguard.sh) |

> 三个最容易搞错的：
> - **RAM 不能排最后** —— `RunInstances` / `CreateFunction` / `ImportImage` 都要角色 ARN
> - **OSS 必须在 ECS 之前** —— 守护的状态存 OSS，桶不存在就启动守护 = 失败时静默放行
> - **EventBridge 规则必须在 `eventbridge` 型触发器之前** —— 触发器要指向已有规则

---

## 三、RAM

### 3.1 运行时策略

```sh
# 创建策略（策略正文就是仓库里那份，替换桶名占位符后上传）
sed "s|<YOUR_OSS_BUCKET>|$OSS_BUCKET|" policies/cdtguard-runtime-policy.json > /tmp/p.json
aliyun ram CreatePolicy --PolicyName cdtguard-runtime-policy \
  --PolicyDocument "$(cat /tmp/p.json)"

# 挂到角色上
aliyun ram AttachPolicyToRole --PolicyType Custom \
  --PolicyName cdtguard-runtime-policy --RoleName cdtguard-runtime
```

> ⚠️ **`policies/cdtguard-runtime-policy.json` 里的 OSS 资源写的是
> `acs:oss:*:*:<YOUR_OSS_BUCKET>/cdtguard/*`** —— 必须先替换成你的桶名，
> 否则守护连自己的状态对象都读写不了。

> 🔴 **同一份策略里，`ecs:StopInstance` 的资源范围是 `*`，不是那一台实例 ——
> 也就是说这个角色能停你账号下**任意**一台 ECS。**
> 这不是随手写的：官方 API 表里 `ecs:StopInstance` **支持**资源级授权
> （资源类型 `Instance`，ARN `acs:ecs:{regionId}:{accountId}:instance/{instanceId}`），
> 所以它**可以**收窄。之所以没写死，是因为实例 ID 在创建之后才知道，
> 而这份策略要在 §3.1 就建好 —— 想收窄就**在拿到实例 ID 之后再改一次策略**：
>
> ```json
> { "Effect": "Allow", "Action": ["ecs:StopInstance"],
>   "Resource": "acs:ecs:cn-hongkong:<YOUR_ACCOUNT_UID>:instance/<YOUR_INSTANCE_ID>" }
> ```
>
> ⚠️ 同理，`ecs:TagResources` / `ecs:UntagResources` 也是 `*`。
> 本仓库**没有**把它们收窄过，也没有实测过收窄后守护是否仍能正常打标签 ——
> 所以上面那段是**方向建议**，不是【实测】结论。收窄后请按 SKILL 第 5 步的
> `DryRun` 自检确认停机路径没被打死（那一步正是为这类问题准备的）。

### 3.2 三个角色 **【未实测】**

方案一共要 **3 个角色**。仓库只提供了第 1 个的信任策略：

| 角色 | 谁 assume | 信任策略 | 挂在什么上 |
|---|---|---|---|
| `cdtguard-runtime` | ECS | [`../../policies/cdtguard-role-trust.json`](../../policies/cdtguard-role-trust.json) | ECS 实例（`RunInstances --RamRoleName`） |
| FC 执行角色 | FC | 见下 | FC 函数（`CreateFunction --role`） |
| `AliyunECSImageImportDefaultRole` | ECS | **固定名**，由 `import-image.sh` 自动创建 | 镜像导入 |

```sh
# 第 1 个：ECS 守护的运行时角色
aliyun ram CreateRole --RoleName cdtguard-runtime \
  --AssumeRolePolicyDocument "$(cat policies/cdtguard-role-trust.json)"

# 第 2 个：FC 执行角色（信任策略形如下面这份；权限策略按最小权限自拟）
aliyun ram CreateRole --RoleName cdt-keeper-role \
  --AssumeRolePolicyDocument '{"Statement":[{"Action":"sts:AssumeRole","Effect":"Allow","Principal":{"Service":["fc.aliyuncs.com"]}}],"Version":"1"}'

# 第 3 个：镜像导入角色 —— 名字是官方规定的，import-image.sh 会自动建
```

> 🔴 **`RunInstances --RamRoleName` 要填的就是 `cdtguard-runtime`** ——
> 本仓库早期版本没有把这个名字写在任何地方，读者只能猜。
>
> 🔴 **FC 执行角色的权限要自己定**（仓库没给）：它至少要能
> `ecs:DescribeInstances` / `ecs:StartInstance` / `ecs:StopInstance`，
> 以及读写那个 OSS 对象的 `oss:GetObject` / `oss:PutObject`。
>
> ⚠️ 但**当前实现里 FC 用的是环境变量里的长期 AK/SK**（见 `fc-keeper/index.js`），
> 不是这个角色 —— 角色是更安全的方向，属于**待改进项**。

---

## 四、网络与计算

### 4.1 VPC **【未实测】**

```sh
VPC_ID=$(aliyun vpc CreateVpc --RegionId "$REGION" --CidrBlock 172.16.0.0/12 --VpcName cdt-vpc \
  --Tag.1.Key cdt-role --Tag.1.Value guard \
  | grep -o '"VpcId":"[^"]*"' | cut -d'"' -f4)
echo "$VPC_ID"
```

### 4.2 VSwitch **【未实测】**

```sh
VSWITCH_ID=$(aliyun vpc CreateVSwitch --RegionId "$REGION" --VpcId "$VPC_ID" \
  --CidrBlock 172.16.0.0/24 --ZoneId "$ZONE" --VSwitchName cdt-vsw \
  --Tag.1.Key cdt-role --Tag.1.Value guard \
  | grep -o '"VSwitchId":"[^"]*"' | cut -d'"' -f4)
echo "$VSWITCH_ID"
```

### 4.3 安全组 + 规则 **【未实测】**

```sh
SG=$(aliyun ecs CreateSecurityGroup --RegionId "$REGION" --VpcId "$VPC_ID" \
       --SecurityGroupName cdt-sg \
       --Tag.1.Key cdt-role --Tag.1.Value guard | grep -o '"SecurityGroupId":"[^"]*"' | cut -d'"' -f4)

# ⚠️ 只放你真正需要的端口。本方案的代理入口是 443（REALITY）。
#    不要把 22 开给 0.0.0.0/0 —— 用你的固定出口 IP 或走跳板。
aliyun ecs AuthorizeSecurityGroup --RegionId "$REGION" --SecurityGroupId "$SG" \
  --IpProtocol tcp --PortRange 443/443 --SourceCidrIp 0.0.0.0/0 --Description proxy
```

> ⚠️ **不要开 22 给全网。** 本仓库的镜像用的是公钥登录（`PermitRootLogin prohibit-password`），
> 但把 22 暴露给 0.0.0.0/0 仍是不必要的风险面。

### 4.4 OSS Bucket **【未实测】**

```sh
ossutil mb "oss://$OSS_BUCKET" --region "$REGION"
```

> ⚠️ 桶名**全局唯一**，且**不要用账号 UID 拼** —— 桶名一出现就等于公开账号 UID，
> 而且它会出现在策略 ARN、日志、截图里。随便取个不带个人信息的名字即可。

### 4.5 ECS **【未实测，且是最容易漏参数的一步】**

> 🔴⚠️⚠️ **先看这一行再抄下面的命令：确认里面有 `--SpotInterruptionBehavior Stop`。**
> 它是「节省停机模式」的开关，官方默认值是 `Terminate` —— **直接释放实例、
> 连系统盘一起销毁**。而它**创建后不可修改**：漏了只能删掉实例重建。
>
> 为什么把警告提到命令**前面**：这段警告原先只写在下面的参数表里（表在命令之后）。
> 本仓库的 [`pre-commit-checklist.md`](../../docs/pre-commit-checklist.md) 明令自查
> 「有没有步骤**按字面执行会有不可逆后果或费用，而警告写在后面**」——
> 这就是那一类。**抄命令的人不会先往下读 30 行表格。**
>
> 后果链（漏了它 = 整套守护静默失效）：实例被销毁 → 实例 ID 失效、Tag 消失 →
> FC 的 `StartInstance` 兜底**永远不可能成功** → 回退查找也找不到 → **静默 fail-open**。

```sh
aliyun ecs RunInstances \
  --RegionId "$REGION" \
  --ZoneId "$ZONE" \
  --ImageId "$IMAGE_ID" \
  --InstanceType ecs.e-c1m1.large \
  --InstanceChargeType PostPaid \
  --SpotStrategy SpotAsPriceGo \
  --SpotDuration 0 \
  --SpotInterruptionBehavior Stop \
  --SystemDisk.Category cloud_essd \
  --SystemDisk.Size 1 \
  --SystemDisk.PerformanceLevel PL0 \
  --VSwitchId "$VSWITCH_ID" \
  --SecurityGroupId "$SG" \
  --RamRoleName cdtguard-runtime \
  --InstanceName "$INST_NAME" \
  --HostName "$HOSTNAME_SAFE" \
  --Tag.1.Key cdt-role --Tag.1.Value guard \
  --Amount 1
```

```sh
# 回读实例 ID。⚠️ RunInstances 的返回字段是 **InstanceIdSets.InstanceIdSet**，
#    不是 InstanceId —— 用 `grep '"InstanceId"'` 会**什么都抓不到**。
INSTANCE_ID=$(aliyun ecs DescribeInstances --RegionId "$REGION" \
  --InstanceName "$INST_NAME" \
  --output cols=InstanceId rows=Instances.Instance | tail -1 | tr -d ' \r')
echo "$INSTANCE_ID"
```

**每个参数为什么是它：**

| 参数 | 说明 |
|---|---|
| `--ImageId` | 必须是依赖表第 **2** 行建出来的自定义镜像。**公共镜像都 ≥20 GB，而这里系统盘只要 1 GB** ⇒ 用公共镜像会报 `InvalidSystemDiskSize.LessThanImageSize` |
| `--InstanceType` | 🔴 **e 系列的型号名要把内存那一位读对。** 命名是 `<vCPU>:<GiB>` 的配比 —— 本方案用 `ecs.e-c1m1.large`（**2 vCPU / 2 GiB**）。⚠️ 别顺手换成 `ecs.e-c4m1.large`：它是 **1:4** 配比 ⇒ 2 vCPU 只有 **0.5 GiB** 内存，而本方案要在这台机器上同时跑**守护 + 代理**。同系列可选：`c1m1` 2 GiB / `c1m2` 4 GiB / `c1m4` 8 GiB。**改规格前先确认内存那一列**（`aliyun ecs DescribeInstanceTypes --RegionId "$REGION" --InstanceTypes.1 <型号>` 看 `MemorySize`） |
| `--SpotStrategy SpotAsPriceGo` | 抢占式按市场价格自动出价 = 成本最低。⚠️ 代价是随时可能被回收 —— 这正是 FC 兜底存在的原因 |
| `--SpotDuration 0` | 抢占式保留时长。**只支持 0 或 1**：`1` = 保证创建后 1 小时不被释放；`0` = 不保证运行时长，系统实时比较出价/库存决定回收。本方案要 7×24 常驻，1 小时保护没有意义，故取 `0` |
| 🔴⚠️⚠️ `--SpotInterruptionBehavior Stop` | **这一行是整条守护链的前提。删掉它，整套设计静默失效。** 它才是「节省停机模式」的开关 —— **不是** `SpotStrategy`，也**不是** `SpotDuration`。官方默认值是 **`Terminate`：直接释放实例，连系统盘一起销毁**。后果链：实例被销毁 → 实例 ID 失效、Tag 消失 → FC 的 `StartInstance` 兜底永远不可能成功 → 回退查找也找不到 → **静默 fail-open**（同 `docs/pitfalls.md` 的「不可逆操作」表）。⚠️ **该参数创建后不可修改** —— 漏了只能重建实例 |
| `--SystemDisk.Size 1` | 1 GB。**必须 ≥ 镜像大小**。⚠️ 必须同时给 `--SystemDisk.PerformanceLevel PL0` —— ESSD 默认 PL1，而下限是 **20 GiB**，`Size=1` 会落在合法区间外 |
| `--RamRoleName cdtguard-runtime` | ← **就是 §3.2 那个角色名**。填错或不填，守护拿不到凭据 |
| `--Tag.1.Key cdt-role` | 给 FC 一个可在实例 ID 失效时回退查找的锚点（**当前实现里 FC 还没用上它**，属待改进项） |
| `--HostName` | ⚠️ **不要直接拿 `$INST_NAME` 用。** 主机名规则比实例名严：只允许**字母、数字、连字符**，**不允许下划线、点**，且不能以连字符开头/结尾。`INST_NAME` 是自由字段（完全可以叫 `cdt_guard_1`），照抄会被 `InvalidHostname` 拒掉。另取一个合规变量（例如 `cdt-guard-01`），或省略本参数让阿里云自生成 |

> ⚠️ **`--InternetMaxBandwidthOut` 不要在这里设** —— 公网出口走 EIP + 共享带宽包（§4.6/§4.7），
> 不在这里开固定带宽。开了就是「固定带宽计费」，**吃不到 CDT 免费额度**。

### 4.6 EIP **【未实测】**

```sh
EIP_JSON=$(aliyun vpc AllocateEipAddress --RegionId "$REGION" \
  --InternetChargeType PayByTraffic \
  --Bandwidth 200 --ISP BGP --Name cdt-eip \
  --Tag.1.Key cdt-purpose --Tag.1.Value cdt-eip)
EIP=$(echo "$EIP_JSON" | grep -o '"EipAddress":"[^"]*"' | cut -d'"' -f4)
EIP_ID=$(echo "$EIP_JSON" | grep -o '"AllocationId":"[^"]*"' | cut -d'"' -f4)
echo "$EIP  $EIP_ID"
```

> ⚠️ **这一步必须回读** —— §4.7 与 §4.8 直接用 `$EIP_ID`。
> 不回读的话它是空的，`AddCommonBandwidthPackageIp` 会把 EIP 加到错的包上。

```sh
# 回读：**两个不同的值** —— AllocationId（资源 ID）与 EipAddress（IP 本身）
EIP_ID=$(aliyun vpc DescribeEipAddresses --RegionId "$REGION" --EipAddress "$EIP" \
  --output cols=AllocationId rows=EipAddresses.EipAddress | tail -1 | tr -d ' \r')
echo "$EIP_ID"
```

> 🔴 **`--InternetChargeType` 必须是 `PayByTraffic`（按流量）。**
> 选固定带宽 ⇒ 吃不到免费额度，还照常收费。
> 🔴 **`--ISP` 必须是 `BGP`**，`BGP_PRO` 一分额度都用不上。

### 4.7 EIP 加入共享带宽包 **【未实测】**

```sh
aliyun vpc AddCommonBandwidthPackageIp --RegionId "$REGION" \
  --BandwidthPackageId "$CBWP_ID" --IpInstanceId "$EIP_ID"
```

### 4.8 EIP 绑定 ECS **【未实测】**

```sh
aliyun vpc AssociateEipAddress --RegionId "$REGION" \
  --AllocationId "$EIP_ID" --InstanceId "$INSTANCE_ID"
```

> 🔴 **用 `vpc` 版，不要用 `ecs` 版。** 官方 ECS 版页面开头就写着：
> 「目前该API接口已标记为弃用，推荐使用替代API：**Vpc(2016-04-28) - AssociateEipAddress**」。
> 弃用版的命令**现在还能跑通** —— 所以这不是「会报错」，而是「照着抄会写到
> 一个正在被淘汰的接口上」。本文件其它 EIP 操作（§4.6/§4.7）本来就用 `vpc`。
>
> 参数**完全兼容**：VPC 版同样收 `RegionId` / `AllocationId` / `InstanceId`，
> 且 `InstanceType` 默认就是 `EcsInstance`，所以绑 ECS 时不必显式给。
> ⚠️ 它和 ECS 版一样是**异步**接口：返回 RequestId 只代表受理。
> 要确认绑好了，查 `DescribeEipAddresses` 看状态 —— `Associating` 是进行中、
> `InUse` 才是绑定完成。

---

## 五、函数计算（FC 兜底）

### 5.1 创建函数 **【未实测】**

> ⚠️ **`deploy-fc.py` 只能「更新已存在的函数」，不能从零创建** ——
> 它第一步就 `get_function()`，函数不存在会直接抛异常。
> 所以**第一次必须先把空函数建出来**，之后的代码更新才走 `deploy-fc.py`。

**正路（推荐）：先用控制台建一个空的 `cdt-keeper`，然后本地跑部署脚本。**
`scripts/deploy-fc.py` 自己就会打 zip 并上传，还会**合并**环境变量（不覆盖）：

```sh
# 先把六个必需环境变量填进 FC（控制台或下面的 CreateFunction），然后：
pip install alibabacloud_fc20230330 alibabacloud_tea_openapi
# ⚠️ 用**真正的换行续行**（行尾反斜杠），别写成字面量 `\n` ——
#    bash 会把 `\n` 当成「转义的 n」，于是它"能跑"但会多定义一个叫 n 的变量，
#    而且 ECS_INSTANCE_ID 根本没被导出。
export ALIYUN_AK=... ALIYUN_SK=... ALIYUN_UID="$ACCOUNT_UID" \
       ECS_INSTANCE_ID="$INSTANCE_ID" REGION_ID="$REGION"
python3 scripts/deploy-fc.py scripts/fc-keeper/index.js
```

**若要从零建（不推荐，参数多且易错）**，先把代码打成 zip 再传 OSS ——
`CreateFunction` 的 `code` 指向的是一个**必须已存在的对象**：

```sh
(cd scripts/fc-keeper && zip -j /tmp/index.zip index.js)
ossutil cp -f /tmp/index.zip "oss://$OSS_BUCKET/fc/index.zip"   --region "$REGION" -i "$ALIYUN_AK" -k "$ALIYUN_SK"
```

⚠️⚠️ **FC 3.0 是 ROA 风格 —— `CreateFunction` 只认 `--body`（一个 JSON）。**
不存在 `--functionName` / `--runtime` / `--code` 这种扁平 flag；
写成扁平参数会**硬报错**：`"--xxx" is not a valid parameter or flag`。

```sh
aliyun fc CreateFunction   --endpoint "$ACCOUNT_UID.$REGION.fc.aliyuncs.com"   --method POST --force   --body '{
    "functionName": "cdt-keeper",
    "runtime": "nodejs18",
    "handler": "index.handler",
    "memorySize": 128,
    "timeout": 60,
    "role": "acs:ram::<YOUR_ACCOUNT_UID>:role/cdt-keeper-role",
    "code": {"ossBucketName": "<YOUR_OSS_BUCKET>", "ossObjectName": "fc/index.zip"},
    "environmentVariables": {
      "ALIYUN_AK": "<YOUR_AK_ID>", "ALIYUN_SK": "<YOUR_AK_SECRET>",
      "REGION_ID": "cn-hongkong", "ECS_INSTANCE_ID": "<YOUR_INSTANCE_ID>",
      "ECS_NAME": "<YOUR_INSTANCE_NAME>", "EIP": "<YOUR_EIP>",
      "ZONE": "cn-hongkong-c", "TRAFFIC_THRESHOLD_GB": "200",
      "OSS_BUCKET": "<YOUR_OSS_BUCKET>", "OSS_ENDPOINT": "oss-cn-hongkong.aliyuncs.com",
      "PUSH_TOKEN": "<YOUR_PUSHPLUS_TOKEN>"
    }
  }'
```

> ✅ **`--body` 里的字段名是对的** —— 已逐字段对过官方结构表
> （`CreateFunctionInput` / `InputCodeLocation`）：`functionName` / `runtime` /
> `handler` / `memorySize` / `timeout` / `role` / `code{ossBucketName,ossObjectName}` /
> `environmentVariables`（object）全部存在。**错的只是不该把它们当 CLI flag。**

> 🔴 **`environmentVariables` 是整体覆盖语义** —— `UpdateFunction` 时漏一个键 = 删一个键。
> 所以 `deploy-fc.py` 走的是「先 Get 读回 → 合并 → 写回」，**绝不硬编码清单**。

**FC 侧必需的 6 个键**（`deploy-fc.py` 的 `REQUIRED_ENV` 会断言它们都在）：

`ALIYUN_AK` · `ALIYUN_SK` · `REGION_ID` · `ECS_INSTANCE_ID` · `OSS_BUCKET` · `PUSH_TOKEN`

（另有 5 个有默认值的可选键：`ECS_NAME` `EIP` `ZONE` `TRAFFIC_THRESHOLD_GB` `OSS_ENDPOINT`）

> ⚠️ 别顺手加 `TZ` —— **FC 代码里没有任何地方读它**（`index.js` 全文中 `process.env.TZ` 命中数为 0）。
> 加了不生效，只会让下一个人以为时区可控。FC 的时间判定走的是
> [`../fc-keeper/index.js`](../fc-keeper/index.js) 里手动算的 UTC+8
> （`new Date(Date.now() + 8 * 3600 * 1000)`），**不是** `TZ`。
>
> ⚠️ **键数要按你**自己**那台函数去数，别照抄一个数字。** 本仓库不记录任何
> 具体部署环境的键数或键值 —— 那属于「部署环境特有的选择」（见
> [`../../docs/pre-commit-checklist.md`](../../docs/pre-commit-checklist.md)
> **二、语义清单**，那里给了判据：*「这个值是部署环境特有的，还是任何读者都会有的？」*）。
> 你可以加 `TZ` 之类的死配置，也可以不加；`deploy-fc.py` 都不会删它
> （它是「Get 读回 → 原样合并 → 写回」，不硬编码清单），所以**以线上现状为准**。
>
> ⚠️ **而 `inventory.json` 的 `requiredEnv` 只声明上面那 11 个键** ——
> 因为本仓库另一处（`scripts/verify-deploy.py`）要拿它去回读线上。
> 若把「文档说别加」的 `TZ` 也列进去，**照文档建出来的部署会被判红**。
> 判据是「线上 ⊇ 声明」，所以多传一个 `TZ` 不算错。

### 5.1.1 怎么确认线上跑的是哪一版 **【实测】**

> **这一段解决的是一个很容易踩的盲区**：仓库里的 FC 代码和线上跑的那份
> 很可能是**两条独立的线** —— 各有各的修复，谁也不是谁的超集。
> 那时没有任何判据能回答「线上到底是哪一版」，而最直觉的答案（比文件哈希）是**错的**。

**三个判据，可靠性递增：**

| 判据 | 怎么取 | 可信度 |
|---|---|---|
| ① 函数元数据 | `aliyun fc GET /2023-03-30/functions/cdt-keeper --region cn-hongkong` | 低 —— `codeSize` 是 **zip 后**大小，不是源文件大小 |
| ② 拉部署包比指纹 | 见下 | 中 —— 能证明「字节一致」，但**注释改一行就变** |
| ③ **运行时自报 `VER`** | `deploy-fc.py` 自动做；手工见下 | **高** —— 来自实际执行的那份代码 |

⚠️ **为什么 ② 单独不够**：FC 的**热实例在 `UpdateFunction` 之后可能仍跑旧代码**。
此时你拿本地的 `sha256` 去比线上部署包，会得出「一致」，但实际在跑的是上一版。
只有 ③ 绕不过去 —— 它是那次调用**真的执行了哪份代码**的回答。

**② 的取法（只读，不动线上）：**

```sh
# 1) 拿到一个预签名 URL（响应里的 url 字段）
aliyun fc GET /2023-03-30/functions/cdt-keeper/code --region cn-hongkong
# 2) 拉下来解开
curl -sL -o /tmp/fc.zip '<上一步的 url>'
unzip -o /tmp/fc.zip -d /tmp/fc && sha256sum /tmp/fc/index.js
```

> ⚠️ 这个 URL **是签名过的、几分钟就过期**，而且签名**绑定了 HTTP 方法**：
> 用 `PUT` 签出来的 URL 拿去 `GET` 会返回 `AccessDenied`。
> 另外若你手工构造签名，`Expires` 必须是**纯十进制秒**——
> shell 转义稍有不慎就会把 `\&` 变成 URL 的一部分，得到
> `Invalid date (should be seconds since epoch)`。

**③ 的取法：**

```sh
python3 scripts/deploy-fc.py scripts/fc-keeper/index.js   # 部署后自动断言 ver
```

**回滚锚点**：改动线上 FC 之前，先把当前部署包**原样存一份**（FC 跑在 `LATEST`，
**没有版本回退路径**）。取法与上面 ② 相同，存到被守护实例之外的地方。

### 5.1.2 为什么会分叉 —— 以及怎么避免

**这是这类「线上 + 仓库」双轨部署的通病，不是本书特有的事故。**

**现象**：仓库版与线上版各拿到一半修复，两边都不是对方的超集。

| 能力 | 可能只存在于线上 | 可能只存在于仓库 |
|---|---|---|
| OSS 读不到就响铃（`error-oss-unreadable`） | **有** | 无 |
| 十进制 GB（判据口径） | 无（用 1024³） | **有** |
| 阈值取护闸发布值（`threshold_gb`） | 无 | **有** |
| 缺环境变量即拒启（`requireEnv`） | 无 | **有** |

**成因**：一次线上热修走了**直接改线上**的路径，**没有回流到仓库**；
而仓库那边在同一时间段独立地加了别的改进，**也没有上线**。
两边都没记录对方的东西，所以差异一直没被发现。

**处置**：取**并集** —— 以仓库版为基线，把线上独有的那支分支移植进来，
`VER` 重建为单一值。**不是**「谁覆盖谁」：反向覆盖会丢掉十进制口径
（与 `quota-guard` 规格冲突），正向覆盖会丢掉那条告警。

> **教训**：**热修线上之后必须回流仓库**，否则下一次「按仓库部署」就是一次**静默回退**。
> 检验办法就是 §5.1.1 那条：用运行时自报的 `VER` 回答「线上跑的到底是哪一版」。

### 5.2 EventBridge 规则 **【未实测】**

```sh
aliyun eventbridge CreateRule --RuleName cdt-ecs-statechange \
  --EventBusName default \
  --FilterPattern '{"type":["ecs:Instance:StateChange","ecs:Instance:PreemptibleInstanceInterruption"]}' \
  --EventTargets.1.Id fc \
  --EventTargets.1.Type acs.fc.function \
  --EventTargets.1.Endpoint "acs:fc:$REGION:$ACCOUNT_UID:services/__FC3.LATEST/functions/cdt-keeper" \
  --EventTargets.1.ParamList.1.ResourceKey serviceName    --EventTargets.1.ParamList.1.Form CONSTANT --EventTargets.1.ParamList.1.Value __FC3 \
  --EventTargets.1.ParamList.2.ResourceKey functionName   --EventTargets.1.ParamList.2.Form CONSTANT --EventTargets.1.ParamList.2.Value cdt-keeper \
  --EventTargets.1.ParamList.3.ResourceKey Qualifier      --EventTargets.1.ParamList.3.Form CONSTANT --EventTargets.1.ParamList.3.Value LATEST \
  --EventTargets.1.ParamList.4.ResourceKey InvocationType --EventTargets.1.ParamList.4.Form CONSTANT --EventTargets.1.ParamList.4.Value Sync \
  --EventTargets.1.ParamList.5.ResourceKey Body           --EventTargets.1.ParamList.5.Form ORIGINAL \
  --EventTargets.1.ParamList.6.ResourceKey DataFormat     --EventTargets.1.ParamList.6.Form CONSTANT --EventTargets.1.ParamList.6.Value Object
```

> 🔴 **必须用 `--EventTargets.N.Field` 的点号形式，不能传一整串 JSON。**
> 这条已经用真实 CLI 验证过：传 `--EventTargets '[{...}]'` 会直接报
> `missing required parameter(s): --EventTargets`（`=` 连接也一样报错）——
> 因为对这类「对象数组」参数，CLI 只认点号展开，它不像 `--body` 那样收 JSON 串。
> 点号形式会被正确拼进查询串：`EventTargets.1.Id=fc&EventTargets.1.Type=...`

> 🔴⚠️⚠️ **`Endpoint` 必须带 `.Qualifier`，`ParamList` 一个都不能少。**
> 官方《事件目标参数》给出的格式是
> `acs:fc:${region}:${uid}:services/${serviceName}.${Qualifier}/functions/${functionName}`，
> 并且以下 6 个 resourceKey **全部标「必传」**：
> `serviceName` · `functionName` · `Body` · `Qualifier` · `InvocationType` · `DataFormat`
> （出处：<https://help.aliyun.com/zh/eventbridge/user-guide/event-target-parameters>）
>
> 少了它们，规则**能建出来但投递不生效** —— 而失败的形态是「静默」：
> 没有任何报错，只是事件永不触发。**这类"建成功了但不工作"最难发现。**
>
> `Body` 用 `ORIGINAL`：FC 侧的处理函数直接读 `event.resourceId` / `event.data.instanceId`，
> 要的就是**原样**的事件体，不要在这里做模板裁剪。

> ⚠️ **上面这段命令里，只有 `Endpoint`/`Type`/`Id` 那一层点号形式被真机验证过。**
> `ParamList.N.*` 这种**两层嵌套**是否也能用点号展开，**本仓库无法核实**
> （没有可用的 EventBridge 环境）。如果 CLI 拒绝了它，就**用控制台或
> `CreateTargets` API 建这个 Target** —— 官方文档给的是 JSON 形式，
> 照上面那 6 个键填即可。**别因为命令跑不通就跳过 ParamList。**
>
> ⚠️ 端点里的伪服务名 `services/__FC3` —— FC 3.0 没有 service 概念，
> 这个占位符是 FC 3.0 的约定；`${Qualifier}` 位填 `.LATEST`。
> 同样**未经真机核实**，以你的实测为准。

> ✅ **建完必须验一次投递**，别只看「创建成功」：
> 1. 手工停一下实例（或对实例打一次 Tag，只要能触发 `StateChange`）
> 2. 看 FC 的**调用日志**里有没有这次调用 —— 有 = 链路通；没有 = ParamList/Endpoint 有问题
> 3. 更省事的办法：`aliyun eventbridge PutEvents` 直接投一条测试事件到 `default` 总线
>
> ⚠️ **这条链路不通的后果是「变慢」不是「失明」**：`ecs-statechange` 只是让 FC
> **立刻**反应；真正兜底的每日巡检与 8 分钟存活巡检都是 **timer 触发器**，
> 不经过 EventBridge。所以链路不通时系统仍会恢复实例，只是晚最多 1 天。
> **但抢占预警就完全收不到了** —— 那个只走事件。

> ⚠️ filter 里那**两个**事件类型写在**同一条规则**里 ——
> 抢占事件**不需要**单独的规则（把它拆成两条规则是常见误解）。

### 5.3 FC 触发器 ×3 **【未实测】**

**方案一共要 3 个触发器**，不是 2 个：

| 触发器 | 类型 | 周期 / 条件 | 作用 |
|---|---|---|---|
| `daily-0005-cst` | timer | `0 5 16 * * *`（= UTC+8 每日 00:05） | 每日巡检 |
| `ecs-statechange` | eventbridge | 见 §5.2 | 实例状态变化 / 抢占 |
| **`guard-liveness-8min`** | timer | `0 */8 * * * *` | **dead-man's switch** |

```sh
# ⚠️ 同样只认 `--body`（`--triggerName`/`--triggerType`/`--triggerConfig` 都不存在）
#    `--functionName` 是 path 参数，要留着。
#
# 🔴 两个 timer 的 payload **不一样**，不能共用一个循环：
#    `guard-liveness-8min` 必须带 `{"liveness": 1}`，否则 fc-keeper 的 `isLiveness`
#    判据恒为 undefined ⇒ 落进「timer 且非存活巡检」分支 ⇒ 走**每日报告**那条路
#    ⇒ **每 8 分钟推一条**（约 180 条/天）。
#    ⚠️ 注意别把后果说大：「护闸失联」告警**不受影响** —— 它在 `isLiveness`
#       分支**之前**判断，只看 OSS 状态的时间戳，与 payload 无关。
#       漏 payload 的代价只有刷屏，不是失去监控。
aliyun fc CreateTrigger --endpoint "$ACCOUNT_UID.$REGION.fc.aliyuncs.com"   --method POST --force --functionName cdt-keeper   --body '{"triggerName":"daily-0005-cst","triggerType":"timer",
           "triggerConfig":"{\"cronExpression\":\"0 5 16 * * *\",\"enable\":true,\"payload\":\"{}\"}"}'

aliyun fc CreateTrigger --endpoint "$ACCOUNT_UID.$REGION.fc.aliyuncs.com"   --method POST --force --functionName cdt-keeper   --body '{"triggerName":"guard-liveness-8min","triggerType":"timer",
           "triggerConfig":"{\"cronExpression\":\"0 */8 * * * *\",\"enable\":true,\"payload\":\"{\\\"liveness\\\": 1}\"}"}'

# eventbridge 型（依赖 §5.2 的规则先建好）
# ⚠️ `sourceArn` 是 **CreateTriggerInput 的顶层字段**，不在 `triggerConfig` 里。
aliyun fc CreateTrigger --endpoint "$ACCOUNT_UID.$REGION.fc.aliyuncs.com"   --method POST --force --functionName cdt-keeper   --body '{"triggerName":"ecs-statechange","triggerType":"eventbridge",
           "sourceArn":"acs:eventbridge:$REGION:$ACCOUNT_UID:eventbus/default",
           "triggerConfig":"{\"triggerEnable\":true}"}'
```

> ⚠️ `payload` 是**字符串化的 JSON**（不是嵌套对象）—— 这正是 `fc-keeper`
> 头部「定时器的 payload 是字符串，不是解析好的对象」那条坑。

> 🔴 **别漏掉 `guard-liveness-8min`。**
> 它是**唯一**能在护闸悄悄死掉时发出告警的机制（详见 `fc-keeper/index.js` 头部）。
> 本仓库早期版本的依赖表只列了 2 个触发器，照它建出来的系统**没有这个保护**。
>
> 💰 **它也是成本来源**：8 分钟一次 ⇒ 每小时都有调用 ⇒
> 按 FC「有调用的小时至少 ¥0.01」算，**约 ¥7.9/月**。
> **这不是 ¥0.33/月** —— 后者要求周期 ≥ 1 天。见 [`../../docs/pitfalls.md`](../../docs/pitfalls.md) 第 5 条。

---

## 六、用 MCP 还是 CLI

两条路都行。**成本预估两边都有**：

| | 阿里云 MCP | `aliyun` CLI |
|---|---|---|
| 成本预估 | ✅ `CallCLI` 执行前返回报价 JSON | ✅ 自带 `--estimate-cost` |
| 凭据 | 服务端注入，不用落地 | 本地配置 |
| 脚本化 | 要经 MCP | 直接 |

> ⚠️ 但**别把报价当护栏**：按量资源**创建时刻**的预估价接近 ¥0
> （共享带宽包的费用公式里有「当日最大带宽峰值」，创建时根本估不出来）。
> **它防不了「不足 1 小时按 1 小时」。** 真正防它的是「不建按带宽计费的包」这条硬规则。
