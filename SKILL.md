---
name: cdt-free-quota
description: 在阿里云中国站部署一套「拿满 CDT 每月 220 GB 免费公网流量、且绝不超额」的方案。含资源创建、守护部署、函数计算兜底与出账核对。当用户要求「低成本用阿里云流量」「CDT 免费额度」「把 220G 用满且不超额」时使用。
---

# cdt-free-quota

> ⚠️ **先读 [`DISCLAIMER.md`](DISCLAIMER.md)。** 本方案的通常用途被阿里云服务条款明文禁止。
> 开工前必须让用户知道这件事并明确同意。

## 目标

在用户的阿里云**中国站**账号上，建成一套：

1. 用满 CDT 免费公网流量 —— **所属地域那个池子**的额度（非中国内地 200 GB / 中国内地 20 GB。
   两个池子独立：单台机器只能吃它所在地域的那一份）
2. **绝不超出**该额度

的系统。

## 非目标

- 不教绕过风控 / 人机校验。**遇滑块即停，不重试。**
- 不替代用户生成任何凭据。
- 不覆盖国际站（账号体系、计价、优惠规则均不同）。

---

## 〇、开工前：只读前置检查

**逐项断言，任一不满足即中止并告知用户。**
注意：**`UNVERIFIABLE`（查不到）一律等同 `FAIL`** —— 不要「跳过查不到的那项」。

| 检查 | 手段 | 通过条件 |
|---|---|---|
| 账号站点 | 让用户确认 | **中国站**（`aliyun.com`），不是国际站 |
| 实名认证 | 让用户确认 | 已完成（个人认证需身份证） |
| 账户余额 | BssOpenApi `QueryAccountBalance` | **可用余额 ≥ ¥100** —— 这是**持续**门槛不是入场费。⚠️ 该数值来自**控制台拦截提示**，帮助文档里没有这项门槛（详见 [`README.md`](README.md) 前置条件表） |
| 地域可用区 | ECS `DescribeZones` | 香港只有 `cn-hongkong-b/-c/-d`，**没有 `-a`** |
| 计费现状 | CDT 是否已升级为 CDT 计费 | 若未升级，**需要人工走控制台**（见第 2 步） |

> ⚠️ **`QueryAccountBalance` 不是 `DescribeAccountBalance`** —— 后者不存在。
> ⚠️ CDT 的 `GetCdtServiceStatus` / `ListSwitchedCdtProducts` 这两个名字**只出现在
> ActionTrail 的事件名表里**，**不能推出存在可调用的 OpenAPI**。
> 这是本仓库踩过的坑：从「审计事件名」推「有 API」是误读。

---

## 一、动作分级

**按「可逆性」而不是「难度」分级。分错级是这套方案最危险的失误。**

| 级别 | 动作 | 谁做 |
|---|---|---|
| **A 只读** | 查用量、查账单、查资源 | agent 全自动，可重试 |
| **B 可逆写** | 创建可随时删除的零成本资源 | agent 可做，**做完必须回读验证** |
| **C 一次性** | 领取 FC 新客试用 | **必须人工**，见 [`console/claim-fc-trial.md`](console/claim-fc-trial.md) |
| **D 身份类** | 注册、实名 | **纯人工** |
| **E 不可逆** | **升级至 CDT 计费**、购买包年包月 | **只能人工**，见 [`console/upgrade-cdt.md`](console/upgrade-cdt.md) |

> 🔴 **C 类是整条链路上唯一「错了就回不来」的动作之一** —— 官方规定
> 「同一用户仅能对同一产品申请试用一次」。**一个会重试的 agent 是在拿永久权益做试错。**
> 提示里写死：**最多一次，失败即停并上报。**

---

## 二、动线

### 第 1 步：资源创建

**原则：先 `describe` 判重，再 `create`。** 不依赖任何 state 文件。

> ⚠️ **`Tag` 是唯一可靠的身份锚点。** 资源名不是唯一的（共享带宽包的 `Name`
> 只是 0–128 字符自由字段，**没有任何唯一性约束**）。**每次创建都带 Tag，每次判重都按 Tag 查。**

**依赖顺序（照这个顺序，无环）。每一步的「怎么做」见
[`scripts/provision/README.md`](scripts/provision/README.md) 的对应小节：**

