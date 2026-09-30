# 工具链：本教程用到的全部工具与服务

> **先说结论：本教程不依赖任何 MCP。**
> 仓库脚本走的是**普通命令行工具**（`aliyun` CLI、`ssh`/`scp`、`python3`）。
> MCP 服务是**让 AI agent 更顺手的封装** —— 装不上也能照本仓库做完。
>
> 但文档里会提到它们（「用 `callcli` 看成本预估」「agent 不得用 Playwright 自动化那两步」），
> 所以这里把**地址、安装、最小用法、必需/可选**一次列全。

---

## 一、必需：命令行工具

| 工具 | 装法 | 用在哪 |
|---|---|---|
| **`aliyun` CLI** | <https://github.com/aliyun/aliyun-cli> — 也可从[官方文档](https://help.aliyun.com/zh/cli/)装 | 建资源、查用量、查账单（`scripts/provision/create-cbwp.sh`） |
| **`ssh` / `scp`** | 系统自带 | 部署守护（`scripts/deploy-cdtguard.sh`） |
| **`python3`** | 系统自带 | 守护本体、`import-image.sh` 的静态检查 |
| **`node` / `npm`** | <https://nodejs.org> | 本地跑 `scripts/fc-keeper/index.js` 的桩测试；装 Playwright MCP |

配置 `aliyun` CLI：

```bash
aliyun configure          # 交互式填 AK/SK/地域
aliyun configure list     # 看当前 profile
```

---

## 二、运行期依赖：推送通道（**必需**）

守护本身不发消息 —— 它把告警 POST 到一个 **HTTP 推送网关**。
`cdtguard.py` 和 `fc-keeper/index.js` 都通过 `PUSH_TOKEN` 调它，
内容形如 `http://www.pushplus.plus/send?token=<TOKEN>&title=...&content=...`。

