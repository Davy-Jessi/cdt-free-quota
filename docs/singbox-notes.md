# sing-box 配置说明

> ⚠️ **本文里的版本断言以 2026-09-30 为界**：sing-box 最新正式版是 **1.14.2**
> （1.15 仍在 alpha）。下面每处「某版本起 / 某版本将移除」都写明了依据；
> **你要照做时，先对着自己的版本核一遍** —— 这类断言天然会过期。<br>
> 权威来源是官方的 [Deprecated](https://sing-box.sagernet.org/deprecated/)
> 与 [Migration](https://sing-box.sagernet.org/migration/) 两页，本文只挑与配置相关的。
>
> 与 [`../scripts/singbox.config.example.json`](../scripts/singbox.config.example.json) 配套：
> 原文件的内联注释，移出以保持 `.json` 是合法 JSON。
>
> ⚠️ **示例配置是「通用最小可用」的** —— 只有一个 VLESS+REALITY 入口、
> 一个 `direct` 出站、以及 DNS 劫持。**本仓库不包含任何分流设计**
> （不给特定服务指定出口、不预设规则集）—— 那是你自己的选择。
>
> **关于本文档里「实测」的边界：** 下面这些坑都是在一套**真实部署**上踩出来的，
> 所以会带上具体的数字（内存、日志速率、二进制体积等）。**别把它们当成对你自己
> 环境的预言** —— 它们的作用是告诉你「有这一类坑，而且它的表象是这样的」。
> 具体到你的机器，请自己量一遍。
>
> **本文档写的是 sing-box 自身的字段语义与踩坑**，包括它各个 endpoint/outbound
> 类型的通用行为（含第六节那个 `tailscale` endpoint）——
> 这些是**工具知识**。
> **它不写、也不推荐「你该用哪种组网方案」**：出口走哪个网络、用哪家云、
> 伪装成哪个域名、给哪些服务分流，都属于「部署环境特有的选择」
> （见 [`pre-commit-checklist.md`](pre-commit-checklist.md) §5），**由你自己定**。

```
# ═══════════════════════════════════════════════════════════════════════
#  部署位置：<你的 ECS 公网 IP 或 EIP> 的 /etc/sing-box/config.json
#  服务    ：OpenRC 服务 sing-box（见文末定义）
#  详见    ：../docs/proxy-interface.md（守护对代理侧的三条硬约定）
# ═══════════════════════════════════════════════════════════════════════
#
# ── 一、DNS 劫持（sniff / hijack-dns / local dns）────────────────────
#  目的：从 inbound 进去的 DNS 流量，不再放给客户端指定的那台服务器，
#        而是被 sing-box 就地截获、交给本机解析器解析后直接应答。
#
#  ⚠️ 语义在 1.11 变过。**而且旧写法后来被硬删了 —— 现在是直接起不来，不是静默失效：**
#     * 1.11 之前：sniff 是 **inbound 的字段** —— "sniff": true,
#                  "sniff_timeout": "300ms", "domain_strategy": ...
#     * 1.11 之后：这些 inbound 字段废弃，改成 **route 规则动作**。
#                  官方迁移写法就是示例里那两条：
#                      { "action": "sniff" }                        ← 非终结动作
#                      { "protocol": "dns", "action": "hijack-dns" } ← 终结动作
#                  ⚠️ 旧字段在 **1.13 被移除** ⇒ 现在仍照着老教程写，是
#                     **启动直接 FATAL**（不再是「只告警、行为静默改变」）。
#     * 1.12 之后：dns.servers 从「字符串地址」改为「带 type 的类型化对象」。
#                  所以是示例里的 { "type": "local", "tag": "local-dns" }，
#                  而不是老的 "address": "local"。
#                  ⚠️ 旧格式在 **1.14 被移除** ⇒ 同样是启动 FATAL。
#
#  local 的含义：用 **/etc/resolv.conf 里的系统解析器**。
#  dns.final 留空时默认取 servers 里的第一个，所以单 server 场景不必写。
#  ⚠️ independent_cache 在 1.14 已废弃，别加。
#
#  注意边界：这条链路只在**客户端把 DNS 也送进隧道**时才起作用。
#
# ── 二、为什么 Reality 的伪装目标要挑 ────────────────────────────────
#  **这一条是通用的，与具体选哪个域名无关。**
#
#  Reality 握手时会读取伪装目标的 TLS Certificate 记录，并且**给它设了一个长度上限**：
#      handshakeLen = recordHeaderLen + Value(s2cSaved[3:5]...)
#      if handshakeLen > size { break f }      // 超了就放弃这条握手
#
#  🔴⚠️ **这个 `size` 是变过的，别照抄老教程里的 8192：**
#
#    | 实现 | 值 | 说明 |
#    |---|---|---|
#    | `metacubex/utls` ≤ v1.8.1 | **8192** | 老版本 |
#    | `metacubex/utls` v1.8.7（sing-box 1.14.2 依赖的版本） | **16384** | 2026-06-26 提到 16 KiB |
#    | `XTLS/REALITY` 主线 | **17 × 1024** | |
#
#  **判据是「以你实际装的那个二进制为准」，不是「记住一个数字」。**
#  查法：对你用的那份实现，在源码里搜 `realitySize` / `size =`。
#  新版（16/17 KiB）比老版宽一倍 —— 老教程里「8192 是硬上限」的说法已经不准了。
#
#  反例：部分站点的完整证书记录可以逼近甚至超过这个上限 → 握手失败，
#  客户端表现为**完全连不上**，服务端日志只有 "processed invalid connection"——
#  两边的报错都不指向真正原因。
#
#  自己挑目标时的要求：TLS 1.3 ✓ / X25519 ✓ / **Certificate 记录不超过你那份实现的上限** ✓
#  ⚠️ **判断方法：不要看 `openssl -showcerts` 的 DER 长度** ——
#     真正撑爆记录的是证书里的 **OCSP staple / SCT 扩展**，
#     必须量 TLS 握手里那个 Certificate 消息的长度。
#
# ── 三、安装要点（踩过的坑）─────────────────────────────────────────
#   1. sing-box 官方 linux-amd64 构建是 **glibc 动态链接**的，
#      Alpine（musl）跑不了，报 "not found"（文件在、权限对，但加载器缺失）。
#      ✅ 用 musl 版：.../v<版本>/sing-box-<版本>-linux-amd64-musl.tar.gz
#      ❌ 不要用 gcompat 绕过 —— 官方有 musl 构建，没必要加东西。
#   2. `log.timestamp` **默认 false**。
#      ⚠️ 两点澄清（原稿把版本归属写错了，实测纠正）：
#        a) **与 1.11 无关** —— 这个默认值至少从 1.4 起就没变过，
#           变更日志里没有对应的 1.11 条目。
#        b) **只对「写文件」成立**。设了 `log.output` 时，false 就是**完全没有时间**；
#           而**控制台/stderr** 上无论 false 与否都会打 `INFO[0000]` 这类
#           **相对秒数**前缀 —— 那不是"没有时间戳"，是另一种时间格式。
#      要绝对时间就显式 `"timestamp": true`。
#   3. ⚠️ sing-box 的日志全部走 **stderr** → 常见的 `sing-box.err`。
#      而 stdout 那个文件**可能是空的**。
#      排查问题时 grep 错文件会得到「日志是空的」的假象 —— 踩过。
#   4. **若你用了 remote rule_set**：URL 必须先逐个验证 —— 404 会让 sing-box
#      启动失败。验证方式就是直接 curl 一遍，别等启动时报错。
#   5. **若你用了 remote rule_set**：1.14 起对「隐式默认 HTTP client」会告警，
#      **计划在 1.16 移除**（截至 2026-09-30 最新正式版仍是 1.14.2，
#      1.15 还在 alpha —— 也就是说**这个移除还没发生**，但你今天写的配置
#      应该直接按新写法来，别等它坏）。新写法是顶层 `http_clients` +
#      route 的 `default_http_client`。
#      🔴 **但 1.15 已经把它变成致命错误了**（实测 1.15.0-alpha.9）：
#         缺 `http_clients` 时直接
#         FATAL to continuing using this feature, set environment variable
#         ENABLE_DEPRECATED_IMPLICIT_DEFAULT_HTTP_CLIENT=true
#         而 1.14.x 上同样的配置只是 WARN。
#         ⇒ **打算跟着 1.15 走的话，`http_clients` 必须先配好**，别等 1.16。
#      ⚠️ 两个坑（文档没写清，实测才知道）：
#        a) **Dial 字段是平铺的**，不能写成 "dial": { "detour": ... } ——
#           sing-box 对未知字段严格报错：unknown field "dial"。
#        b) **不要设 detour: "direct"** —— check 能过，但运行时报
#           FATAL: detour to an empty direct outbound makes no sense。
#           留空即可（走默认路由，即 route.final）。
#      验证方式：重启前后对比该告警的**计数**（它没有时间戳，
#      只看总数无法判断是否本次产生）。
#      （本仓库的示例配置**不含 rule_set**，所以删掉了 `http_clients`。）
#      ⚠️ **告警的触发条件容易误判，别靠"看起来配了"下结论**：实测它的判据是
#      **两个条件同时成立** ——
#        ① `http_clients` **为空**；**且**
#        ② 真有一个组件在用「默认传输」（remote rule_set / ACME / DERP 的
#           `verify_client_url` 之类）。
#      ⚠️ **少了第 ② 条会误判**：`http_clients` 为空但配置里**根本没有这类组件**时，
#      **一行输出都没有** —— 那不代表"配好了"，只代表没东西可告警。
#      （反向：`http_clients` 非空时，即使 `default_http_client` 没显式指
#       —— 会取第一条 —— **也不告警**。）
#      所以「配了却还告警」多半是看错了主机或看错了日志文件 ——
#      实测不要靠肉眼，用 A/B 隔离跑一遍就能钉死（**A/B 里两边都要有 rule_set**，
#      否则 B 侧会因为第 ② 条不成立而一行不吐，看起来像"没差别"）：
#          sing-box run -c <配置>       # 前台跑，stdout/stderr 直接可见
#      配好的是 0 行输出，摘掉 `http_clients` 的那份吐 1 行 WARN。
#      ⚠️ 别用 `... | head -N` 的退出码判断成败 —— 那是 `head` 的，
#      恒为 0，命令没跑起来也「成功」。要么重定向到文件再看行数，
#      要么直接 cat。踩过。
#      ⚠️ 最小可用写法：`http_clients` 的 `engine`/`version` 都有默认值
#      （`go` / `2`），所以有条目只写 `"tag"` 就能用。可加
#      `domain_resolver` 指到本机 dns 的 tag（**平铺**在顶层，见 (a)）。
#   6. 精简镜像**没有 ss / tc / ethtool**，`ss` 命令可能不存在；
#      用 netstat 或 python socket 探端口，别被 "ss: not found" 误导成「没监听」。
#   7. ⚠️ **小内存实例上 sing-box 会被 OOM-killer 杀掉，而且被杀是无声的**
#      （实测：在几百 MiB 这一档内存的实例上，sing-box 的常驻内存可以涨到
#       吃掉大半物理内存，从而触发 global OOM —— **具体阈值取决于你的负载，
#       自己压一遍**，别照抄别人的数字）。
#      容器里跑的话 `restart: always` 兜得住；**裸二进制 + OpenRC 就没人管** ——
#      进程消失、端口不再监听，而 `rc-service sing-box status` 只显示
#      `crashed`，没有任何东西会把它拉起来。所以必须配保活，见第五节。
#      查证：`dmesg | grep -i 'killed process'`，能看到 `Out of memory: Killed
#      process <pid> (sing-box)` 那一行 —— 别只看 sing-box 自己的日志，
#      它被 SIGKILL 时来不及写任何东西，日志里**没有**对应的收尾记录。
#
# ── 四、改配置的安全动线（务必照做）──────────────────────────────────
#   1) 先写到 /tmp/config.new.json
#   2) sing-box check -c /tmp/config.new.json     ← 校验不过就别往下走
#   3) cp /etc/sing-box/config.json /etc/sing-box/config.json.bak.$(date +%Y%m%d-%H%M%S)
#   4) 覆盖并再 check 一次线上路径
#   5) rc-service sing-box restart
#   ⚠️ 重启会**打断所有已建立的代理会话**（日志表现为一批
#      "mux connection closed: read frame header: EOF"，EOF 是客户端发的），
#      客户端会自动重连。选在低峰期做。
#   ⚠️ 排查进程时不要用 `pkill -f sing-box` —— 你当前 shell 的 cmdline 里
#      就含这个字符串，会把自己杀掉（实测被 SIGTERM 掉、拿不到输出）。
#      按 /proc/<pid>/exe 的 readlink 识别。
#
# ── 五、保活：必须用 supervise-daemon，不能用 command_background ────────
#  `command_background=true` 只是「把进程丢到后台」——**进程一死就没人管**，
#  OpenRC 不监督也不重启。它适合前台会自己崩了拉倒的场景，不适合这里。
#
#  ✅ 正确写法是交给 supervise-daemon（OpenRC 自带的监督进程），与
#     `cdtguard` 服务用的是同一套：
#         supervisor="supervise-daemon"
#         respawn_delay=5      ← 退出后等 5 秒再拉起
#         respawn_max=0        ← 0 = 无限重启
#     respawn_max=0 是**故意**的：设成有限次数，就等于「重试几次后放弃」，
#     而放弃之后又是无声停摆 —— 正是要避免的那个失败模式。
#     坏配置会导致每 5 秒重启一次（日志刷屏），这是**可见**的，比静默强。
#     另有 respawn_period（默认 1800 秒）作为计数窗口，一般不用动。
#
#  ⚠️ 换成 supervise-daemon 后，**升级二进制的顺序变了**：
#     supervise-daemon 在 `rc-service stop` 之后仍可能持有旧二进制的
#     inode，紧跟着 `cp -f` 覆盖会踩到 ETXTBSY（内核拒绝写入正在执行的
#     文件）。`command_background` 模式没这个问题，所以旧的升级脚本
#     换到这里也可能翻车。
#     稳妥做法：先升级、后改 init 脚本；或替换前确认无 sing-box 进程存活。
#
#  ⚠️ 验证保活**不能只看 `rc-service status`** —— 必须真的杀一次：
#         kill -9 <sing-box-pid>; sleep 10; rc-service sing-box status
#     确认**新 PID** 出现、端口重新在听，才算数。
#
# ── 六、tailscale endpoint 的 auth_key 与「免轮换」─────────────────────
#  ⚠️ **两把 key 别混为一谈**，这是最容易白忙一场的地方：
#
#    | | 有效期 | 用途 | 过期后果 |
#    |---|---|---|---|
#    | **auth key**（`tskey-auth-…`）| 1~90 天，不填则 90 天 | **仅首次注册**用一次 | 对已入网节点**毫无影响** |
#    | **node key**（节点身份）| 默认 180 天 | 长期身份 | 掉线，需重新认证 |
#
#  ⚠️ **node key 的时长不在 sing-box 里配** —— 它由 **Tailscale 侧**控制
#     （控制台 Device management → Key Expiry，或 tailnet settings 的
#      `devices_key_duration_days`）。**sing-box 的 tailscale endpoint 没有这个字段**，
#     别去 `sing-box` 的配置里找它。
#
#  「每三个月要换 key」的根源是 **node key**，解法是给节点设
#  **`keyExpiryDisabled`**（Tailscale 设备设置里的 "Disable key expiry"），
#  **不是**删 auth key —— auth key 过期根本不会让你做任何事。
#
#  ✅ 免维护的正解：**节点禁 expiry（一次性）+ 配置里不放 auth_key**。
#  实测：`auth_key` 一旦成功注册过，state 目录里就有 `profile-data/` +
#  `tailscaled.state`，此后**配不配 `auth_key` 都能正常起来**
#  （`sing-box check` 通过、重启后无 `API key does not exist`、端口正常）。
#  所以留着明文 auth_key 是纯粹的多余暴露面，删掉它、只留 state 即可。
#  ⚠️ 代价：**state 丢了就得重新认证**。故建议「配置里删、Tailscale 后台保留
#  一条可复用 auth key 作为应急」，而不是把 key 彻底删掉。
#
#  ⚠️ `auth_key` 的**明文只有在创建那一刻返回一次**，之后 API 查不到
#  （`get_key` 只给 id/描述/能力，无 `key` 字段）。所以「复用旧 key」往往做不到，
#  只能新建。判断是否已失效：节点上 `tailscale status` 或日志里
#  `invalid key: API key does not exist`。
#
#  ⚠️ **`tailscale0` 系统网卡不存在 ≠ 故障**。sing-box 的 tailscale
#  endpoint 默认走 **userspace(gVisor) 协议栈**，本就不建网卡；
#  只有设了 `system_interface: true` 才建。别拿它当判据 —— 踩过。
#  真正的判据是节点在 tailnet 里的 `connectedToControl` / `lastSeen`。
#
# ── 服务定义：/etc/init.d/sing-box ───────────────────────────────────
# #!/sbin/openrc-run
# name="sing-box"
# description="sing-box Service"
# command="/usr/local/bin/sing-box"
# command_args="run -c /etc/sing-box/config.json"
# supervisor="supervise-daemon"      ← 保活，见第五节
# respawn_delay=5
# respawn_max=0                      ← 0 = 无限重启
# pidfile="/run/sing-box.pid"        ← 注意：记的是 supervise-daemon 的 pid
# output_log="/var/log/sing-box.log" ← stdout，可能为空
# error_log="/var/log/sing-box.err"  ← stderr，**真正的日志在这**
# depend() { need net; after firewall; }
#
# 启用：rc-update add sing-box default
# 常用：rc-service sing-box status | restart
```