```
0a RAM 策略                          ← —              provision §3.1
0b RAM 角色 ×3                       ← 0a             provision §3.2
0c VPC                               ← —              provision §4.1
1a VSwitch                           ← 0c             provision §4.2
1b 安全组 + 规则                     ← 0c             provision §4.3
1c OSS Bucket                        ← —              provision §4.4
2  自定义镜像（build → import）      ← 1c, 0b         build-image.sh → import-image.sh
3  ECS（RunInstances + RamRoleName） ← 1a,1b,2,0b     provision §4.5
4a EIP                               ← —              provision §4.6
4b 共享带宽包                        ← —              create-cbwp.sh
5a EIP 加入共享带宽包                ← 4a, 4b         provision §4.7
5b EIP 绑定 ECS                      ← 4a, 3          provision §4.8
6  FC 函数（role = FC 执行角色）     ← 0b             provision §5.1
7  EventBridge 规则 + Target         ← 6              provision §5.2
8a FC 触发器（timer ×2）             ← 6              provision §5.3
8b FC 触发器（eventbridge）          ← 6, **7**       provision §5.3
9  部署 cdtguard.py + 起服务         ← 3, 1c, 0b      deploy-cdtguard.sh
10 端到端验收                        ← 全部           SKILL 第 5 步
```

**四件必须知道的事：**

- **RAM 不能排最后** —— `RunInstances` / `CreateFunction` / `ImportImage` 都用到角色。
  而 **`RunInstances --RamRoleName` 要填 `cdtguard-runtime`** —— 这个名字只在
  provision §3.2 里有出处。
- **OSS 必须在 ECS 之前** —— 守护的状态存 OSS，桶不存在就启动守护 = 失败时静默放行。
- **EventBridge 规则必须在 `eventbridge` 型触发器之前** —— 触发器要指向已有规则。
- 🔴 **FC 触发器一共 3 个，不是 2 个。** 除了每日巡检与状态变化，
  还有一个 **8 分钟周期的 `guard-liveness-8min`** —— 它是**唯一**能在护闸悄悄死掉时
  告警的机制。**别漏**；它也是 FC 成本的来源（见 provision §5.3）。

### 第 2 步（若需要）：升级至 CDT 计费

**⛔ 停下来，交给人工。** 这是**不可逆**的账号级变更。
路径与验收见 [`console/upgrade-cdt.md`](console/upgrade-cdt.md)。

### 第 3 步：领取 FC 新客试用

**⛔ 停下来，交给人工。** 一次性资格，**不可重试**。
路径与验收见 [`console/claim-fc-trial.md`](console/claim-fc-trial.md)。

### 第 4 步：部署守护

```bash
# 直接用仓库自带的脚本（它会把服务定义也装上、并在最后自检占位符）：
#     scripts/deploy-cdtguard.sh <本地 cdtguard.py> root@<你的 ECS IP>
#
# 下面这几步是它内部做的事，供你理解/手工执行：
# 1) 先装**服务定义**（⚠️ 漏了这步，后面 rc-service 会报 service not found）
scp scripts/cdtguard.initd root@<你的 ECS IP>:/etc/init.d/cdtguard
ssh root@<你的 ECS IP> 'chmod +x /etc/init.d/cdtguard && rc-update add cdtguard default'
#    并确认目标机有 python3（精简镜像默认不带）：command -v python3
# 2) 传到 ECS —— **传到 /root/cdtguard.py.new**，别直接覆盖线上那份
scp <本地 cdtguard.py> root@<你的 ECS IP>:/root/cdtguard.py.new
# 3) 服务器本地校验
python3 -m py_compile /root/cdtguard.py.new && echo COMPILE_OK
# 3.5) ⚠️ **占位符自检 —— 手工路径也要做这一步**
#      py_compile 看不见 <YOUR_...>：语法没问题，但部署上去之后
#      告警发不出、状态写不进去，而服务状态照样显示 started。
grep -n '<YOUR_' /root/cdtguard.py.new && echo 'X 上面这些占位符还没填 —— 先改掉再往下' || echo '占位符已填'
#      （用 deploy-cdtguard.sh 的话它自带这一步，且在覆盖之前就拦下）
# 4) 备份（带时间戳）
cp -p /root/cdtguard.py /root/cdtguard.py.bak.$(date +%Y%m%d-%H%M%S)
# 5) 覆盖并重启
cp /root/cdtguard.py.new /root/cdtguard.py && rc-service cdtguard restart
```