| 项 | 说明 |
|---|---|
| 项目 | [PushPlus](http://www.pushplus.plus/) —— 国内的免费推送网关，微信公众号收消息 |
| 是否必需 | **是。** 这是唯一的告警出口 —— 没有它，停机仍会执行，但你**不会被告知** |
| 费用 | 免费额度足够本方案（每天个位数消息）—— **但前提是已实名**，见下面的红字 |
| 注册与取 token | 微信扫码登录 → **先完成实名认证** → 「一对一推送」页复制 `token`（32 位十六进制）|
| 填在哪 | `cdtguard.py` 的 `PUSH_TOKEN` 与 FC 的 `PUSH_TOKEN` 环境变量。**两处都要** |
| 换别的 | 任何接受 `?token=&title=&content=` 的网关都行 —— 改 `notify()` / `push()` 里的 URL 即可 |

> 🔴 **必须先实名认证，否则接口直接返回错误码 905。**
> 自 **2024-08-01** 起 PushPlus 实行实名制：**未实名的账号调用「发送消息」接口一律失败**
> （`905`），只有接收消息不受影响。**API 调用也属于「发送消息」**，所以本方案的推送同样用不了。
>
> ⚠️ **实名认证要付费**（第三方收取，或开通一个月会员免认证费）。
> 未实名时微信渠道**日额度为 0**；实名后 200 条/天，会员 2000 条/天 ——
> 本方案每天只有个位数消息，实名后绰绰有余。
>
> **为什么这条值得单独标红**：推送通道是**单点依赖**。少了它，守护照跑、
> 到阈值照样停机，但**你永远收不到通知** —— 而 `deploy-cdtguard.sh` 的占位符检查
> 只能验证「token 填了」，验证不了「token 还能用」。**换网关或换账号后，务必手动跑一次推送验证。**

> ⚠️ `scripts/deploy-cdtguard.sh` 的 STEP 4.5 把 `PUSH_TOKEN` 列为**致命占位符**
> （没填就 `exit 1`），原因就是上面那条：它是唯一出口，静默失效等于没装告警。

---

## 三、构建与部署期工具

只在**建自定义镜像**与**上传**时用到：

| 工具 | 装法 | 用在哪 |
|---|---|---|
| **`ossutil`** | <https://help.aliyun.com/zh/oss/developer-reference/ossutil> | 上传 raw 镜像到 OSS（`scripts/import-image.sh`） |
| `dd` `sfdisk` `losetup` `mkfs.ext4` `mount` `chroot` | 发行版包管理器 | `scripts/build-image.sh` |
| **`extlinux` / `syslinux`** | Debian: `apt install syslinux extlinux`；Alpine: `apk add syslinux` | 同上（写引导） |
| `tar` `wget`/`curl` | 系统自带 | 同上 |

> ⚠️ **`build-image.sh` 的 Alpine 版本是个会过期的默认值。**
> 脚本里写死了某个小版本（当前默认值见脚本头部注释），但 Alpine 每半年出一个稳定分支、
> 定期发补丁、并按分支给 EOL 日期。**动手前先去
> <https://alpinelinux.org/releases/> 看当前稳定分支是哪个**，
> 或者直接把版本当第二个参数传进去。用 EOL 分支建镜像不会立刻报错 ——
> 它只是再也收不到安全更新。

> ⚠️ **`ossutil` 不读 `ALIYUN_AK`/`ALIYUN_SK` 环境变量。**
> 它的凭据入口只有三处：配置文件（`ossutil config`）、
> 命令行 `-i/--access-key-id` + `-k/--access-key-secret`、
> 以及 RAM 角色模式（`--mode RamRoleArn` / `EcsRamRole`）。
> `import-image.sh` 里用的是命令行方式。
>
> 官方同时提醒：**命令行传密钥可能被日志系统记录**。在意就用配置文件。

---

## 四、可选：MCP 服务

### 4.1 阿里云 OpenAPI MCP Server

**用途**：让 agent 直接调阿里云 OpenAPI，**凭据由服务端注入、不必落地**。

| | |
|---|---|
| 仓库 | <https://github.com/aliyun/alibabacloud-api-mcp-server> |
| 文档 | <https://help.aliyun.com/zh/openapi/user-guide/openapi-mcp-server-core-tool-guide> |
| 入口 | <https://api.aliyun.com> |

**【官方】** 常用工具（Core 版）。⚠️ **下表是节选** —— 官方说 Core 版有 **15 个**工具，
这里只列与本文档相关的那些（其余如 `GetTask` / `GetPresignedUrl` / `GetDocumentTree`
等按需去官方页面查）：

| 工具 | 作用 |
|---|---|
| `SearchApis` | 自然语言检索需求对应的 API |
| `GetApiDefinition` | 按 API 三元组查定义（入参/出参/错误码） |
| `ListApis` / `ListProducts` / `ListProductRegions` | 列产品、列 API、列地域 |
| `GenerateCLICommand` | **生成** CLI 命令（不执行） |
| `CallCLI` | **执行** CLI 命令 |
| `RunScript` | 执行 Python 脚本（多 API 编排） |
| `RunIaC` | 执行 Terraform HCL |
| `SearchDocuments` / `GetDocument` / `GrepDocuments` | 检索阿里云文档 |

**为什么提它**：`CallCLI` 在**计费写操作**前**据称**会返回一份报价 JSON
（含 `priceSummary.effectiveModuleSum`）。
⚠️ **这个字段名在官方 Core 版工具文档里查不到** —— 和 `costPreview` 一样属于
**无公开出处**，只能当线索，**不要写进任何断言或校验代码**。
**同一能力 `aliyun` CLI 也有**（`--estimate-cost`），见下文第六节。
资源创建的动线见 [`../scripts/provision/README.md`](../scripts/provision/README.md)。

> ⚠️ **但别把它当护栏。** 按量资源**创建时刻**的预估价接近 ¥0
> （共享带宽包的费用公式含「当日最大带宽峰值」，创建时估不出来）。
> **它防不了「不足 1 小时按 1 小时」。** 真正防它的是「不建按带宽计费的包」这条硬规则 ——
> 见 [`pitfalls.md`](pitfalls.md) 第 1、3 条。

> ⚠️ **`CallCLI` 在远程服务器上执行，读不到你的本地文件。**
> 这条是官方明写的，也是本仓库「没有文件传输通道」那个结论的由来。

### 4.2 ssh-mcp-server

**用途**：通过标准接口在远端执行 SSH、上传下载文件，**不必把 SSH 凭据暴露给模型**。

| | |
|---|---|
| 仓库 | <https://github.com/classfang/ssh-mcp-server> |
| npm | `@fangjunjie/ssh-mcp-server` |

**工具**：`execute-command`、`upload`、`download`、`list-servers`。

**最小用法**：在 MCP 客户端配置里挂上它，然后
`execute-command(cmdString="rc-service cdtguard status", connectionName="<你的连接名>")`。

> 🔴 **两个坑**：
> 1. **`connectionName` 不传会落到配置里的「第一个」机器。** 当你配了多台时，
>    报错看起来会像「服务没跑」，而其实你查的是另一台。**始终显式写 `connectionName`。**
> 2. **`timeout` 单位是毫秒。** 传 `30` 会被理解成 30 毫秒，报错里会说 "within 30ms"。
>
> ⚠️ `upload`/`download` 用的是**远端 POSIX 路径**。两台机器之间没有共享文件系统时，
> **别指望用它做「本地 ↔ 远端」的文件比对** —— `tokhash.py` 要求两个文件同在本机
> （见其文件头）。

### 4.3 Playwright MCP

**用途**：驱动真实浏览器。本仓库只在**控制台那两步**用到它，且**用得很克制**。

| | |
|---|---|
| 仓库 | <https://github.com/microsoft/playwright-mcp> |
| npm | `@playwright/mcp` |
| 文档 | <https://playwright.dev/docs/getting-started-mcp> |

> 📌 **官方现在对「编码 agent」另推了一个 CLI 版**（<https://github.com/microsoft/playwright-cli>）。
> 本仓库仍按 MCP 写，因为控制台那两步要的是「有头浏览器 + 复用登录态」，
> MCP 这条路已经够用；但你知道有另一个选项即可。

```bash
claude mcp add playwright npx @playwright/mcp@latest     # Claude Code
```

其他客户端用标准配置：

```json
{ "mcpServers": { "playwright": { "command": "npx", "args": ["@playwright/mcp@latest"] } } }
```

**两条硬约束**：

1. 🔴 **`console/` 下那两步不可用 Playwright 执行。** 前者（升级至 CDT 计费）
   **不可逆且影响账号上所有按流量业务**；后者（领 FC 新试用）是**一次性资格**。
   **agent 最多导航到位后停下。** 见顶部 PAUSE 标记。
2. ⚠️ **它会把你已登录页面的无障碍树落到磁盘**，其中含**账号 UID**。
   写进 `.gitignore`；截图只截**元素**、不截整页。见 [`pitfalls.md`](pitfalls.md) 第 29 条。

---

## 五、开发期工具（只在你改本仓库时才需要）

| 工具 | 装法 | 用在哪 |
|---|---|---|
| **`pre-commit`** | `pip install pre-commit && pre-commit install` | `.pre-commit-config.yaml` |
| **`gitleaks`** | <https://github.com/gitleaks/gitleaks> | 同上（提交前扫凭据） |

---

## 六、不用 MCP 行不行

**行。** 逐个对照：

| 要做什么 | 不用 MCP 的做法 |
|---|---|
| 建资源 | `aliyun` CLI（`scripts/provision/create-cbwp.sh` 就是这么写的） |
| 查用量/账单 | `aliyun` CLI，或 CDT 控制台 |
| 部署守护 | 普通 `ssh` + `scp`（`scripts/deploy-cdtguard.sh` 就是这么写的） |
| 控制台那两步 | **本来就只能人工**，与 MCP 无关 |
| 看成本预估 | ✅ **`aliyun` CLI 自带**（`--estimate-cost`，只报价不执行）。见 <https://help.aliyun.com/zh/cli/> |

**成本预估不是 MCP 独有的** —— `aliyun` CLI 自带 `--estimate-cost`（只报价不执行），
还有 `aliyun utils list-supported-pricing-apis` 列出支持询价的接口。
**所以本教程用普通 CLI 就能完整走完，一处都不缺。**

> ⚠️ **版本号与子命令名请以你这台机器上的 `aliyun --help` 为准。**
> 本仓库不写死「从哪个版本起支持」这类说法 —— 那类断言没有稳定出处，
> 而且会随 CLI 版本失效。要确认能力在不在，**直接跑一次 `aliyun --help` 看有没有这个 flag**。
> （上面那个子命令也属于此类：它的位置变过，**以 `--help` 里实际列出的路径为准**。）

> 📌 如果你就是不用 MCP：那更要守住 [`billing.md`](billing.md) 第七节那条 ——
> **真正防误建烧钱的是余额上限，不是任何预估接口。**