> ⚠️ **第 1 步不能省。** 服务定义不在 `cdtguard.py` 里，它是一个独立文件
> （`scripts/cdtguard.initd`）。少了它，`rc-service cdtguard restart` 会报
> `service not found` —— 读者看到的现象是「部署完了但服务起不来」。
> 用 `deploy-cdtguard.sh` 的话它已包含这一步（STEP 1.5）。

**验收标准（不要用「端口在听」「ERR 计数为 0」这类指标）**：

| 验收项 | 方法 | 通过条件 |
|---|---|---|
| 进程在跑 | `ps aux \| grep '[c]dtguard.py'` | 有 python 进程 |
| **在采样** | 状态文件的 mtime | **每 2 秒左右变一次**（写盘每轮都做；60 秒是 fsync 与 OSS 同步的周期）。**mtime 长时间不动 = 真的死了** |
| 凭据取到 | 日志里 `凭据来源：实例 RAM 角色` | **必须是这一句**。若是 `凭据来源：内嵌值/环境变量` 就说明没取到角色凭据 —— 此时只有你手写进文件的 AK/SK 有效 |
| API 通 | 启动时看 `cdt_floor(init)=…GB` 与 `bw_month(init)=…GB`（**注意带 `(init)`**；裸形式 10 分钟后才出现）；若看到 `bw init ERR` 说明 `BW_ID` 填错了 | 两个 init 行都有值，且**没有** `bw init ERR` —— 那代表混合口径已静默降级（见 `deploy-cdtguard.sh` STEP 4.5） |
| **OSS 通**（最硬的一条） | 先把脚本传上去：`scp -q scripts/check-state.py root@<你的 ECS IP>:/root/`，再在实例上 `python3 /root/check-state.py` —— 它用实例 RAM 角色**独立读回**状态对象 | 读得到，且里面的 **`ts` 是近几分钟的**、`month` 是本月。⚠️ 这一步**不经过守护**，所以它能识破「守护已经死了但 rc-service 仍显示 started」 |

> 🔴 **不要用「`:443` 在监听」当验收** —— 那测的是另一个进程，恒真。
> **不要用「日志里 ERR=0」当验收** —— 死掉的守护不写日志，恒为 0。
> **能通过一个已经死掉的守护的标准不是标准。**

### 第 4.5 步：运维动线 —— 只换一个值时怎么改

**换 `PUSH_TOKEN`（换网关账号 / token 失效）是最常见的一次运维。**
它有两个落点，**两个都要改**，而且是两套机制：

| 落点 | 在哪 | 怎么改 |
|---|---|---|
| **ECS 守护** | 实例上 `/root/cdtguard.py` 的 `PUSH_TOKEN` 那一行 | **改文件**（环境变量送不进去，见下） |
| **FC 兜底** | FC 函数的 `PUSH_TOKEN` **环境变量** | **改环境变量**（不是文件） |

> ⚠️⚠️ **为什么 ECS 侧必须改文件、不能 export**：
> 守护由 `supervise-daemon` 拉起，它**会清空子进程环境** ——
> `export`、`.env`、`/etc/conf.d/cdtguard` 全都送不进去。
> 改完必须**重启服务**才生效（`PUSH_TOKEN` 是模块级常量，进程启动时读一次）。

**ECS 侧：一条命令替换 + 校验 + 重启**（在实例上以 root 执行）：

```sh
cd /root
cp -p cdtguard.py cdtguard.py.bak.$(date +%Y%m%d-%H%M%S)   # 先备份

# ⚠️ 用 python 读 token 而不是写进命令行 —— 避免它落进 shell history / ps 输出
read -r -p "新 PUSH_TOKEN: " TOK
python3 - "$TOK" <<'PY'
import io, re, sys
tok = sys.argv[1]
assert re.fullmatch(r"[0-9a-fA-F]{32}", tok), "token 形态不对（pushplus 是 32 位十六进制）"
p = "/root/cdtguard.py"
s = io.open(p, encoding="utf-8").read()
# 行首锚定：只改模块级那一行，不会碰注释与函数体里的引用
new, n = re.subn(r'^PUSH_TOKEN = .*$',
                 'PUSH_TOKEN = os.environ.get("PUSH_TOKEN", "%s")' % tok,
                 s, count=1, flags=re.M)
assert n == 1, "没找到唯一的 PUSH_TOKEN 赋值行（命中 %d 次）—— 别用 sed，先人工看一眼" % n
io.open(p, "w", encoding="utf-8", newline="").write(new)
print("OK 已替换")
PY

python3 -m py_compile cdtguard.py && echo COMPILE_OK     # 语法不过就别重启
grep -c '<YOUR_' cdtguard.py                              # 应为 0
rc-service cdtguard restart
sleep 3 && tail -5 /var/log/cdtguard.log                  # 看有没有 push[...] -> {"code":200}
unset TOK
```

> 🔴 **验证只看一个信号：日志里 `push[CDT 守护已启动] -> {"code":200,...}`。**
> 守护**每次启动都会推一条**（`main()` 开头那次 `rpt("CDT 守护已启动", …)`），
> 所以重启后 tail 一下就能确认新 token 是活的。
> **`rc-service status` 显示 started 不能证明推送能发出去** —— 那正是本仓库反复强调的假信号。
>
> ⚠️ **HTTP 200 ≠ 推送成功。** pushplus 的业务错误码在响应体里：
> **未实名认证会返回 `905`**（见 [`docs/toolchain.md`](docs/toolchain.md) 第二节）；
> token 写错一般是 `401`/`403` 一类。**必须看 `code` 字段，不能只看有没有异常。**
>
> 💡 **想立刻单独验一次、不重启守护**：在实例上直接怼一次网关即可 ——
> `python3 -c 'import json,urllib.request,sys;b=json.dumps({"token":sys.argv[1],"title":"test","content":"ok","template":"txt","channel":"wechat"}).encode();print(urllib.request.urlopen(urllib.request.Request("https://www.pushplus.plus/send",data=b,headers={"Content-Type":"application/json"},method="POST"),timeout=10).read().decode())' '<新token>'`

**FC 侧：改环境变量，走部署脚本（别手点控制台）**

`environment_variables` 是**整体覆盖**语义 —— 控制台上少填一个键就是删掉一个键。
所以用仓库里的脚本，它是「Get 读回 → 原样合并 → 写回」：

```sh
python3 scripts/deploy-fc.py scripts/fc-keeper/index.js --dry   # 先预检：读回 12 个键、必需键齐全
# 改本地环境里的 PUSH_TOKEN，或直接改 FC 控制台里的那个键，然后正式部署：
python3 scripts/deploy-fc.py scripts/fc-keeper/index.js         # 部署后会自动调用一次并断言 ver
```

> ⚠️ **FC 侧的 `PUSH_TOKEN` 缺失 = 函数直接抛错拒绝启动**（`requireEnv()`），
> 不像 ECS 侧那样能降级。所以改这一侧时**别把键删了**，只换值。
>
> 📌 **换 token 不会影响停机**：`PUSH_TOKEN` 只被 `notify()` 用，
> 停机路径（`do_stop()`）不依赖它，推送失败也只记一行日志、不抛。
> **但「静默失效」正是本仓库最不想要的失败模式** —— 换完一定要按上面的 `code:200` 验一次。

### 第 5 步：端到端验收

**没有这一步，整套系统只是「看起来在跑」。** 但**必须按下面两个子步骤安全地做** ——
直接「压低阈值跑一遍」是会出事的（见 5b 的红字）。

#### 5a. 先自检：Deny 策略有没有把守护的**停机路径**打死

这是最阴的一类故障：`policies/cdtguard-runtime-policy.json` 若把 `ecs:StopInstance`
拒了，守护**照样每 2 秒正常采样、日志一片祥和，但真要停机时停不掉**。

用阿里云原生的 **`DryRun`** 验证 —— **零副作用、零成本**：

| 调用 | 是否在 Allow 列表里 | 期望 |
|---|---|---|
| `StopInstance` + `DryRun=true` | ✅ 是 | `DryRunOperation` |
| `RebootInstance` + `DryRun=true` | ❌ 否 | `Forbidden.RAM` |

**【实测】** 在本方案上跑出的真实结果：

```
StopInstance    (在 Allow 里)  -> HTTP 400  Code=DryRunOperation
RebootInstance  (不在 Allow 里) -> HTTP 403  Code=Forbidden.RAM
```

**负对照是关键**：它证明**授权先于 DryRun 校验生效** —— 所以
`DryRunOperation` 确实蕴含「授权已通过」，而不是「这条路径根本没查权限」。

> ⚠️ **别拿别的接口做这个测试。** `DryRun` 只保证「不停机」，不保证「无副作用」；
> 而 `CreateCommonBandwidthPackage` 之类**根本没有 `DryRun`** —— 调用即创建。
> 「不足 1 小时按 1 小时」这条就是这么触发的，见 [`docs/pitfalls.md`](docs/pitfalls.md) 第 3 条。

#### 5b. 再演练：确认守护**真的会拦**

用守护自带的 **`--dry-run`** —— 它走 `StopInstance` 的 `DryRun`，
**真实 API 预检、会校验身份与权限，但不会真停实例**。

**守护自己挡住了「停机锁」的三个写入点 —— 但演练仍会写别的东西，落点要换。**

`--dry-run` **不会**打 `cdt-halted-month` 标签、**不会**把 `halted=1` 写进 OSS 对象 ——
「停机锁」相关的三处（实例 tag、`st["halted"]`、停机前那笔 `oss_put` 里的 `halted` 字段）
都被 `DRY_RUN` 挡着。所以**不需要**再换标签键或 OSS 的 `halted` 语义。

> ⚠️⚠️ **但「挡住了停机锁」≠「演练无副作用」。三处仍然会被写：**
>
> 1. **本地状态文件**：默认 `/var/lib/cdtguard/state.json` 与 `/root/.cdtguard.state`，
>    里面带着**一次性告警标志**。不止 `warned` / `thr_alerted`，还有
>    `rollover_hold_alerted`、`bw_alert_ts`、`preempt_alerted` ——
>    **共五个**。演练若在真守护重启的那个窗口里写了它们，
>    对应的告警会被静默吞掉（跨月「重置未确认」那条尤其要紧）。
> 2. **OSS 上的状态对象**：周期写入（每约 60 秒那笔）**完全不看 `DRY_RUN`** ——
>    它照常把 `threshold_gb` / `eff` / `ts` / `month` 写进**真实的**共享对象。
>    而 FC 会读 `threshold_gb` 当告警阈值（`fc-keeper/index.js` 里那行
>    `if (st.threshold_gb) THRESHOLD_GB = st.threshold_gb;`）。
>    所以**演练压低阈值时，FC 侧看到的是演练阈值**。
> 3. 由此 `ts` 也被刷新 ⇒ 演练期间 FC 的「护闸失联」判断会被掩盖。
>
> **结论：演练仍应按「会污染共享状态」来对待** —— 跑完把阈值与状态恢复原样，
> 别在真守护的重启窗口里做。下面的 sed 只换本地状态文件，**不是全部。**

**必要的一步：把本地状态文件换到 `/tmp`。**

```sh
cp -p /root/cdtguard.py /root/cdtguard.drill.py      # 只动副本，不碰线上那份
sed -i 's|^STATE_A = .*|STATE_A = "/tmp/cdtguard.drill.state"|'  /root/cdtguard.drill.py
sed -i 's|^STATE_B = .*|STATE_B = "/tmp/cdtguard.drill2.state"|' /root/cdtguard.drill.py
python3 -m py_compile /root/cdtguard.drill.py
```

> ✅ **「停机锁」那三处为什么值得信任**：它们**设计上**被挡在 `DRY_RUN` 之外，
> 不是靠事后清理 —— 即使你忘了收尾，实例也不会被钉到月底（那是演练最严重的后果）。
> 你改停机块时，**记得三处一起看**（tag / 内存标志 / 停机前那笔 OSS 写）。
>
> ⚠️ 但**别再把它推广成「演练整体无副作用」** —— 上面那三条就是反例。
> 判据是：**「会不会改变线上可用性」**，而不是「是不是在停机块里」。

**压阈值不要改文件 —— 手动运行时用环境变量。**

> ⚠️ **环境变量只在「手动运行」时有效**：手动起的进程会继承你的 shell 环境。
>    而**被 `supervise-daemon` 托管的那个进程收不到任何环境变量**
>    （它启动时会清空子进程环境，见 `scripts/cdtguard.initd`）。
>    所以：演练用环境变量；**改线上正式运行的阈值必须改文件**。

```sh
cd /root && TRAFFIC_THRESHOLD_GB=1.0 timeout 150 python3 -u /root/cdtguard.drill.py --dry-run > /root/drill.out 2>&1
cat /root/drill.out
```

> ⚠️ **为什么用环境变量而不是 `sed` 改 `THRESHOLD_GB`** —— 两个真实原因：
> 1. 那行**不保证是单行**。当前实现是
>    `THRESHOLD_GB = (float(THRESHOLD_ENV) if THRESHOLD_ENV` ⏎ `else free_quota_gb(...))`，
>    用 `sed 's/^THRESHOLD_GB = .*/…/'` 只会替换第一行，**第二行变成悬空缩进 ⇒
>    `IndentationError`**，`py_compile` 直接失败。
> 2. 即使改成功，守护启动时 `apply_region()` 会**按实测地域把阈值重算回去**
>    （未设 `TRAFFIC_THRESHOLD_GB` 时）—— 香港会算成 200，演练根本不会触发。
>    **而设了环境变量，这两条都不发生**：它优先，且 `apply_region()` 会直接返回。

**【实测】** 在真实环境跑通的输出（当时用的是改文件的写法；输出本身有效）：

```
凭据来源：实例 RAM 角色，有效至 <YYYY-MM-DD hh:mm>
guard start iface=eth0 threshold=1.0GB
⚠️ DRY_RUN 已启用：停机只做 DryRun 预检，不会真的停实例
push[CDT 守护已启动] -> {"code":200,...}          ← 推送链路通
bw_month(init)=NN.NNNNGB (计费侧主流量)
push[CDT 流量达阈值（1 GB）] -> {"code":200,...}   ← 阈值告警发得出去
THRESHOLD HIT eff=NN.NNNG -> STOP
DRY_RUN 模式：跳过 tag_halted（不打真实停机锁）
oss halted=0 written BEFORE stop          ← ⚠️ 必须是 0，写 1 就是谎
DRY_RUN 预检通过（DryRunOperation），实例未停 —— 链路可达、身份有效、权限齐备
```

> 🔴 **看这两行的「值」，不要只看有没有这行**：
> 演练里应当出现 **`跳过 tag_halted`** 与 **`oss halted=0`**。
> 若看到 `tagged cdt-halted-month=…` 或 `oss halted=1` ——
> 说明 `DRY_RUN` 的挡板被改坏了，**演练已经污染了真实停机判据**，要立刻处理。
> （这两条日志曾经就是在演练下说谎的：上面那句「跳过」没打、下面那句硬编码 1。
> 现在它们按实际值输出 —— 判据是「**日志说的与代码做的必须一致**」。）

**这一条日志把整条链路的每一环都验到了**：凭据 → 身份自识别 → 混合口径 → 推送 →
阈值命中 → 打标签 → OSS 写入 → **停机预检通过**。而**实例始终保持 Running**。

**这就是 DRY_RUN 生效的直接证据**：代码路径**完整走完**了（含 `StopInstance` 的
DryRun 预检），而真实停机判据**一个都没被碰** —— 没有 tag、OSS 里的 `halted` 仍是 0。

**收尾：清进程 + 删文件 + 恢复被碰过的共享状态。**

⚠️ **「不需要回滚 OSS」只对停机锁成立**：`cdt-halted-month` 标签与 OSS 里的
`halted` 字段**从没被写过**，所以不用去解绑标签、也不用去回滚那个字段。
**但周期写入确实碰过 OSS**（见本节前面那三条）—— 阈值必须改回原值，
否则 FC 会拿着演练阈值继续判断。**别把这两件事混成一句「演练无副作用」。**

```sh
# 1) 清进程
ps -o pid,args | awk '/cdtguard[.]drill/ {print $1}' | while read p; do kill -9 $p; done
# 2) 删文件（本地状态落在 /tmp，一并删掉）
rm -f /root/cdtguard.drill.py /root/drill.out /tmp/cdtguard.drill.state /tmp/cdtguard.drill2.state
# 3) 事后核验：实例上**不应有** cdt-halted-month 标签，只剩系统标签
#    （若真出现了 —— 说明 DRY_RUN 挡写入那几处被改坏了，那才是要立刻处理的）
# 4) 恢复被演练改动的共享状态：把阈值改回原值，并确认 OSS 状态对象的
#    threshold_gb 已随真守护的下一轮写入回到正常值（周期写入不看 DRY_RUN）
```

> ⚠️ **别用 `pkill -f 'cdtguard.drill'` 清进程** —— 你这条 SSH 命令的**命令行里就含**
> 那个字符串，`pkill -f` 会把**它自己**也匹配杀了（实测踩到，命令被 SIGTERM 掉、
> 拿不到任何输出）。用上面那种按 PID 杀的方式。

> 🔴 **为什么必须用 `--dry-run`，而不是「只是把阈值压低跑一遍」：**
> 真停机演练有个**恶名昭著的死锁** —— 实例一被停，SSH 就断了，
> 而阈值还压着、再也改不回来，只能靠往 OSS 写「月份不符」的诱饵对象来解锁。
> `--dry-run` 让实例根本不停，**这个死锁不存在**。
>
> ⚠️ **停机锁那三处「设计上被挡住」比「事后清理」可靠得多。** 演练根本不去写它们
> （tag / `st["halted"]` / 停机前那笔 OSS 的 `halted` 字段），所以**即使你忘了收尾，
> 真实的停机判据也从未被污染**。事后核验时真实的 `cdt-halted-month` **根本没被设置过**。
>
> ⚠️ 但这**只覆盖停机锁**。本地状态文件与**周期性** OSS 写入仍会被演练改动
> （见本节上面的三条），所以第 4 步的恢复动作不能省。
>
> 📌 **两个实测出来的坑：**
> - **`log()` 走的是 `print()` → stdout**，日志文件是靠 supervise-daemon 重定向填充的。
>   所以手动跑演练**必须自己重定向**（`> /root/drill.out 2>&1`）。
>   不重定向的话输出进 SSH 管道，**日志文件里什么都看不到** ——
>   而标签却已经打上了，很容易误判成「演练没跑起来」。第一次就这么被骗了。
> - **首次采样很快**：从启动到 `THRESHOLD HIT` 只用了约 2 秒；但命中后代码会
>   `sleep(600)` 退避，所以用 `timeout 150` 收尾即可，不会挂住。

---

## 三、不可变清单（任何情况下不许做）

这些是本方案的安全边界，**不是建议**：

1. **阈值只能下调，不能上调。** 200 GB 等于免费额度本身，**余量为 0**。
2. **运行时 RAM 策略只能收窄，不能放宽。**
3. **护闸组件不得删除** —— `cdtguard` 服务、FC 函数、EventBridge 规则、告警通道。
   一个「清理未使用资源」的 agent 很容易把它们当垃圾删掉。
4. **任何情况下不得在实例上放置长期 AccessKey。** 用实例 RAM 角色。
5. **不得创建按带宽计费的共享带宽包**，也不得修改带宽峰值
   （计费取**当日最大带宽峰值**，当天调高**全天按高的收**）。
6. **不得为了放大额度而多开账号。** 阿里云对「同一用户」的判定覆盖
   「同一证件、同一手机号、同一支付账号、同一设备、同一登录环境」。

---

## 四、成本红线

| 动作 | 成本 |
|---|---|
| 创建按带宽计费的共享带宽包 | **不足 1 小时按 1 小时**，且**没有 `DryRun`** |
| 创建按增强型 95 计费的共享带宽包 | **至少持有 1 个自然月，期间无法删除** |
| 给 FC 加一个「小于 1 小时」周期的定时器 | 该小时就算「有调用」= ¥0.01，**与调用量无关** |
| 解绑闲置 EIP | **立刻开始产生保有费**（绑着反而通常免费） |

**任何计费写操作前先跑一次报价**（`aliyun` CLI 自带 `--estimate-cost`，MCP 的 `callcli` 转发 CLI 命令因而也有；
只报价不执行）。看到 `NOT_ESTIMATED` **不要当成免费**。

---

## 五、参考资料

- [`docs/billing.md`](docs/billing.md) —— 220 GB 怎么分、怎么算，含 CDT 2.0 的账单字段变更
- [`docs/pitfalls.md`](docs/pitfalls.md) —— 29 条踩坑清单，每条附证据等级
- [`docs/proxy-interface.md`](docs/proxy-interface.md) —— 代理侧必须遵守的三条硬约定
  （**agent 不要照它去搭代理** —— 它只写「守护对代理有什么要求」）
- [`docs/toolchain.md`](docs/toolchain.md) —— 本教程用到的工具与 MCP 服务：
  地址、装法、最小用法，以及**哪些是必需的、哪些可省**
- [`policies/`](policies/) —— 运行时 RAM 策略（最小权限 + 显式 Deny）
- [`scripts/`](scripts/) —— 守护、FC 兜底、部署脚本
