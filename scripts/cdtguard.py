#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CDT 流量守护（混合口径 + 云监控交叉校验 + 实例身份自识别 + 实例 Tag 停机锁 + 微信推送 + OSS 审计）

部署位置：ECS 实例 <YOUR_INSTANCE_NAME> (<YOUR_INSTANCE_ID>) 的 /root/cdtguard.py
运行方式：OpenRC 服务 cdtguard（supervise-daemon，崩溃自动重启，开机自启）
时间口径：统一 UTC+8
详见 ../docs/proxy-interface.md（守护对代理侧的三条硬约定）

⚠️ 凭据一律走 `<YOUR_*>` 占位符 —— 推荐用实例 RAM 角色，值不必写进文件。
   部署后想确认装上去的是不是你要的那份，在实例上跑
   `sha256sum /root/cdtguard.py`，与你**自己本地那份**比对。

⚠️ `scripts/tokhash.py` 做的是 token 级等价校验，**只在两个文件都在本机时可用**
   （它只读两个本地路径，不做传输）。跨机器比对得先把文件取到同一处。
   它的输出也**随 Python 版本变化**（tokenize 的切分细节会变），所以
   「token 数相等」只能算同版本下的弱证据；`ast.dump` 同理（输出格式随版本变）。

   **更强的替代是静态检查**，见 `ci/check-names.py`：
   载入但未绑定 / 定义了但从未被引用。有一类失效是**函数定义部署上去了、
   调用漏了** —— `py_compile` 查不出（语法没错）、导入也查不出（不执行那个函数），
   只有静态检查能抓。

⚠️⚠️ **改本文件必须保持 LF 行尾**（踩过）：
   某些写入方式会把 LF **静默转成 CRLF**，
   于是 token 流里的 NEWLINE token 由 `'\n'` 变成 `'\r\n'` ——
   **token 数量不变，但指纹不同**，看起来像"代码不一致"，
   实际只是行尾差异。写入必须用 `open(p, "w", encoding="utf-8", newline="")`
   或二进制模式。事后修复：`open(p,"wb").write(open(p,"rb").read().replace(b"\r\n", b"\n"))`

── 关键设计（每一条都对应一个踩过的坑）──────────────────────────────────

1. 度量口径：**混合口径** —— 云监控为基准，网卡补最近几分钟的增量
       eff = 云监控值 + max(网卡自「云监控覆盖到的时刻 X」到现在的累计增量)
   * 云监控 = 共享带宽包 acs_bandwidth_package 的 net_rx.rate/net_tx.rate 积分。
     它**就是计费原始数据**：准、且与实例完全解耦（不依赖 guest OS、不受网卡
     计数器归零影响、不含内网非计费流量）。
   * 但云监控**滞后 170~300 秒**（会波动，别按固定值算，
     会波动）。全速 800Mbps 下 3.7 分钟 = 约 22GB 盲区，所以**不能单独当闸门**。
   * ⚠️⚠️ **两条路径的偏差方向不一样，别混为一谈：**
     · **纯网卡口径**（回退路径）：每 2 秒 `acc += max(drx, dtx)`，即 Σmax(每样本)；
       而计费按 max(Σ整小时)。恒有 Σmax ≥ max(Σ) ⇒ **结构性偏高约 +5%**（方向安全）。
     · **混合口径**：桥接段取的是 `max(Δrx, Δtx)` —— **一整段一个 max，不是逐样本求和**。
       形状与计费口径同形 ⇒ **不额外引入那 5%**。
   * 结论：混合口径**更准，但余量比纯网卡小** —— 它靠的是云监控本身准
     （它就是计费原始数据），**不是**靠偏高。别把「偏高 5%」当混合口径的安全垫。
   * ⚠️ 偏差告警比的是**纯网卡 acc vs 计费侧**，所以那里出现约 +5% 是正常的
     —— 那走的是 Σmax 那条路，与混合口径无关。
   * ⚠️ 桥接起点必须是**云监控实际覆盖到的时刻 X**，不是「取数时刻」——
     用取数时刻当起点的话，那几分钟滞后照样漏掉。
   * 🔴 **混合口径没有那 5% 的安全缓冲**
     纯网卡口径下 `eff = acc` 偏高 5%，所以 acc 到 200 时计费侧只到约 191GB
     （8.7GB 缓冲）。混合口径下 `eff ≈ 计费侧口径`，触发时计费侧正好 200 ——
     **缓冲为 0**。
     要留余量请显式设 `TRAFFIC_THRESHOLD_GB=185`（见 `deploy-cdtguard.sh` STEP 4.5）。
     ⚠️ 正因如此，下面第 23 条那层兜底（eff 与 cdt_floor 取 max）更重要 ——
        它至少保证**绝不少算**，且不依赖"额度在月首 00:00 重置"这个假设。

2. ⚠️⚠️ CMS 数据点的**时间戳语义**（极易搞错 —— 下面标出来的就是错过一次的样子）
      小时级点标注 T 覆盖的是 **[T-1h, T)** —— 标注的是区间**结束**时刻。
      分钟级同理，标注 X 覆盖 [X-1min, X)。
      实测对齐证据：取该小时的分钟级积分求和，
      与小时级那个点的值完全相等。
      曾按「标注=区间开始」写成 `t + 3600000 > now 就跳过`，
      于是把最近一个**已走完**的整小时误判成「当前小时」丢弃，
      改用分钟级去估 —— 实测少算约 0.6 GB。

3. ⚠️ 小时级数据点必须留**结算余量**（BW_SETTLE_MIN=5）
      刚结束的那个小时，聚合值仍在形成中。实测 17:00:12 就把「17:00」这个点
      当成已结算纳入，结果 (a) 金额少算 (b) 覆盖时刻被报成整点 → 桥接窗口被
      压到 12 秒 → 合计少算。整点后 5 分钟内不纳入该小时。

4. ⚠️ 覆盖时刻 = 最新**分钟级**数据点，不是小时级标注
      云监控的值是「整小时 + 分钟级尾部」拼出来的，真实覆盖到的是那几个分钟点。
      曾错用小时级标注当覆盖时刻，导致桥接窗口被算成 1 小时，
      15 分钟的网卡历史根本回溯不到 → 基准永远建不起来。

5. ⚠️ 偏差告警必须是**单向**的，不能写 abs(dev)
      偏高是上述结构性使然（约 +5%），属预期行为；**偏低才危险**
      （说明网卡少算、可能在免费额度外跑）。写成双向必然误报。
          BW_UNDER_PCT = -2.0    # 网卡低于计费侧 -> 告警（危险方向）
          BW_OVER_PCT  = 30.0    # 极端偏高兜底（正常约 +5%）
      且比较必须用**同一时刻的快照**（bw_acc）：acc 每 2 秒刷新、bw 每 600 秒
      才刷新，拿实时 acc 比会随 bw 变旧而单调放大 —— 纯属跨新鲜度比较的假象
      （曾因此每 10 分钟误报一条）。

6. 持久化矩阵（重启后不丢统计的正确性）
      * 本月累计 acc：本地双文件 + OSS，load_state 取三者**最大**（只可增不可减）
      * 混合基准 hb_base/hb_ts/hb_rx/hb_tx：本地 state，跨**进程**重启存活
      * 网卡采样环：仅内存 —— 它只用于刷新时回溯新基准，丢了会沿用旧基准，不影响正确性
      * **OSS 从只写改成可回读**：整机重建时本地文件不存在，靠 OSS 接续累计
        （此前只写不读 → 重建后 acc 从零开始 → 少算 → 可能超额）
      * ⚠️ 网卡计数在**虚拟机重启**后归零 → 恢复基准时校验 rx/tx >= 基准，
        不满足则作废（退回纯网卡）。实测重启后该分支正确触发
      * ⚠️⚠️ **last_rx/last_tx 必须随 OSS 对象一起持久化**（见第 17 条）

7. 停机锁用「实例 Tag」而不是 OSS
      达阈值时给实例打 Tag `cdt-halted-month = <当前月>`，FC 读到即拒绝拉起。
      ⚠️ Tag 挂在资源上，实例一旦重建就没了。
      🟢 **这条已经不需要人工核对了** —— 护闸会从元数据服务自识别真实
         实例身份（第 15 条），FC 侧也加了 fail-loud。但**仍然**要记得：
         FC 的 `ECS_INSTANCE_ID` 环境变量是人工维护的，重建后必须更新。
      ⚠️ OSS 的 halted 字段**不是**可选审计，它是 Tag 打标失败时的唯一兜底
         （FC 的判据是 tagHalted || ossHalted）。
      ⚠️⚠️ FC 侧读 halted **必须绑定月份**（第 29 条），否则跨月自锁。

8. ⚠️ 停机前必须补写一次 OSS halted=1
      OSS 平时每 60 秒才写一次，达阈值那一刻它的 halted 仍是 0（上次写时 eff 还
      低于阈值）。若不补这一笔，一旦 tag_halted() 失败，FC 两条判据全为 false，
      会把刚停下的实例重新拉起继续烧流量 —— 兜底形同虚设。

9. ForceStop 必须为 "true"
      本实例用自制精简镜像导入，镜像内无阿里云代理组件，优雅关机指令投递不进
      guest，实例会**永久卡在 Stopping**（实测 >2分39秒），而进程仍在跑、流量仍在烧。
      强制关机实测 6 秒完成。

10. 推送必须在 do_stop() 之前
      ForceStop 会**立刻杀死本进程**，停机之后的任何代码都不会执行（已实际验证）。
      所以「停机成功」的推送改由 FC 侧发出。
      ⚠️ 而 notify() 是**阻塞**的 —— 临界路径必须用短超时（第 19 条），
         否则 pushplus 慢时会把停机推迟最多 15 秒 = 800Mbps 下 1.5GB。

11. 日志与状态必须 fsync
      硬关机时 guest 的脏页会被丢弃。log() 恒 fsync；save_state() 按**字节**决定
      是否 fsync（FSYNC_BYTES），这样硬关机后的「少算量」有上界。
      save_state 的临时文件名必须带 PID（固定名会并发互踩，实测 save_state ERR）。

12. 计数只在月份切换时归零（同时作废混合基准 —— 云监控值按月重置）
      绝不在「实例非 Running」时清零 —— 否则配合坑 9 会导致护闸永久解除武装。

13. 网卡计数器随重启归零：用 rx >= last_rx ? rx-last_rx : rx 处理回退。

14. 采样间隔 2 秒：触发瞬间的超调量 ≈ 采样间隔 × 速率。

15. ⚠️⚠️ 实例身份必须**运行时自识别**，不能只用内嵌常量
      内嵌的 INSTANCE_ID/EIP 一旦过期（实例重建后 ID 会变），整条停机链会
      **静默失效**。实测证据链：
        DescribeInstances 查一个**不存在**的 ID → HTTP 200 + TotalCount 0 +
        空数组，**不是错误** → is_running() 返回 None 而**不抛异常** →
        旧代码 `if s == "Running"` 不成立 → 停机块被整个跳过 →
        连 err_streak 的兜底推送都不触发（因为没有异常）。
        结果：只有一行 THRESHOLD HIT 日志每 2 秒重复，流量继续烧，全程无声。
      降级链：元数据 → EIP 反查 → 内嵌值 + 强告警。
      ⚠️ 元数据读取必须做两个防护（否则会亲手制造新的静默 fail-open）：
         1) 用**空 ProxyHandler** —— urllib 默认读 http_proxy/HTTP_PROXY，
            哪天 rc 环境里加了 proxy，元数据请求会被送去代理、返回错误页。
            这个故障只在元数据这一处爆出来，极难查。
         2) 必须 `.strip()` —— 元数据返回值带 \r\n，不 strip 会让 INSTANCE_ID
            变成 "i-xxx\r\n"，拼进 InstanceIds JSON 后报 InvalidInstanceId.NotFound。
      ⚠️ 取不到时**保留内嵌值**而不是拒绝启动：拒绝启动 = OpenRC 每 5 秒重启一次
         → 无限 crash-loop，期间完全无计量、无护闸，而且 rpt() 在 main() 里，
         连告警都发不出去。那是拿小风险换大风险。
      ⚠️ 身份解析与计量**解耦**：计量必须永远跑；身份只决定「停机打给谁」。

16. ⚠️⚠️ 停机判定**不能**依赖 is_running() 状态查询
      旧代码是 `s = is_running(); if s == "Running": 停机` —— 那是个 fail-open：
        ID 过期时 is_running() 查的是**旧实例**。旧实例若处于 Stopped
        （节省停机模式会留着它），s == "Stopped" → 整个停机块被跳过 → 流量继续烧。
      根本性的认识：**护闸运行在这个实例内部，代码能执行 = 实例必然 Running。**
      状态查询在语义上是多余的，它只能是「省一次 API 调用」的优化，
      **绝不能当安全闸门**。所以直接删掉，无条件走完整路径。
      停机依据始终是 `eff >= 阈值`（本地网卡 + 计费侧口径），与实例状态无关。
      ⚠️ 相应地把阈值告警**移出 if** 并无条件发一次（`st["thr_alerted"]` 一次性
         标志，随 st 落盘故跨重启有效，跨月重建 st 时自动复位）——
         否则连告警都会被一起吞掉，整个失败完全静默。

17. ⚠️⚠️ load_state 的 OSS 候选必须补 last_rx/last_tx，且**缺省 None 不是 0**
      
      旧代码的 OSS 候选没有这两个键，而主循环 else 分支直接取 st["last_rx"]。
      整机重建后本地两个 state 文件不存在 → OSS 候选胜出 → 月份相同 →
      进 else → **KeyError** → 被外层 except 吞掉 → 每 2 秒重复。
      而告警条件是 `err_streak == 5`（只在"等于"时推一次），所以此后永远沉默。
      表现：rc-service status 显示 started，实际零计量、零 OSS 写入、零停机 ——
      护闸看起来完全正常，实际彻底失效。
      ⚠️⚠️ 缺省值必须是 **None 而不是 0**：若给 0，主循环 `if lrx is None` 不会
         成立，就会走 `drx = rx - 0 = rx` —— 把**整个网卡计数器**当成一轮增量，
         凭空多算几个 GB，可能直接误触发停机。**那比 KeyError 更糟：
         崩溃是响的，静默多算才致命。** 用 None 才能让主循环走「以当前计数
         重新基线化」的分支（保留 acc、本轮增量记 0）。
      同时 oss_put 的负载也要写入 last_rx/last_tx，否则每次重建都要重新基线化
      （无害但会多一条日志）。

18. ⚠️ 演练开关用**命令行参数**，不用环境变量
      环境变量会从 /etc/conf.d/cdtguard 进来，一旦有人写进去忘了删，护闸就
      **永远只打 tag、不停机** —— 而 tag 打上了、推送也发了，看起来像
      "触发过了"，可能几个月都发现不了。这是把 fail-open 从「条件触发」
      变成「配置即触发」。
      命令行参数只能在手动调用时加上，OpenRC 的 command_args 里不会意外带上。
      注意 `--dry-run` 走的是 StopInstance **自带的 DryRun**（真实 API 预检，
      实测：HTTP 400 + Code=DryRunOperation，实例状态保持 Running；
      它会真实校验 INSTANCE_ID 是否有效、AK 有无 ecs:StopInstance 权限、
      实例是否支持该停机方式），**不是**"跳过停机"。
      顺带：DryRun 演练**不会真停实例**，所以 SO 也不再需要 OSS 诱饵解锁流程。

19. ⚠️ 停机路径必须区分「看起来像失败」的几种情况
      官方错误码（已查证 + DryRun 实测）：
        404 InvalidInstanceId.NotFound  → **唯一的真失败**，必须响铃
                                          （正是身份过期时的症状）
        403 IncorrectInstanceStatus     → 当前状态不支持（即已 Stopped）
                                          → 视为成功（幂等语义），不告警
        400 DryRunOperation             → DryRun 预检通过，实例**未停** → 成功
      ⚠️ 注意 StopInstance 对已停实例**不是** API 幂等的（返回 403），
         所以不能把任何 4xx 都当失败。
      ⚠️ 临界路径的推送用 `fast=True`（5 秒超时），别让推送把停机推迟。

20. ⚠️ err_streak 告警必须**周期重推**
      旧代码是 `if err_streak == 5` —— **只在恰好等于 5 时推一次**，之后无论
      失败多久都永远沉默。而「持续失败」恰恰是最需要被知道的状态
      （例如第 17 条那个 KeyError 卡死：护闸每 2 秒抛一次异常，
      但只在第 5 次推了一条，此后彻底无声）。
      改成 `err_streak >= 5 and (err_streak - 5) % 300 == 0`（约每 10 分钟一条）。

21. 🔴 跨月额度重置的**确切时刻从未验证**（遗留）
      护闸假定额度在 UTC+8 每月 1 日 00:00 重置，并据此重算 bw_month_ex() 起点。
      若真实重置点晚于 00:00，月头那段时间护闸以为归零、计费侧仍在上月额度里，
      属**危险方向**。建议下月 1 日 00:00~08:00 抓一次 bw_month_ex() 与账单页
      对比后再信任；确认前月头别顶格跑。

22. 🔴 诱饵对象是未纳管的后门（遗留）
      OSS 诱饵（`{"month":"<上月>","acc":<巨大>}`）靠护闸重启后 60 秒内的首次
      OSS 写入自我覆盖，用完后必须**显式删除并验证**，不能"等它自己覆盖"。
      实测出现过 FC 运行读到诱饵的情况（ossTs 是某个**非本月**月份、
      eff 是一个异常大的值、halted=1），当时正被 FC 当停机判据消费。
      含义：(a) 任何能写 OSS 的动作都能让护闸静默归零（load_state 取 max acc +
      月份不符即重置），(b) 建议把诱饵挪到独立 key，并让 load_state 永不读它。
      —— 不过第 18 条之后演练改用 DryRun，不再需要诱饵，这条风险自然消失。

23. ⚠️⚠️ eff 必须与 cdt_floor 取 max —— 额度是**账号级**的
      混合口径的 `eff_hybrid` 只估算【共享带宽】那一份，而 200GB 免费额度是
      **账号级**的。实测 `ListCdtInternetTraffic` 按产品拆解：
        cbwp（共享带宽）2.6 GB + eip 0.0004 GB + fc（函数计算）0.0012 GB
        = 合计 2.6 GB     ← 数值为示意，只为说明「分项之和 = 顶层」
      **合计才是真正被限的那个数** —— 注意 FC 自己的流量也在烧这 200GB。
      所以：`eff = max(eff_hybrid, cdt_floor)`，其中 cdt_floor 是该 API 的
      **本月累计**（权威口径，只是有延迟）。取 max 保证**绝不少算**。
      正常月份不受影响：两个口径量级相同，仍由 hybrid 主导。

24. ⚠️⚠️ 跨月时**不要清零 cdt_floor**
      旧代码在跨月分支里直接 `cdt_floor = 0` —— 那等于**假设**
      "额度已在月首 00:00 重置"。若真实重置更晚，月首那段时间护闸会误以为
      额度已刷新而**放松保护**，属危险方向。
      改为立刻向 CDT API 取一次权威值：
        API 若已进新月份 → 它自然返回 ~0 ✓
        API 若还没进     → 它仍返回上月累计 → 护闸保持戒备 ✓
      ⚠️ 这让"重置时刻"从一个**必须猜对的前提**降级成一个**只影响精度的观测项**。
      官方口径佐证：CDT 额度公告用「自 北京时间 YYYY年MM月01日00:00:00 起」
      措辞，且非中国内地额度 200 GB/月（与内地 20 GB/月**独立计算**）；
      但**政策生效时刻 ≠ 额度重置时刻**，两者不能混为一谈。

25. ⚠️ 推送失败必须被计数并透出
      `notify()` 把异常全吞了，推送失败只落一行日志 —— 而整条告警链是
      **单点依赖 pushplus**。加模块级 `PUSH_FAILS[0]` 连续失败计数，
      并写进 OSS 的 `push_fail_streak` 字段，FC 的存活巡检会读它。
      ✅ FC 侧已对齐：`PUSH_TOKEN` 缺失时 `requireEnv()` 直接抛错拒绝启动，
         不再静默降级成「护闸在跑、告警永不发」。

26. ⚠️ spot 抢占预警改用 `OperationLocks.LockReason`
      官方文档「感知抢占式实例中断事件与响应」给的三种方式里，**最直接的是第二种**，
      而且用的就是本进程本来就在调的 `DescribeInstances`：
        * OperationLocks 返回空值   -> 实例可持续使用
        * LockReason == "Recycling" -> 抢占式实例被中断，处于待回收状态
      另据官方原文：「在实例被释放的**前五分钟**，云服务器ECS会发送抢占式实例中断事件」。
      实现：`inst_state()` 取 (Status, lock_reasons)，`preempt_check()` 挂在
      CDT_EVERY（10 分钟）周期上，检测到 Recycling 推一次预警
      （`st["preempt_alerted"]` 一次性标志，锁解除后复位以便下次能再报）。
      ⚠️ **为什么不用 `DescribeSystemEventAttribute`**（三条实测出来的限制）：
         1) **没有实例 ID 参数** —— 只能按 Product/EventType/Name 过滤，返回的是
            **账号内所有实例**的该类事件，得自己筛
         2) **结果按时间升序且不给总数** —— 实测 PageSize=20 返回的全是**最旧的**
            （旧实例的磁盘/网卡/镜像操作），要拿最近的得翻到最后一页
         3) **查询窗口 ≤ 7 天** —— 实测查 30 天直接 `ParameterInvalid`
         4) 还要额外的 `cms:DescribeSystemEventAttribute` 权限（资源 `*`）
         收益（拉取式、免费、只读、可查历史）抵不过这些限制。

27. 🔴 跨月时 `bw_acc` 必须跟着 `st` 一起归零
      `bw_acc` 只在 `i % CDT_EVERY == 0` 那一拍刷新（`bw_acc = st["acc"]`），
      而跨月分支换掉了整个 `st` 却不管它 → 此后最多 10 分钟内 `dev` 拿**上月的
      bw_acc** 去比**新月的 bw_bytes**；若跨月恰落在 CDT_EVERY 那一拍
      （概率 1/300），`bw_acc` 直接是 0 → `dev = -100%`
      → 误报「网卡口径低于计费侧」—— **危险方向**的假警报
      （真"偏低"才意味着可能超额）。
      `bw_acc` 不参与 `eff`，**不会误停机**，但它出现在**跨月路径**上，
      而跨月是第 24 条专门加固过的地方。
      症状：会报出一个**极其夸张**的 `DEV ALERT` 百分比 ——
      这正是把「毫秒」当「秒」和把「字节」当「GB」叠加出来的假象。

28. 🔴 `s2 = s` 悬空引用 —— 停机「未生效核验」从未执行过
      `s` 原本来自已被删除的 `s = is_running()`（第 16 条那次 fail-open 清理），
      删的时候把 `s2 = s` 落下了。main() 里**再无任何地方**给 `s` 赋值
      → 执行到这一行必抛 `NameError`。
      ⚠️ 这一行**只在"停机没能把本进程干掉"时才到得了**（DRY_RUN、或停机失败）
      —— 真实停机成功时 ForceStop 会**立刻杀掉本进程**，根本走不到这里。
      所以它**只在最不该哑火的地方哑火**。后果是双重的：
        ① 「停机未生效」核验（20×3s 轮询 + 人工介入告警）**从未真正执行过**；
        ② 末尾 `time.sleep(600)` 退避被跳过 → 异常冒到外层 except
           → 只 `sleep(INTERVAL)=2s` → 每 2 秒重跑整个阈值块：
           `tag_halted` + `oss_put` + `StopInstance` 各 30 次/分钟，无限持续。
      修法：`s2 = None` 起手；并把整段包进 `if DRY_RUN:` 跳过
      （演练本就没打算真停，原先会白等 60 秒再报一条假的"停机未生效"）。
      验证：作用域扫描器查未定义引用，能报出 `s` 这种悬空引用，修掉即消失。
      ⚠️ 而 EventBridge 推送那条**已经开着**（filter 含
         `ecs:Instance:PreemptibleInstanceInterruption`），保留作**双保险** ——
         它在护闸已死时仍能工作（视角是 FC 而非实例）。
      ⚠️ **无法端到端验证**：除非真发生一次抢占回收，否则只能验证到
         "字段存在且常态为空"。已做的验证：单元桩测试 5 个场景全过
         （无锁不报 / 有锁报一次 / 不重复 / 解锁复位 / 再锁能再报）
         + 在实例上跑 `inst_state()`，返回 ('Running', [])。

29. 🔴 跨月"重置未确认"时不得凭 cdt_floor 停机+打标
      这是**整月自我锁死**：跨月时若 CDT API 尚未重置（或取数失败），
      `cdt_floor` 仍是上月终值 ≈200GB，而第 23 条的 `eff = max(eff_hybrid, cdt_floor)`
      会被它顶到阈值 → 立即停机 → `tag_halted(st["month"])` 写入**新月份**
      → FC 判 `tagVal === cur` 成立 → **整月拒绝拉起**，
      而推送还写着「等待次月 1 日额度重置」—— 主动误导。
      两条独立触发路径：① API 小时级批量刷新，跨月那一刻大概率仍读到上月值；
      ② 那一刻 `cdt_bytes()` 恰好抛一次异常（第 24 条"保留旧值"）。
      ⚠️ 与第 24 条看似矛盾，实则互补：第 24 条**不清零**是对的
         （清零 = 假设额度已重置，会放松保护），但它没考虑到
         「保持戒备」的动作恰好是打上新月份 tag，而那是 FC 眼里"本月已停机"的铁证。
         **两半各自都对，合起来才出问题。**
      修法：引入「重置已确认」判据 ——
        * 跨月瞬间记 `floor_prev = cdt_floor`，`reset_confirmed = False`
        * 观测到 `cdt_floor < floor_prev`（下跌）= 额度确已重置 → `True`
        * 兜底：跨月后 `ROLLOVER_GRACE_S`（6 小时）仍未见下跌 → 判"未重置"，
          恢复正常判据并告警（否则会永远不停机）
        * 停机条件拆成两条：
            仅 cdt_floor 顶到阈值 且 重置未确认  → **只告警，不停机、不打标**
            本月自身 `eff_hybrid` 顶到阈值        → 照常停机+打标（保护不弱）
      ⚠️ 只在**真跨月**（`st is not None` 且月份不符）进入该状态 ——
         首次启动 `st is None`，没有"上月终值"可比，若也进入会导致开机 6 小时不停机。
      ⚠️ `ROLLOVER HOLD` 日志必须放在 `rollover_hold_alerted` 的 **once-only 块内**：
         该判据成立后会**持续**成立，放外面每 2 秒刷一行，最多 6 小时 ≈ 上万行。
      验证：桩替换全部外部依赖、**驱动真实新代码**跑 5 个场景
        （floor 保持200GB / floor 跌到1GB / floor 200GB 且 eff_hybrid 也到 /
          cdt_bytes 抛异常 / 宽限期已过），tag 与 stop 结果全部符合设计，
        该日志整段从「每轮一行」降为「只报一次」。
      ⚠️ 未做端到端演练：该路径只在**跨月那一拍**（次月 1 日 00:00）才可能触发，无法提前实测。
"""
import base64, hashlib, hmac, json, os, sys, time, uuid, datetime
import urllib.error, urllib.parse, urllib.request
IFACE = "eth0"
# ⚠️ 阈值 = 200，等于免费额度本身，**没有安全余量**。
#    这是刻意的取舍：可用流量最大化。混合口径已把纯网卡口径下约 5% 的
#    结构性偏高抵消掉，所以触发时计费侧正好 200 —— 缓冲为 0。
#    想留余量就设 TRAFFIC_THRESHOLD_GB=185（留 15 GB）。
# ⚠️⚠️ 阈值**必须按地域取** —— 免费额度是两个池子，不是一个：
#      中国内地地域 20 GB/月，非中国内地地域 200 GB/月（官方口径，见 docs/billing.md §一）。
#    曾经这里写死 200，对香港恰好正确、对内地则**必然超支 180 GB（约 ¥144）**。
#
#    判定规则（取自官方 `DescribeRegions` 的 33 个地域）：`cn-` 开头且非 `cn-hongkong`
#    ⇒ 中国内地。中国香港虽属中国，但在 CDT 口径里是**非中国内地**地域。
#
#    显式设 TRAFFIC_THRESHOLD_GB 时**仍然优先**（用于想留余量的情形，如 185）。
THRESHOLD_ENV = os.environ.get("TRAFFIC_THRESHOLD_GB")
MAINLAND_FREE_GB = 20.0      # 中国内地地域免费额度
NON_MAINLAND_FREE_GB = 200.0  # 非中国内地地域免费额度
WARN_MARGIN_GB = 10.0        # 预警提前量：距免费额度还剩这么多时提前提醒

def is_mainland(region):
    # 判定该地域是否属「中国内地」（决定 20 GB 还是 200 GB 的池子）
    return bool(region) and region.startswith("cn-") and region != "cn-hongkong"

def free_quota_gb(region):
    return MAINLAND_FREE_GB if is_mainland(region) else NON_MAINLAND_FREE_GB


def apply_region(region):
    """按**实测**地域重算阈值。仅在未显式设 TRAFFIC_THRESHOLD_GB 时生效
    （显式值优先，用于「想留余量」的情形）。返回是否改动了阈值。"""
    global THRESHOLD_GB, WARN_GB
    if THRESHOLD_ENV:
        return False
    q = free_quota_gb(region)
    if q == THRESHOLD_GB:
        return False
    old = THRESHOLD_GB
    THRESHOLD_GB = q
    WARN_GB = THRESHOLD_GB - WARN_MARGIN_GB
    log("⚠️ 地域 %s 属%s，免费额度 %.0fGB —— 阈值已按地域调整 %.0f -> %.0f GB"
        % (region, "中国内地" if is_mainland(region) else "非中国内地",
           q, old, THRESHOLD_GB))
    return True

# 先定一个启动值，随后由 apply_region() 按**实测**地域重算（见 resolve_identity）——
# ⚠️⚠️ **未知地域必须按更小的那个池子守。方向反了就不是 fail-safe。**
#    早期版本这里是 `or "cn-hongkong"` ⇒ 取不到地域时阈值 200GB。
#    而部署形态下环境变量送不进来（supervise-daemon 清空环境），
#    `REGION_ID` 基本总是空的 —— 也就是说**默认路径就走这个分支**。
#    一台内地实例若元数据服务不可达，就会拿着 200GB 的阈值守着只有
#    20GB 的池子 ⇒ 必然超支 180GB。而唯一的现象只是一条「元数据不可达」，
#    没人会把它读成「你的阈值高了 10 倍」。
#    宁可早停（可用性损失），不可多烧（真金白银）—— 这才是 fail-safe 的方向。
BOOT_REGION = os.environ.get("REGION_ID") or ""
UNKNOWN_REGION_GB = MAINLAND_FREE_GB     # 未知 ⇒ 按最小池子
THRESHOLD_GB = (float(THRESHOLD_ENV) if THRESHOLD_ENV
                else (free_quota_gb(BOOT_REGION) if BOOT_REGION
                      else UNKNOWN_REGION_GB))
WARN_GB = THRESHOLD_GB - WARN_MARGIN_GB    # 随阈值一起动，不写死
INTERVAL = 2       # 收紧采样，压低触发瞬间的超调量
SYNC_EVERY = 30    # 2s x 30 = 60s 同步 OSS（保持原节奏）
FSYNC_BYTES = 100 * 1024 * 1024   # 未落盘累计 >= 100MB 立即 fsync（限制"少算多少字节"）
FSYNC_ITERS = 30                  # 或每 30 次迭代(60s)至少 fsync 一次（空闲兜底）
CDT_EVERY = 300    # 2s x 300 = 600s 查 CDT 兜底（保持原节奏）
BW_ID = "<YOUR_CBWP_ID>"   # 共享带宽包实例（计费侧口径来源）
BW_EP = "metrics.cn-hongkong.aliyuncs.com"   # ⚠️ 写死香港。换地域要改这里
# ⚠️ 偏差告警是**单向**的，不要写成 abs(dev)。
# 护闸按 Σmax(每次采样) 累加，计费按 max(Σ整小时)，数学上恒有 Σmax ≥ max(Σ)，
# 即网卡口径**结构性偏高**（实测约 +5%，随流量形态浮动，低负载时更小）。
# 偏高是设计使然、方向安全（会提前停机，不会超额）；
# 偏低才危险 —— 说明网卡少算，可能在免费额度外跑。
BW_UNDER_PCT = -2.0                    # 网卡低于计费侧超过此值 -> 告警（危险方向）
BW_OVER_PCT = 30.0                     # 网卡高于计费侧超过此值 -> 告警（正常结构性偏高实测约 +5%）
# ── 混合口径（云监控基准 + 网卡增量补偿）──────────────────────────
# 云监控（共享带宽包 net_rx/net_tx）是计费原始数据：准、且与实例解耦；
# 但实测落后约 4 分钟（单次采样 3m43s，会波动）。
# 网卡是实时的，补的是「云监控还没覆盖到的那几分钟」。
# ⚠️⚠️ 这里取的是 **max(累积增量)** —— **一整段一个 max**，与计费口径同形，
#    所以桥接段**不额外引入**纯网卡口径那约 5% 的结构性偏高。
#    推论：混合口径更准，但**余量比纯网卡小**（它靠云监控准，不靠偏高）。
#    见上面第 1 条「两条路径的偏差方向不一样」。
# 所以：eff = 云监控值 + max(Δrx, Δtx)      （Δ 自「云监控覆盖到的时刻 X」起算）
# ⚠️ 桥接起点必须是 X 而不是「取数时刻」，否则那几分钟滞后照样漏掉。
HYBRID_ENABLED = True    # 切换依据：独立验证 + 护闸自身路径均通过
                         # (保护闸自身路径的 lag 与偏差都落在预期区间)
HIST_MINUTES = 15        # 网卡采样历史长度，须大于云监控滞后的最坏值
# ⚠️ 小时级数据点的结算余量：刚结束的那个小时，聚合值仍在形成中。
#    若不设余量：整点刚过就把该小时的点当成已结算纳入，
#    结果 (a) 金额少算 (b) 覆盖时刻被报成整点 -> 桥接窗口被压到 12 秒
#    -> 合计少算，方向危险。整点后 5 分钟内不纳入该小时。
BW_SETTLE_MIN = 5
# ── 跨月"重置已确认"判据（修跨月自我锁死）─────────────
# 问题：跨月时若 CDT API 尚未重置（或取数失败），cdt_floor 仍是上月终值，
#   而 eff = max(eff_hybrid, cdt_floor) 被它顶到阈值 → 立即停机
#   → tag_halted(st["month"]) 写入**新月份** → FC 判 tagVal===cur
#   → 整月拒绝拉起 → 实例被钉死到次月 1 日。
# 对策：跨月后只有在**观测到 cdt_floor 下跌**（=额度确已重置）或
#   **本月自身用量 eff_hybrid 确实到阈值**时，才允许停机+打标；
#   否则只告警、不停机。兜底：跨月后 ROLLOVER_GRACE_S 仍未见下跌，
#   判定"额度未重置"，恢复正常判据。
ROLLOVER_GRACE_S = 6 * 3600
STATE_A = "/var/lib/cdtguard/state.json"
STATE_B = "/root/.cdtguard.state"
# ── 账号相关值一律从环境变量取（清单见 .env.example）──────────────────
# ⚠️ AK / SK **不在这里解析** —— 它们优先来自实例 RAM 角色的临时凭据，
#    而角色凭据要用 meta()，meta() 定义在本文件后面。解析挪到 meta() 之后。
#    见下方「凭据解析」一节。
#
# 下面三项是「过期兜底」而非凭据：后文会从实例元数据 / DescribeEipAddresses
# 自纠这三个值（real[...] 覆盖 hard[...]），取不到时只告警并推送，
# 注释明写「内嵌身份可能是过期的」。
# 设计上要求「元数据不可达时仍要能停机」—— 所以这里给占位符默认值、
# **不 fail-loud**。改成必填会与该 fail-safe 意图相反。
OSS_BUCKET  = os.environ.get("OSS_BUCKET", "<YOUR_OSS_BUCKET>")
INSTANCE_ID = os.environ.get("ECS_INSTANCE_ID", "<YOUR_INSTANCE_ID>")
EIP         = os.environ.get("EIP", "<YOUR_EIP>")

REGION      = os.environ.get("REGION_ID", "cn-hongkong")

# 与本方案无关的固定值，不含账号信息
OSS_EP    = "oss-cn-hongkong.aliyuncs.com"   # ⚠️ 写死香港。换地域要改这里
OSS_KEY   = "cdtguard/state.json"
INST_NAME = "<YOUR_INSTANCE_NAME>"
ZONE      = "cn-hongkong-c"                  # ⚠️ 写死香港可用区。换地域要改这里
# ⚠️⚠️ 本文件里 BW_EP / OSS_EP / ZONE / REGION 的默认值**都写死香港**。
#    阈值会按实测地域自动取（见 apply_region），但**端点不会** ——
#    换到别的地域必须手改这几行，否则云监控与 OSS 两条链路一起失效（且是静默的）。

# ── 凭据解析──────────────────────────────────────────────
# 优先级：实例 RAM 角色 > 环境变量。
#
# 用角色的理由：凭据不落盘、可轮换、且 RAM 策略能挂在这个角色上
# （挂在实例上而不是代码里）。环境变量作为兜底，让本地/调试仍能跑。
#
# ⚠️ 三个必须做对的地方：
#   1) 解析必须在 meta() 定义**之后** —— 本文件是顺序执行的。
#   2) 角色凭据有**有效期**（实测 Expiration 在 6 小时后），到期前必须刷新。
#      不刷新 = 运行到点后所有 API 调用开始 403，而护闸恰好在那一刻失效。
#   3) 刷新失败**不能**清空已有凭据 —— 宁可继续用旧的（可能已过期、会报错），
#      也不要变成空串（会以奇怪的方式失败，且难查）。
STS_TOKEN = None
_CRED_EXP = 0.0

def _resolve_creds(verbose=False):
    """取实例 RAM 角色的临时凭据。失败返回 None（由调用方决定兜底）。"""
    role = meta("ram/security-credentials/")
    if not role:
        return None
    d = meta("ram/security-credentials/" + role)
    if not d:
        return None
    try:
        d = json.loads(d)
    except Exception as e:
        if verbose:
            log("角色凭据解析失败: %s" % str(e)[:80])
        return None
    if d.get("Code") != "Success" or not d.get("AccessKeyId") or not d.get("AccessKeySecret"):
        if verbose:
            log("角色凭据不完整: Code=%s" % d.get("Code"))
        return None
    exp = 0.0
    try:
        exp_s = d.get("Expiration", "").replace("Z", "+0000")
        exp = datetime.datetime.strptime(exp_s, "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except Exception:
        exp = time.time() + 3600
    return d["AccessKeyId"], d["AccessKeySecret"], d.get("SecurityToken"), exp


# ⚠️ 同样不 fail-loud：pushplus 不是阿里云凭据，取不到角色凭据也拿不到它。
#    缺了它只会「告警发不出去」，不该让整个护闸起不来。
PUSH_TOKEN = os.environ.get("PUSH_TOKEN", "<YOUR_PUSHPLUS_TOKEN>")

def refresh_creds():
    """到期前刷新角色凭据。返回 True 表示这次确实刷新了。
    失败时**保持原值** —— 见上面「凭据解析」一节的第 3 点。"""
    global AK, SK, STS_TOKEN, _CRED_EXP
    if not _CRED_EXP:
        return False                      # 本就不是角色模式，不刷
    if time.time() < _CRED_EXP - 1800:    # 还剩 30 分钟以上，不动
        return False
    c = _resolve_creds(verbose=True)
    if not c:
        log("⚠️ 角色凭据刷新失败，继续使用旧凭据（可能即将过期）")
        return False
    AK, SK, STS_TOKEN, _CRED_EXP = c
    log("角色凭据已刷新，有效至 %s"
        % datetime.datetime.fromtimestamp(_CRED_EXP, TZ8).strftime("%m-%d %H:%M"))
    return True
TZ8 = datetime.timezone(datetime.timedelta(hours=8))

# ⚠️ 演练开关用**命令行参数**，不用环境变量。
#    环境变量会从 /etc/conf.d/cdtguard 进来，一旦有人写进去忘了删，护闸就
#    **永远只打 tag、不停机** —— 而 tag 打上了、推送也发了，看起来像
#    "触发过了"，可能几个月都发现不了。这是把 fail-open 从"条件触发"
#    变成"配置即触发"。
#    命令行参数只能在手动调用时加上，OpenRC 的 command_args 里不会意外带上。
#    注意：--dry-run 走的是 StopInstance 自带的 DryRun（真实 API 预检、
#    会校验身份与权限，但不会真停实例），不是"跳过停机"。
DRY_RUN = "--dry-run" in sys.argv

def now8():
    return datetime.datetime.now(TZ8).strftime("%Y-%m-%d %H:%M:%S")

def now8_iso():
    return datetime.datetime.now(TZ8).isoformat(timespec="seconds")

def month_key():
    return datetime.datetime.now(TZ8).strftime("%Y-%m")

def log(m):
    print("%s %s" % (now8(), m), flush=True)
    try:
        os.fsync(sys.stdout.fileno())   # ForceStop 会硬杀 VM，必须强制落盘
    except Exception:
        pass

PUSH_FAILS = [0]   # 连续推送失败计数。模块级，供 OSS 审计透出。
                   # ⚠️ 此前 notify() 把异常全吞了，推送失败只落一行日志 ——
                   #    而整条告警链是**单点依赖 pushplus**。有了这个计数，
                   #    FC 的每日巡检就能把"护闸推送已连续失败 N 次"报出来。

def notify(title, content, timeout=15):
    # ⚠️ 临界路径（达阈值停机前）必须用**短超时**：
    #    推送在 do_stop() 之前（ForceStop 会立刻杀死进程，之后发不出去），
    #    而这里是阻塞的。pushplus 慢的时候 timeout=15 会把停机推迟最多
    #    15 秒 —— 800Mbps 下就是 1.5GB，占总配额 0.75%。
    #    停机越晚，超额窗口越大；而混合口径已经把 5% 安全缓冲吃掉了。
    try:
        b = json.dumps({"token": PUSH_TOKEN, "title": title, "content": content,
                        "template": "txt", "channel": "wechat"}).encode()
        req = urllib.request.Request("https://www.pushplus.plus/send", data=b,
              headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            log("push[%s] -> %s" % (title, r.read().decode()[:100]))
        PUSH_FAILS[0] = 0
    except Exception as e:
        PUSH_FAILS[0] += 1
        log("push ERR (连续第 %d 次) %s" % (PUSH_FAILS[0], str(e)[:150]))

def rpt(title, action, acc_bytes, extra="", fast=False):
    # ⚠️ 必须与阈值同口径（十进制 GB）—— 用 1024³ 会让「已用 186 GB / 阈值 200 GB」
    #    在真正触发时显示成「使用率 93%」，读者会以为还有 7% 余量。
    used = acc_bytes / 1e9
    body = (
        "【实例】%s (%s)\n"
        "【公网IP】%s\n"
        "【地域】%s / %s\n"
        "【已用流量】%.3f GB\n"
        "【阈值】%.1f GB ｜ 剩余 %.3f GB\n"
        "【使用率】%.1f%%\n"
        "【计费口径】共享带宽 max(入,出) 按月累计\n"
        "【动作】%s\n"
        "【时间】%s (UTC+8)"
    ) % (INST_NAME, INSTANCE_ID, EIP, REGION, ZONE, used, THRESHOLD_GB,
         THRESHOLD_GB - used, used / THRESHOLD_GB * 100.0, action, now8())
    if extra:
        body += "\n" + extra
    notify(title, body, timeout=5 if fast else 15)

def read_nic():
    with open("/proc/net/dev") as f:
        for line in f:
            s = line.strip()
            if s.startswith(IFACE + ":"):
                p = s.split(":", 1)[1].split()
                return int(p[0]), int(p[8])
    raise RuntimeError("iface not found")

def meta(path, default=None):
    # 实例元数据服务。每台 ECS 都能访问，无需任何额外权限。
    # 实测可用：instance-id / region-id / eipv4
    # ⚠️ 两个必须做的防护：
    #   1) 必须用**空 ProxyHandler** —— urllib 默认会读 http_proxy/HTTP_PROXY
    #      环境变量。哪天为别的事在 rc 环境里加了 proxy，元数据请求会被送去
    #      代理、返回错误页或超时。这个故障只在元数据这一处爆出来，极难查。
    #   2) 必须 .strip() —— 元数据返回值带 \r\n。不 strip 会让 INSTANCE_ID
    #      变成 "i-xxx\r\n"，拼进 InstanceIds JSON 后报
    #      InvalidInstanceId.NotFound —— 等于亲手制造一个静默 fail-open。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(3):
        try:
            with opener.open("http://100.100.100.200/latest/meta-data/" + path,
                             timeout=2) as r:
                v = r.read().decode().strip()
                if v:
                    return v
        except Exception:
            pass
        time.sleep(1)
    return default

_c = _resolve_creds(verbose=False)
if _c:
    AK, SK, STS_TOKEN, _CRED_EXP = _c
    log("凭据来源：实例 RAM 角色（有效至 %s）"
        % datetime.datetime.fromtimestamp(_CRED_EXP, TZ8).strftime("%m-%d %H:%M"))
else:
    # 兜底：环境变量 > 内嵌值（本仓库里是占位符，线上部署时替换为真值）。
    # ⚠️ 这里**故意不 fail-loud**。理由见脚本第 15 条设计决策：
    #    「取不到时保留内嵌值而不是拒绝启动：拒绝启动 = OpenRC 每 5 秒重启一次
    #      → 无限 crash-loop，期间完全无计量、无护闸，而且 rpt() 在 main() 里，
    #      连告警都发不出去。那是拿小风险换大风险。」
    # 实测补充：**`/etc/conf.d/<svc>` 对这个服务无效** ——
    #    本机 supervisor 是 supervise-daemon，它会把子进程环境清零；实测 python
    #    进程的 environ 只有 HOME/PATH/PWD/SHLVL/USER。所以环境变量这条路在
    #    当前部署形态下是死的，真正起作用的是内嵌值与角色。
    AK = os.environ.get("ALIYUN_AK", "<YOUR_AK_ID>")
    SK = os.environ.get("ALIYUN_SK", "<YOUR_AK_SECRET>")
    STS_TOKEN = None
    _CRED_EXP = 0.0
    log("凭据来源：内嵌值/环境变量（未取到实例 RAM 角色凭据）")


def resolve_identity():
    # 把内嵌的实例身份与**实际**身份对齐。
    #
    # 为什么必须做：INSTANCE_ID 一旦过期（实例重建后 ID 会变），停机链会
    # **静默失效**。实测证据链：
    #   DescribeInstances 查一个**不存在**的 ID -> HTTP 200 + TotalCount 0 +
    #   空数组，**不是错误** -> is_running() 返回 None 而**不抛异常** ->
    #   旧代码 `if s == "Running"` 不成立 -> 停机块被整个跳过 ->
    #   连 err_streak 的兜底推送都不触发（因为没有异常）。
    #   结果：只有一行 THRESHOLD HIT 日志每 2 秒重复，流量继续烧，全程无声。
    #
    # 降级链：元数据 -> EIP 反查 -> 内嵌值 + 强告警。
    # ⚠️ 为什么用 EIP 兜底：EIP 是唯一"重建后必须重新绑定、否则整个服务就没了"
    #    的资源，比 instance-id 稳定得多，而且元数据挂了它还能用。
    # ⚠️ 为什么取不到时保留内嵌值而不是拒绝启动：拒绝启动 = OpenRC 每 5 秒
    #    重启一次 -> 无限 crash-loop，期间完全无计量、无护闸，而且 rpt() 在
    #    main() 里，连告警都发不出去。那是拿小风险换大风险。
    # ⚠️ 身份解析与计量**解耦**：计量必须永远跑；身份只决定"停机打给谁"。
    global INSTANCE_ID, REGION, EIP
    hard = {"instance-id": INSTANCE_ID, "region-id": REGION, "eipv4": EIP}
    real = dict((k, meta(k)) for k in hard)
    src = "metadata"
    if not real["instance-id"]:
        try:
            d = rpc("DescribeEipAddresses", "2016-04-28",
                    {"RegionId": REGION, "EipAddresses": json.dumps([EIP])},
                    "vpc.aliyuncs.com")
            addrs = (d.get("EipAddresses") or {}).get("EipAddress") or []
            if addrs and addrs[0].get("InstanceId"):
                real["instance-id"] = addrs[0]["InstanceId"]
                src = "eip-reverse"
        except Exception as e:
            log("EIP 反查失败 %s" % str(e)[:100])
    drift = []
    for k in ("instance-id", "region-id", "eipv4"):
        if real[k] and real[k] != hard[k]:
            drift.append("%s: 内嵌=%s 实际=%s" % (k, hard[k], real[k]))
    if real["instance-id"]:
        INSTANCE_ID = real["instance-id"]
    if real["region-id"]:
        REGION = real["region-id"]
        apply_region(REGION)
    if real["eipv4"]:
        EIP = real["eipv4"]
    if drift:
        # ⚠️ 内嵌值与实际不一致**本身不是异常**，是实例重建后的正常现象。
        #    所以只在进程启动时告警一次（不是每轮），避免天天推。
        log("实例身份已对齐（来源=%s）: %s" % (src, "; ".join(drift)))
        rpt("CDT 守护：实例身份已自动纠正",
            "内嵌的实例身份已过期，护闸已改用实际值继续工作。",
            0, "【变更】%s\n【来源】%s" % ("; ".join(drift), src))
    elif src == "metadata":
        log("实例身份核对通过: %s" % INSTANCE_ID)
    else:
        log("⚠️ 元数据不可达，身份来源=%s，INSTANCE_ID=%s" % (src, INSTANCE_ID))
        rpt("CDT 守护：元数据服务不可达",
            "取不到实例元数据。若实例曾重建，内嵌身份可能是过期的 —— 那时达阈值不会停机。",
            0, "【INSTANCE_ID】%s" % INSTANCE_ID)
    if DRY_RUN:
        log("⚠️ DRY_RUN 已启用：停机只做 DryRun 预检，不会真的停实例")

def load_state():
    # 候选：两个本地副本 + OSS。取 acc 最大者 —— 累计量只可增不可减，
    # 取最大才不会因为某份副本陈旧/丢失而少算（少算是危险方向）。
    # OSS 那份的价值：实例**整机重建**时本地文件不存在，靠它接续累计。
    cands = []
    for p in (STATE_A, STATE_B):
        try:
            cands.append(json.load(open(p)))
        except Exception:
            pass
    try:
        o = oss_get()
        if isinstance(o, dict) and o.get("acc"):
            # OSS 的对象结构略不同：把 eff/acc 映射回来。
            # ⚠️⚠️ last_rx/last_tx 必须补上——
            #   旧代码没有这两个键，而主循环 else 分支直接取 st["last_rx"]。
            #   整机重建后本地两个 state 文件不存在 → 这份 OSS 候选胜出 →
            #   月份相同 → 进 else 分支 → **KeyError** → 被外层 except 吞掉 →
            #   每 2 秒重复。而告警条件是 err_streak == 5（只在"等于"时推一次），
            #   所以此后永远沉默。
            #   表现：rc-service status 显示 started，实际零计量、零 OSS 写入、
            #   零停机 —— 护闸看起来完全正常，实际彻底失效。
            #
            # ⚠️⚠️ 缺省值必须是 **None 而不是 0**：
            #   若给 0，主循环 `if lrx is None` 不会成立，就会走
            #   `drx = rx - 0 = rx` —— 把**整个网卡计数器**当成一轮增量，
            #   凭空多算几个 GB，可能直接误触发停机。
            #   那比 KeyError 更糟：崩溃是响的，静默多算才致命。
            #   用 None 才能让主循环走"以当前计数重新基线化"的分支。
            # ⚠️ halted 必须一起带回来 —— 它是 tag 打标失败时的**唯一兜底**。
            #    丢了这个键，整机重建后 st 里没有 halted，周期写入就会把它写成 0，
            #    FC 的两条判据（tagHalted/ossHalted）同时为假 ⇒ 把刚停下的实例拉起来。
            cands.append({"month": o.get("month"), "acc": o.get("acc", 0),
                          "eff": o.get("eff", 0), "ts": o.get("ts"),
                          "halted": o.get("halted", 0),
                          "last_rx": (int(o["last_rx"]) if o.get("last_rx") else None),
                          "last_tx": (int(o["last_tx"]) if o.get("last_tx") else None),
                          "source": "oss"})
    except Exception as e:
        print("%s OSS 回读失败(忽略，用本地副本): %s" % (now8(), str(e)[:80]), flush=True)
    best = None
    for s in cands:
        if best is None or s.get("acc", 0) > best.get("acc", 0):
            best = s
    return best

def save_state(s, sync=False):
    # sync=True 才 fsync。默认不 fsync 是为了省 I/O：
    # 硬关机后 acc 的"少算量"按字节控制（见 FSYNC_BYTES），而非按秒。
    for p in (STATE_A, STATE_B):
        try:
            d = os.path.dirname(p)
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            t = "%s.tmp.%d" % (p, os.getpid())   # PID 唯一，避免并发写入互相踩
            with open(t, "w") as f:
                json.dump(s, f)
                f.flush()
                if sync:
                    os.fsync(f.fileno())
            os.replace(t, p)
        except Exception as e:
            log("save_state ERR %s" % str(e)[:80])

def hist_push(hist, ts_ms, rx, tx, keep_ms):
    hist.append((ts_ms, rx, tx))
    cutoff = ts_ms - keep_ms
    while hist and hist[0][0] < cutoff:
        hist.pop(0)

def hist_at(hist, ts_ms):
    """返回 <= ts_ms 的最近一次网卡计数 (rx,tx)；找不到返回 None。"""
    best = None
    for h in hist:
        if h[0] <= ts_ms:
            best = h
        else:
            break
    return (best[1], best[2]) if best else None

def pct(x):
    r = urllib.parse.quote(str(x), safe="")
    return r.replace("+", "%20").replace("*", "%2A").replace("~", "~")

def rpc(action, version, params, endpoint):
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    p = {"AccessKeyId": AK, "Action": action, "Format": "JSON",
         "SignatureMethod": "HMAC-SHA1", "SignatureNonce": str(uuid.uuid4()),
         "SignatureVersion": "1.0", "Version": version, "Timestamp": ts}
    # ⚠️ STS 临时凭据必须把 SecurityToken 作为**普通参数**纳入签名
    #    （它参与排序后的 cqs，所以加在这里而不是 header）。
    #    漏了它 → 所有 RPC 调用 403，且错误信息不会告诉你缺的是 token。
    if STS_TOKEN:
        p["SecurityToken"] = STS_TOKEN
    if params:
        p.update(params)
    cqs = "&".join("%s=%s" % (pct(k), pct(p[k])) for k in sorted(p))
    sig = base64.b64encode(hmac.new((SK + "&").encode(),
          ("GET&%2F&" + pct(cqs)).encode(), hashlib.sha1).digest()).decode()
    url = "https://%s/?Signature=%s&%s" % (endpoint, pct(sig), cqs)
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read().decode())

def cdt_bytes():
    d = rpc("ListCdtInternetTraffic", "2021-08-13", {}, "cdt.aliyuncs.com")
    return sum(x.get("Traffic", 0) for x in d.get("TrafficDetails", []))

def bw_month_ex():
    # 共享带宽包的云监控指标积分 -> 本月主流量（计费侧口径，近实时）
    # 单位 bit/s；CMS 的 StartTime/EndTime 按 UTC+8 解释
    #
    # ⚠️⚠️ 时间戳语义（极易搞错）：
    #   小时级数据点标注 T，实际覆盖的是 **[T-1h, T)** —— 标注的是区间**结束**时刻。
    #   分钟级同理，标注 X 覆盖 [X-1min, X)。
    #   实测对齐证据：取该小时的分钟级积分求和，
    #   与小时级那个点的值完全相等。
    #   曾按"标注=区间开始"写成 `t + 3600000 > now 就跳过`，
    #   于是把最近一个**已走完**的整小时误判成"当前小时"丢弃，
    #   改用分钟级去估 —— 会少算，并制造出「网卡口径偏高」的假象
    #   （真实的结构性偏高约 +5%）。
    #   正确判据：**T <= now 即为已结算**。
    def fetch(metric, period, s, e):
        d = rpc("DescribeMetricList", "2019-01-01", {
            "Namespace": "acs_bandwidth_package",
            "MetricName": metric, "Period": str(period),
            "StartTime": s, "EndTime": e, "Length": "1000",
            "Dimensions": json.dumps([{"instanceId": BW_ID}])}, BW_EP)
        return json.loads(d.get("Datapoints") or "[]")

    n = datetime.datetime.now(TZ8)
    st = n.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    et = n.strftime("%Y-%m-%d %H:%M:%S")
    now_ms = int(n.timestamp() * 1000)

    # 1) 已结算的整小时：标注 T <= now 即其窗口 [T-1h, T) 已走完
    series = {}
    for metric, key in (("net_rx.rate", "rx"), ("net_tx.rate", "tx")):
        for dp in fetch(metric, 3600, st, et):
            series.setdefault(int(dp.get("timestamp", 0)), {})[key] = float(dp.get("Value", 0) or 0)
    total = 0.0
    last_end = None
    _settle_ms = BW_SETTLE_MIN * 60 * 1000
    for t, v in series.items():
        # 必须留结算余量，不能只用 t <= now（见 BW_SETTLE_MIN 注释）
        if t > now_ms - _settle_ms:
            continue
        if last_end is None or t > last_end:
            last_end = t
        total += max(v.get("rx", 0.0), v.get("tx", 0.0)) * 3600.0 / 8.0

    # 2) 尾部：最后一个已结算小时的末端 -> 现在，用分钟级补齐
    # ⚠️ 覆盖时刻 = 最新**分钟级**数据点，不是小时级标注。
    #    云监控的值是「整小时 + 分钟级尾部」拼出来的，
    #    真实覆盖到的是那几个分钟点，不是整点。
    #    曾错用小时级标注当覆盖时刻，导致桥接窗口被算成 1 小时，
    #    15 分钟的网卡历史根本回溯不到 -> 基准永远建不起来。
    cover_ts = last_end
    if last_end is not None:
        tail_from = datetime.datetime.fromtimestamp(last_end / 1000, TZ8).strftime("%Y-%m-%d %H:%M:%S")
        cur = {}
        for metric, key in (("net_rx.rate", "rx"), ("net_tx.rate", "tx")):
            a = 0.0
            for dp in fetch(metric, 60, tail_from, et):
                a += float(dp.get("Value", 0) or 0) * 60.0 / 8.0
                t = int(dp.get("timestamp", 0) or 0)
                if t > cover_ts:
                    cover_ts = t
            cur[key] = a
        total += max(cur.get("rx", 0.0), cur.get("tx", 0.0))
    # last_end = 最后一个已结算小时的标注 = 云监控**已覆盖到**的时刻
    return total, cover_ts

def bw_month_bytes():
    # 兼容旧调用点：只要总量
    return bw_month_ex()[0]

def _oss_canon_headers():
    """STS 模式下把 x-oss-security-token 纳入 CanonicalizedOSSHeaders。

    ⚠️ 只往请求里加 header、不同时改签名串，OSS 会直接拒签。
       签名串里这一行**必须以 \\n 结尾**，所以它作为独立的一段参与 join。
    """
    if STS_TOKEN:
        return "x-oss-security-token:" + STS_TOKEN
    return ""


def _oss_req_headers(date, extra=None):
    h = {"Date": date, "Authorization": ""}   # Authorization 由调用方填
    if STS_TOKEN:
        h["x-oss-security-token"] = STS_TOKEN
    if extra:
        h.update(extra)
    return h


def oss_get():
    # 读回 OSS 上的状态对象。此前 OSS 是**只写不读**的，
    # 导致实例整机重建（换机器/从镜像重开）后本地文件没了、
    # 累计值从零开始 -> 护闸少算 -> 可能超额。
    date = datetime.datetime.now(datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    parts = ["GET", "", "", date]
    ch = _oss_canon_headers()
    if ch:
        parts.append(ch)
    parts.append("/%s/%s" % (OSS_BUCKET, OSS_KEY))
    sts = "\n".join(parts)
    sig = base64.b64encode(hmac.new(SK.encode(), sts.encode(), hashlib.sha1).digest()).decode()
    hdrs = _oss_req_headers(date)
    hdrs["Authorization"] = "OSS %s:%s" % (AK, sig)
    req = urllib.request.Request(
        "https://%s.%s/%s" % (OSS_BUCKET, OSS_EP, OSS_KEY), method="GET",
        headers=hdrs)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())

def oss_put(obj):
    body = json.dumps(obj).encode()
    date = datetime.datetime.now(datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
    ct = "application/json"
    parts = ["PUT", md5, ct, date]
    ch = _oss_canon_headers()
    if ch:
        parts.append(ch)
    parts.append("/%s/%s" % (OSS_BUCKET, OSS_KEY))
    sts = "\n".join(parts)
    sig = base64.b64encode(hmac.new(SK.encode(), sts.encode(), hashlib.sha1).digest()).decode()
    hdrs = _oss_req_headers(date, {"Content-MD5": md5, "Content-Type": ct})
    hdrs["Authorization"] = "OSS %s:%s" % (AK, sig)
    req = urllib.request.Request(
        "https://%s.%s/%s" % (OSS_BUCKET, OSS_EP, OSS_KEY), data=body, method="PUT",
        headers=hdrs)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status

def tag_halted(month):
    # 给实例打停机锁标签：FC 靠它判断「是不是因为达阈值才停机」
    rpc("TagResources", "2014-05-26", {
        "RegionId": REGION,
        "ResourceType": "instance",
        "ResourceId.1": INSTANCE_ID,
        "Tag.1.Key": "cdt-halted-month",
        "Tag.1.Value": month}, "ecs.aliyuncs.com")

def is_running():
    d = rpc("DescribeInstances", "2014-05-26",
            {"RegionId": REGION, "InstanceIds": json.dumps([INSTANCE_ID])}, "ecs.aliyuncs.com")
    ins = (d.get("Instances") or {}).get("Instance") or []
    return ins[0].get("Status") if ins else None

def inst_state():
    # 返回 (Status, lock_reasons)。lock_reasons = OperationLocks.LockReason 列表。
    # ⚠️ 这是官方给的三种抢占感知方式里**最直接**的一种（官方文档查证）：
    #    官方文档「感知抢占式实例中断事件与响应」原文：
    #      * OperationLocks 返回空值   -> 实例可持续使用
    #      * LockReason == "Recycling" -> 抢占式实例被中断，处于待回收状态
    #    另有原文：「在实例被释放的**前五分钟**，云服务器ECS会发送抢占式实例中断事件」。
    #
    # 为什么选它而不是另外两条路：
    #   * 无新权限 —— 本进程本来就在调 DescribeInstances
    #     （EventBridge 推送那条要 EventBridge 规则 + FC 活着；
    #      DescribeSystemEventAttribute 那条要额外的 cms:* 权限）
    #   * 无分页/排序坑 —— 实测 DescribeSystemEventAttribute 没有实例 ID 参数、
    #     结果按时间**升序**且不给总数，PageSize=20 返回的全是最旧的事件
    #   * 无查询窗口限制 —— 实测 DescribeSystemEventAttribute 查 30 天直接
    #     ParameterInvalid，只能查 7 天内
    #   * 数据源最直接 —— 这是**实例自身的锁定状态**，不是事件总线/云监控的转述
    # 常态下该字段存在但为空：OperationLocks: {"LockReason": []}
    try:
        d = rpc("DescribeInstances", "2014-05-26",
                {"RegionId": REGION, "InstanceIds": json.dumps([INSTANCE_ID])},
                "ecs.aliyuncs.com")
        ins = (d.get("Instances") or {}).get("Instance") or []
        if not ins:
            return None, []
        locks = (ins[0].get("OperationLocks") or {}).get("LockReason") or []
        return ins[0].get("Status"), [str(x) for x in locks]
    except Exception as e:
        log("inst_state ERR %s" % str(e)[:120])
        return None, []

def preempt_check(st, eff):
    # 每 CDT_EVERY（10 分钟）调一次。检测到 Recycling 就推**一次**预警。
    # ⚠️ 用 st["preempt_alerted"] 做一次性标志：随 st 落盘故跨重启有效；
    #    跨月重建 st 时自动复位。锁解除后把标志清掉，以便下次抢占能再报。
    # ⚠️ 无法端到端验证：除非真发生一次抢占回收，否则只能验证到
    #    "字段存在且当前为空"。这一点与 EventBridge 那条一样（那条也是
    #    靠构造事件形状验证的，事件本身的真实性同样未经验证）。
    try:
        s, locks = inst_state()
    except Exception as e:
        log("preempt_check ERR %s" % str(e)[:120])
        return
    rec = any("Recycling" in x for x in locks)
    if rec and not st.get("preempt_alerted"):
        st["preempt_alerted"] = 1
        save_state(st, sync=True)
        log("⚠️ OperationLocks.LockReason=Recycling —— 实例即将被 spot 回收")
        rpt("CDT 实例即将被 spot 回收",
            "阿里云已把本实例标记为待回收（OperationLocks.LockReason=Recycling）。"
            "官方口径是提前约 5 分钟通知。",
            eff, "【锁原因】%s\n【实例状态】%s" % (", ".join(locks), s))
    elif (not rec) and st.get("preempt_alerted"):
        st.pop("preempt_alerted", None)
        save_state(st, sync=True)
        log("spot 回收锁已解除，预警标志复位")

def do_stop():
    p = {"InstanceId": INSTANCE_ID, "ForceStop": "true", "StoppedMode": "StopCharging"}
    if DRY_RUN:
        p["DryRun"] = "true"
    rpc("StopInstance", "2014-05-26", p, "ecs.aliyuncs.com")

def stop_and_report(eff):
    # 停机，并把各种结果翻译成人话。fail-safe 的核心判据：
    #   **只有「实例 ID 无效」才是真失败**，其余都算目标状态已达成。
    # 官方错误码（官方文档查证 + DryRun 实测）：
    #   404 InvalidInstanceId.NotFound  -> ID 不存在（正是身份过期时的症状）
    #   403 IncorrectInstanceStatus     -> 当前状态不支持（即已 Stopped）
    #   400 DryRunOperation             -> DryRun 预检通过，实例**未停**
    try:
        do_stop()
        log("STOP sent (ForceStop=true)%s" % ("  [DRY_RUN: 未真停]" if DRY_RUN else ""))
        return True
    except urllib.error.HTTPError as e:
        body = str()
        try:
            body = e.read().decode()
        except Exception:
            pass
        if "DryRunOperation" in body:
            log("DRY_RUN 预检通过（DryRunOperation），实例未停 —— 链路可达、身份有效、权限齐备")
            return True
        if "IncorrectInstanceStatus" in body:
            log("StopInstance 返回 IncorrectInstanceStatus，实例已处停机态，视为成功（幂等语义）")
            return True
        if "InvalidInstanceId.NotFound" in body:
            log("⚠️⚠️ StopInstance 失败：INSTANCE_ID 无效（%s）—— 配置错误！" % INSTANCE_ID)
            rpt("!! CDT 停机失败：实例 ID 无效",
                "护闸已达阈值但无法停机，因为 INSTANCE_ID 在阿里云侧不存在。"
                "流量仍在消耗，请立即人工介入。",
                eff, "【INSTANCE_ID】%s\n【返回】%s" % (INSTANCE_ID, body[:300]), fast=True)
            return False
        log("stop ERR %s" % body[:250])
        return False
    except Exception as e:
        log("stop ERR %s" % str(e)[:250])
        return False

def main():
    log("guard start iface=%s threshold=%.1fGB tz=UTC+8" % (IFACE, THRESHOLD_GB))
    resolve_identity()   # 必须在任何用到 INSTANCE_ID 的调用之前
    st0 = load_state()
    # ⚠️⚠️ **进入主循环前必须清掉 halted** —— 它是跨月自锁的另一半。
    #    理由：护闸运行在实例内部，**它能跑起来就说明实例是开着的** ——
    #    也就是说上一次的停机锁已经不生效了（跨月重置，或有人手工解锁后拉起）。
    #    若不清，`st["halted"]` 会带着本月月份被每 60 秒重新写进 OSS，
    #    FC 的判据 `ossHalted && month===cur` 就一直为真 ⇒
    #    **哪怕之后因为抢占/故障真的停了，FC 也会整月拒绝拉起**，
    #    推送还写着「等待次月 1 日额度重置」—— 而额度根本没超。
    #    清掉之后，下一次阈值判定若仍超限会**立刻重新置位**，保护不弱。
    if st0 and st0.pop("halted", None):
        save_state(st0, sync=True)
        log("已清除本月的 halted 标志（护闸在运行 ⇒ 上次停机锁已不生效）")
    rpt("CDT 守护已启动", "守护进程就绪，开始实时计量与自动停机保护",
        (st0 or {}).get("acc", 0))
    st = st0
    cdt_floor = 0
    bw_bytes = 0
    bw_acc = 0          # 取 bw_bytes 那一刻的 acc 快照（跨新鲜度比较的修正）
    bw_last_alert = 0
    unsynced = 0
    hist = []            # 网卡采样历史 [(ts_ms, rx, tx)]
    bw_base = 0          # 云监控基准值（字节）
    bw_base_ts = 0       # 该基准已覆盖到的时刻 ms
    bw_base_rx = 0       # 该时刻的网卡累计计数
    bw_base_tx = 0
    bw_avail = False     # 基准是否可用（历史不足/计数器回退时为 False）
    bw_lag_s = None      # 实测的云监控滞后秒数
    floor_prev = 0.0        # 跨月前最后一次 cdt_floor 读数（上月终值）
    reset_confirmed = True  # 本月额度是否已确认重置（非跨月恒 True）
    rollover_ts = 0.0       # 本次跨月时刻
    # ── 基准的持久化恢复 ──────────────────────────────────────────
    # 基准 = (云监控值, 覆盖时刻, 该时刻的网卡 rx/tx) 四个数。
    # 必须跨**进程**重启存活：否则每次 restart 都要等满一个刷新周期
    # 才能重建，这期间只能退回纯网卡口径（安全但不够准）。
    # ⚠️ 网卡计数在**虚拟机重启**后归零，故恢复时必须校验 rx/tx >= 基准；
    #    不满足则作废（退回纯网卡），避免桥接算出负数或错值。
    if st0 and st0.get("month") == month_key():
        _b = int(st0.get("hb_base", 0) or 0)
        _ts = int(st0.get("hb_ts", 0) or 0)
        _brx = int(st0.get("hb_rx", 0) or 0)
        _btx = int(st0.get("hb_tx", 0) or 0)
        if _b > 0 and _ts > 0:
            try:
                _rx0, _tx0 = read_nic()
                if _rx0 >= _brx and _tx0 >= _btx:
                    bw_base, bw_base_ts = _b, _ts
                    bw_base_rx, bw_base_tx = _brx, _btx
                    bw_avail = True
                    bw_lag_s = max(0.0, time.time() - _ts / 1000.0)
                    log("HYBRID 基准已从本地恢复: base=%.4fGB 覆盖=%s"
                        % (_b / 1073741824.0,
                           datetime.datetime.fromtimestamp(_ts / 1000, TZ8).strftime("%H:%M")))
                else:
                    log("HYBRID 本地基准失效（网卡计数已归零，疑似虚拟机重启）")
            except Exception as e:
                log("HYBRID 基准恢复失败 %s" % str(e)[:80])
    # 告警限流时间持久化：否则每次重启都会把 1 小时限流清零，
    # 表现为重启后立刻又推一条
    if st0:
        bw_last_alert = float(st0.get("bw_alert_ts", 0) or 0)
    # 启动即拉一次兜底值与计费侧值，避免重启后 10 分钟空窗
    try:
        cdt_floor = cdt_bytes()
        log("cdt_floor(init)=%.4fGB" % (cdt_floor / 1073741824.0))
    except Exception as e:
        log("cdt init ERR %s" % str(e)[:120])
    try:
        bw_bytes = bw_month_bytes()
        bw_acc = (st or {}).get("acc", 0)   # 与 bw_bytes 同一时刻
        log("bw_month(init)=%.4fGB (计费侧主流量)" % (bw_bytes / 1073741824.0))
    except Exception as e:
        log("bw init ERR %s" % str(e)[:150])
    i = 0
    err_streak = 0
    while True:
        try:
            rx, tx = read_nic()
            mk = month_key()
            if (st is None) or (st.get("month") != mk):
                if st is not None and st.get("month") != mk:
                    rpt("CDT 月度重置", "月份切至 %s，累计计量归零" % mk, 0)
                    # ⚠️ 只有**真跨月**才进入"重置待确认"。首次启动 st is None，
                    #    没有"上月终值"可比，若也进入会导致开机 6 小时内不停机。
                    floor_prev = cdt_floor
                    reset_confirmed = False
                    rollover_ts = time.time()
                    log("跨月进入「重置待确认」：上月终值 cdt_floor=%.4fGB，"
                        "观测到下跌前不因 cdt_floor 触发停机"
                        % (floor_prev / 1073741824.0))
                st = {"month": mk, "last_rx": rx, "last_tx": tx, "acc": 0}
                bw_avail = False   # 跨月：云监控基准一并作废，待重新建立
                # ⚠️ bw_acc 必须跟着归零：
                #   bw_acc 只在 `i % CDT_EVERY == 0` 那一拍才刷新（见下方
                #   `bw_acc = st["acc"]`）。跨月分支换掉了整个 st 却不管它，
                #   于是此后最多 10 分钟内，dev 会拿**上个月的 acc** 去比
                #   **新月的 bw_bytes**；若跨月恰好落在 CDT_EVERY 那一拍
                #   （概率 1/300），bw_acc 直接是 0 -> dev = -100%
                #   -> 误报"网卡口径低于计费侧"—— 那是**危险方向**的假警报。
                bw_acc = 0
                # ⚠️ 不要把 cdt_floor 清零：
                #    清零等于**假设**"额度已在 00:00 重置"。若真实重置更晚，
                #    清零会让护闸在月首那段时间误以为额度已刷新 → 放松保护。
                #    改为立刻向 CDT API 取一次权威值：API 若已进新月份，它自然
                #    返回 ~0；若还没进，它仍返回上月累计 → 护闸保持戒备。
                try:
                    cdt_floor = cdt_bytes()
                    log("月度重置：cdt_floor 取自 CDT API = %.4fGB（未清零，避免假设重置时刻）"
                        % (cdt_floor / 1073741824.0))
                except Exception as _e:
                    log("月度重置：cdt_floor 取数失败(%s)，保留旧值 %.4fGB"
                        % (str(_e)[:60], cdt_floor / 1073741824.0))
                log("baseline %s rx=%d tx=%d" % (mk, rx, tx))
            else:
                # ⚠️ 用 .get() 并处理缺失（见 load_state 注释）：
                #   缺失 = 整机重建后从 OSS 恢复 → 以当前 rx/tx 作新基线、
                #   本轮增量记 0，既不丢累计也不虚增。
                #   回退（rx < last_rx，网卡重启过）保留原行为：drx = rx。
                lrx = st.get("last_rx")
                ltx = st.get("last_tx")
                if lrx is None or ltx is None:
                    log("last_rx/last_tx 缺失（疑似整机重建后从 OSS 恢复），"
                        "以当前网卡计数重新基线化，保留 acc=%.4fGB"
                        % (st.get("acc", 0) / 1073741824.0))
                    st["last_rx"], st["last_tx"] = rx, tx
                    drx = dtx = 0
                else:
                    drx = rx - lrx if rx >= lrx else rx
                    dtx = tx - ltx if tx >= ltx else tx
                st["acc"] += max(drx, dtx)
                unsynced += max(drx, dtx)
                st["last_rx"], st["last_tx"] = rx, tx

            # 网卡采样进历史环形缓冲 —— 供云监控桥接回溯「覆盖时刻 X」用
            hist_push(hist, int(time.time() * 1000), rx, tx, HIST_MINUTES * 60000)

            i += 1
            if i % CDT_EVERY == 0:
                # 角色临时凭据到期前刷新（内部自带 30 分钟余量判断，未到期直接返回）。
                # 放在这个 10 分钟的周期上：余量远大于刷新所需。
                # ⚠️ 刷新失败不抛 —— 抛出去会被外层吞掉，反而看不出凭据出过问题。
                try:
                    refresh_creds()
                except Exception as e:
                    log("凭据刷新异常 %s" % str(e)[:120])
                try:
                    cdt_floor = cdt_bytes()
                    log("cdt_floor=%.4fGB" % (cdt_floor / 1073741824.0))
                    if (not reset_confirmed) and cdt_floor < floor_prev:
                        reset_confirmed = True
                        log("月度重置已确认：cdt_floor 由 %.4fGB 跌至 %.4fGB"
                            % (floor_prev / 1073741824.0, cdt_floor / 1073741824.0))
                except Exception as e:
                    log("cdt ERR %s" % str(e)[:120])
                if (not reset_confirmed) and rollover_ts and (time.time() - rollover_ts) > ROLLOVER_GRACE_S:
                    reset_confirmed = True
                    log("⚠️ 跨月后 %.0f 小时仍未见 cdt_floor 下跌，"
                        "判定额度未重置，恢复正常停机判据"
                        % (ROLLOVER_GRACE_S / 3600.0))
                    rpt("⚠️ CDT 跨月额度疑似未重置",
                        "跨月已超过 %.0f 小时，CDT 累计值始终未下跌。护闸已恢复"
                        "常规停机判据 —— 若额度确实未重置，这段时间的流量会算在"
                        "上月已耗尽的额度上。" % (ROLLOVER_GRACE_S / 3600.0), cdt_floor)
                try:
                    bw_bytes, cover_ts = bw_month_ex()
                    bw_acc = st["acc"]
                    log("bw_month=%.4fGB (计费侧主流量)" % (bw_bytes / 1073741824.0))
                    # 建立混合口径基准：以云监控为准，并回溯它**覆盖到**的时刻 X
                    hit = hist_at(hist, cover_ts) if cover_ts else None
                    if hit:
                        bw_base, bw_base_ts = bw_bytes, cover_ts
                        bw_base_rx, bw_base_tx = hit
                        bw_avail = True
                        bw_lag_s = max(0.0, time.time() - cover_ts / 1000.0)
                        st["hb_base"] = bw_base
                        st["hb_ts"] = bw_base_ts
                        st["hb_rx"] = bw_base_rx
                        st["hb_tx"] = bw_base_tx
                        save_state(st, sync=True)   # 基准必须落盘
                        hy = bw_base + max(rx - bw_base_rx, tx - bw_base_tx)
                        log("HYBRID base=%.4fGB lag=%.1fs | 纯网卡acc=%.4fGB 混合=%.4fGB 差=%+.2f%%"
                            % (bw_base / 1073741824.0, bw_lag_s, st["acc"] / 1073741824.0,
                               hy / 1073741824.0,
                               (st["acc"] - hy) * 100.0 / hy if hy else 0.0))
                    else:
                        # ⚠️ 不要在这里 bw_avail = False：
                        # 旧基准依然有效，桥接窗口只是变长了一点。
                        # 作废它会让闸门无谓地退回纯网卡口径。
                        if bw_avail:
                            log("HYBRID 本次无历史可回溯，沿用旧基准 base=%.4fGB"
                                % (bw_base / 1073741824.0))
                        else:
                            log("HYBRID 无基准可用(需 %d 分钟历史)，回退纯网卡口径" % HIST_MINUTES)
                except Exception as e:
                    log("bw ERR %s" % str(e)[:150])
                # 抢占回收预警（挂在同一个 10 分钟周期上，1 次 API 调用）
                preempt_check(st, eff)

            # 混合口径：云监控基准 + 网卡自「覆盖时刻 X」以来的增量
            # ⚠️ 计数器回退(rx/tx 小于基准)说明网卡重启过，基准失效，必须回退
            eff_hybrid = None
            if bw_avail and bw_base > 0 and rx >= bw_base_rx and tx >= bw_base_tx:
                eff_hybrid = bw_base + max(rx - bw_base_rx, tx - bw_base_tx)
            # 只有显式启用且基准可用时，混合口径才接管闸门；
            # 否则回退纯网卡口径（偏高约 5%，方向安全）
            if HYBRID_ENABLED and eff_hybrid is not None:
                # ⚠️ 必须再与 cdt_floor 取 max：
                #    eff_hybrid 只估算【共享带宽】那一份，而 200GB 是
                #    **账号级**额度 —— ListCdtInternetTraffic 实测按产品拆解为
                #    （各产品用量之和 = 该组 Traffic 值），
                #    合计才是真正被限的那个数。cdt_floor 是该 API 的**本月累计**
                #    （权威口径，只是有延迟），取 max 保证**绝不少算**。
                #    🔴 更关键：这一层让护闸**不再依赖"额度在月首 00:00 重置"
                #    这个假设** —— 若真实重置时刻晚于 00:00，cdt_floor 仍是上月
                #    的累计值，max 会让 eff 保持高位，护闸照样会停。
                #    没有这一层，月首那段时间护闸会误以为额度已刷新而放松。
                eff = max(eff_hybrid, cdt_floor)
            else:
                eff = max(st["acc"], cdt_floor)
            st["ts"] = now8_iso()
            st["eff"] = eff
            do_sync = (unsynced >= FSYNC_BYTES) or (i % FSYNC_ITERS == 0)
            save_state(st, sync=do_sync)
            if do_sync:
                unsynced = 0

            if i % SYNC_EVERY == 0:
                try:
                    oss_put({"month": st["month"], "acc": st["acc"], "cdt_floor": cdt_floor,
                             "eff": eff, "rx": rx, "tx": tx, "ts": st["ts"],
                             "last_rx": st.get("last_rx", 0),
                             "last_tx": st.get("last_tx", 0),
                             "push_fail_streak": PUSH_FAILS[0],
                             "halted": st.get("halted", 0),
                             "threshold_gb": THRESHOLD_GB,
                             "bw_bytes": bw_bytes,
                             "dev_pct": (round((bw_acc - bw_bytes) * 100.0 / bw_bytes, 2)
                                         if bw_bytes else None),
                             "eff_hybrid": (round(eff_hybrid / 1073741824.0, 4)
                                              if eff_hybrid is not None else None),
                             "hybrid_enabled": 1 if HYBRID_ENABLED else 0,
                             "hybrid_avail": 1 if bw_avail else 0,
                             "bw_lag_s": (round(bw_lag_s, 1) if bw_lag_s is not None else None),
                             "bw_base_gb": round(bw_base / 1073741824.0, 4)})
                except Exception as e:
                    log("oss ERR %s" % str(e)[:120])

            # 必须用 bw_acc 快照，不能用实时 st[acc]：实时 acc 每 2 秒涨，
            # bw_bytes 每 600 秒才更新，拿实时值比会让偏差随 bw 变旧而单调放大。
            # ⚠️ 还要要求 bw_acc > 0：跨月归零、或冷启动（无状态可恢复）时
            #    bw_acc 是 0 而 bw_bytes 仍非 0，会算出 dev≈-100% 的**危险方向**假警报。
            #    没有有效快照就不该判偏差。
            if bw_bytes > 0 and bw_acc > 0:
                dev = (bw_acc - bw_bytes) * 100.0 / bw_bytes
                if (dev < BW_UNDER_PCT or dev > BW_OVER_PCT) and (time.time() - bw_last_alert) > 3600:
                    bw_last_alert = time.time()
                    st["bw_alert_ts"] = int(bw_last_alert)
                    save_state(st, sync=True)
                    if dev < 0:
                        rpt("!! CDT 网卡口径低于计费侧",
                            "网卡口径比计费侧低 %.1f%%，说明可能少算流量，有超额风险" % (-dev), bw_acc,
                            "【计费侧主流量】%.4f GB" % (bw_bytes / 1073741824.0))
                    else:
                        rpt("!! CDT 网卡口径异常偏高",
                            "网卡口径比计费侧高 %.1f%%，超出结构性偏高的合理范围，疑计数有误" % dev, bw_acc,
                            "【计费侧主流量】%.4f GB" % (bw_bytes / 1073741824.0))
                    log("DEV ALERT %+.1f%% acc=%.4fGB bw=%.4fGB" % (
                        dev, bw_acc / 1073741824.0, bw_bytes / 1073741824.0))

            # ⚠️⚠️ 阈值比较用**十进制 GB**（1e9），不是 GiB（1024³）。
            #    免费额度是按十进制 GB 计的（控制台/账单口径：200 GB = 200e9 字节）。
            #    若在这里用 1024³，停机点会落到 200 GiB = 214.7e9 字节 ——
            #    **白超 14.7 GB 才停**，而设计上它是「等于额度、没有安全余量」。
            #    （日志里别处的 "GB" 仍是 1024³ 的显示约定，不影响判据。）
            gb = eff / 1e9
            if gb >= WARN_GB and not st.get("warned"):
                st["warned"] = 1
                save_state(st, sync=True)
                log("WARN %.3fGB (剩 %.3fGB)" % (gb, THRESHOLD_GB - gb))
                rpt("CDT 流量预警（%.0f GB）" % WARN_GB,
                    "已用 %.3f GB，距自动停机阈值仅剩 %.3f GB" % (gb, THRESHOLD_GB - gb), eff)
            # ── 跨月"重置未确认"保护─────────────────────
            # 若只有 cdt_floor（可能是上月残留）顶到阈值、而本月自身用量
            # eff_hybrid 远未到，则**不停机、不打 tag** —— 打 tag 会写入新月份，
            # FC 据此整月拒绝拉起，实例被钉死到次月 1 日。
            _thr_bytes = int(THRESHOLD_GB * 1e9)   # 同上：十进制 GB
            # ⚠️ 「本月自身用量」不能只看 eff_hybrid —— 它在跨月后与重启后会
            #    **有一段时间是 None**（混合基准要等下一次云监控刷新才重建）。
            #    若把 None 当成「本月用量未确认」而进入保持期，那么在这
            #    10~25 分钟的窗口里，即使 st["acc"]（本月自己的网卡累计）
            #    已远超阈值，护闸也**不会停机** —— 保持期反而制造了一个盲区。
            #    而 st["acc"] 本身就是合法的「本月用量」：它在跨月时被重置。
            #    在内地 20GB 池上这个窗口尤其危险（几分钟就能攒出几十 GB）。
            _own_bytes = max(eff_hybrid or 0, st["acc"])
            _rollover_hold = ((not reset_confirmed)
                              and _own_bytes < _thr_bytes)
            if gb >= THRESHOLD_GB and _rollover_hold:
                if not st.get("rollover_hold_alerted"):
                    st["rollover_hold_alerted"] = 1
                    save_state(st, sync=True)
                    rpt("⚠️ CDT 跨月疑似未重置，已暂停自动停机",
                        "eff 被 cdt_floor（疑似上月残留 %.4fGB）顶到阈值，但本月"
                        "自身用量未到。为避免打上**新月份**停机锁导致整月无法"
                        "拉起，本次不停机 —— 等额度重置确认。"
                        % (cdt_floor / 1073741824.0), eff, fast=True)
                    # ⚠️ 日志必须放在这个 once-only 块**内**：本判据成立后会**持续**
                    #    成立（直到重置确认或宽限期到），放外面会每 2 秒刷一行，
                    #    最多 6 小时 ≈ 上万行，把 logcap 保留的历史全冲掉。
                    log("ROLLOVER HOLD eff=%.3fGB（重置未确认，仅 cdt_floor 到阈值，暂停停机）" % gb)
            if gb >= THRESHOLD_GB and not _rollover_hold:
                # ⚠️ 阈值告警必须**无条件**发、且**只发一次**。
                #    旧代码把 rpt() 嵌在 `if s == "Running"` 里面 —— 一旦
                #    is_running() 因身份过期返回 None，连告警都被一起吞掉，
                #    整个失败完全静默。这是必须补上的一半。
                #    一次性标志随 st 落盘故跨重启有效；跨月重建 st 时自动复位。
                #    fast=True：临界路径用短超时，别让推送把停机推迟。
                if not st.get("thr_alerted"):
                    st["thr_alerted"] = 1
                    save_state(st, sync=True)
                    rpt("CDT 流量达阈值（%.0f GB）" % THRESHOLD_GB,
                        "已用 %.3f GB，护闸正在执行停机保护。" % gb, eff, fast=True)
                log("THRESHOLD HIT eff=%.3fGB" % gb)
                # ── 为什么这里**没有** is_running() 状态判断 ──────────────
                # 旧代码是 `s = is_running(); if s == "Running":` —— 只有确认
                # "运行中"才停机。那是一个 fail-open：
                #   ID 过期时 is_running() 查的是**旧实例**。旧实例若处于
                #   Stopped（节省停机模式会留着它），s == "Stopped" ->
                #   整个停机块被跳过 -> 流量继续烧。
                # 根本性的认识：**护闸运行在这个实例内部，代码能执行 = 实例
                #   必然 Running。** 状态查询在语义上是多余的，它只能是
                #   "省一次 API 调用"的优化，**绝不能当安全闸门**。
                # 所以直接删掉判断，无条件走完整路径。停机依据始终是
                # eff >= 阈值（本地网卡 + 计费侧口径），与实例状态无关。
                log("THRESHOLD HIT eff=%.3fGB -> STOP" % gb)
                # ⚠️ 先置 halted 标志，再落盘 —— 这个字段的语义是
                #    **「护闸确实执行了停机」**，不是「eff 越过了阈值」。
                #    两者在跨月「重置未确认」保持期里会分叉（见上面 _rollover_hold）：
                #    那时 eff 被 cdt_floor 顶到阈值、但护闸**故意不停机**。
                #    若周期写入按 eff 算 halted，它会把 halted=1 + 新月份写进 OSS，
                #    而 FC 的判据正是 ossHalted && month===cur ——
                #    于是「靠不停机来避免自锁」的保护，被**兜底那条路**原样绕过：
                #    实例一旦真的停了，FC 整月拒绝拉起，推送还写着「等额度重置」。
                if not DRY_RUN:
                    # ⚠️ 演练绝不能打真实停机锁：打了 tag / 写了 halted=1 而实例还在跑，
                    #    本月剩余时间 FC 就再也不会拉起它 —— 而演练的整个卖点就是「无副作用」。
                    st["halted"] = 1
                save_state(st, sync=True)   # 停机前确保计数已落盘
                try:
                    # ⚠️ tag 是 FC 的**权威**判据（tagVal === cur），比 OSS 那条更硬。
                    #    演练打上它，本月剩余时间 FC 就再也不会拉起实例 —— 必须一起挡住。
                    if DRY_RUN:
                        log("DRY_RUN 模式：跳过 tag_halted（不打真实停机锁）")
                    else:
                        tag_halted(st["month"])
                    log("tagged cdt-halted-month=%s" % st["month"])
                except Exception as e:
                    log("tag ERR %s" % str(e)[:150])
                # 必须在 do_stop() 之前推送：ForceStop 会立刻杀死本进程，
                # 停机之后的任何代码都不会执行（这一点曾被实际验证过）。
                # ⚠️ 必须在 do_stop() 之前把 halted=1 写进 OSS。
                # OSS 对象平时每 60 秒才写一次，达阈值那一刻它的 halted 仍是 0
                # （上一次写时 eff 还低于阈值）。若不补这一笔，
                # 一旦 tag_halted() 失败，FC 的两条判据 tagHalted/ossHalted 全为 false，
                # 会把刚停下的实例重新拉起继续烧流量 —— 兜底形同虚设。
                try:
                    # 字段与周期写入保持一致，避免把这笔紧急记录
                    # 变成精简 payload 而丢掉审计信息
                    oss_put({"month": st["month"], "acc": st["acc"],
                             "cdt_floor": cdt_floor, "eff": eff, "rx": rx, "tx": tx,
                             "ts": st["ts"],
                             # 演练不得写真实停机锁（写了 FC 会整月拒绝拉起）
                             "halted": (0 if DRY_RUN else 1),
                             "last_rx": st.get("last_rx", 0),
                             "last_tx": st.get("last_tx", 0),
                             "push_fail_streak": PUSH_FAILS[0],
                             "threshold_gb": THRESHOLD_GB, "bw_bytes": bw_bytes,
                             "dev_pct": (round((bw_acc - bw_bytes) * 100.0 / bw_bytes, 2)
                                         if bw_bytes else None),
                             "eff_hybrid": (round(eff_hybrid / 1073741824.0, 4)
                                              if eff_hybrid is not None else None),
                             "hybrid_enabled": 1 if HYBRID_ENABLED else 0,
                             "hybrid_avail": 1 if bw_avail else 0,
                             "bw_lag_s": (round(bw_lag_s, 1) if bw_lag_s is not None else None),
                             "bw_base_gb": round(bw_base / 1073741824.0, 4),
                             "reason": "threshold-hit"})
                    log("oss halted=1 written BEFORE stop")
                except Exception as e:
                    log("oss pre-stop ERR %s" % str(e)[:150])
                # （原先这里有一笔 rpt("...正在强制停机")，已上移为
                #   无条件的一次性告警，见上面的 st["thr_alerted"] 块，
                #   避免达阈值后每 2 秒重复推送。）
                stop_and_report(eff)
                # 下面这段只在「停机失败、本进程还活着」时才有机会执行
                # ⚠️⚠️ `s2 = s` 是**悬空引用**：
                #   `s` 原本来自已被删除的 `s = is_running()`，删那个判断时
                #   把它落下了。main() 里**没有任何地方**给 `s` 赋值
                #   -> 执行到这一行必抛 NameError。
                #   而这一行只在「停机没能把本进程干掉」时才到得了
                #   （DRY_RUN、或停机失败）—— 那恰恰是**最需要这段核验**的场景。
                #   后果是双重的：
                #     ① "停机未生效"核验（20x3s 轮询 + 人工介入告警）
                #        **从未真正执行过**；
                #     ② 末尾的 time.sleep(600) 退避被跳过 -> 异常冒到外层
                #        except -> 只 sleep(INTERVAL)=2s -> 每 2 秒重跑整个
                #        阈值块：tag_halted + oss_put + StopInstance 各
                #        30 次/分钟，无限持续。
                #   ⚠️ 真实停机成功时 ForceStop 会立刻杀掉本进程，到不了这里，
                #      所以这个 bug 只在"停不掉"时暴露 —— 最不该哑火处哑火。
                if DRY_RUN:
                    log("DRY_RUN 模式：跳过停机后置核验（本就没打算真停）")
                else:
                    s2 = None
                    for _ in range(20):
                        time.sleep(3)
                        try:
                            s2 = is_running()
                        except Exception:
                            continue
                        if s2 in ("Stopped", "Stopping"):
                            break
                    if s2 not in ("Stopped", "Stopping"):
                        rpt("!! CDT 停机未生效 流量仍在消耗",
                            "StopInstance 已下发但实例仍为 %s，请立即人工介入" % s2, eff)
                time.sleep(600)
            err_streak = 0
        except Exception as e:
            err_streak += 1
            log("ERR(%d) %s" % (err_streak, str(e)[:200]))
            # ⚠️ 旧代码是 `if err_streak == 5` —— **只在恰好等于 5 时推一次**，
            #   之后无论失败多久都永远沉默。而"持续失败"恰恰是最需要被知道的
            #   状态（例如重建后 KeyError 卡死：护闸每 2 秒抛一次异常，
            #   但只在第 5 次推了一条，此后彻底无声）。改成周期重推。
            if err_streak >= 5 and (err_streak - 5) % 300 == 0:
                rpt("CDT 守护异常", "连续 %d 次采集出错" % err_streak,
                    (st or {}).get("acc", 0), "【错误】%s" % str(e)[:200])
        time.sleep(INTERVAL)

# ═══════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════
# 验证状态
# ═══════════════════════════════════════════════════════════════════════
# ✅ 已用桩 / 单测覆盖：
#      · 跨月自锁修复（5 个用例：「上月停机锁 + 本月」旧 = true(bug) → 新 = false）
#      · load_state 缺 last_rx 时走「重新基线化」，acc 不被虚增
#        （**第一版给缺省值 0 是错的** —— 会走 drx = rx，把整个网卡计数器当增量）
#      · StopInstance 的 DryRun 语义：HTTP 400 + Code = DryRunOperation，实例保持 Running
#      · meta() 返回值已 .strip()（元数据带尾部空白，不 strip 会让实例 ID 带进去）
#      · resolve_identity() 身份核对通过
#
# ✅ 停机路径已走**真实 API** 的 DryRun 预检，顺序为：
#      WARN → THRESHOLD → tag → 补写 OSS halted=1 → push → do_stop(DryRun)
#      实例始终 Running —— **「真的停机」这一步没有真实发生过**。
#
# 🔴 **未验证：真实的超额停机 + FC 拉起这条完整链路。**
#      要验证它必须让实例真的进入 Stopped。**不要为此在无人值守时压低阈值** ——
#      见下面那条死锁警告。想演练就用 `--dry-run`。
# ═══════════════════════════════════════════════════════════════════════

# ⚠️⚠️ 演练的死锁风险（**压低阈值做演练前必读**）
#     压低阈值演练后，**必须把阈值改回原值再启动实例**。否则实例一开机、
#     护闸一跑，会因为 acc 已超过演练阈值而**立即再次停机**；
#     而实例停止后 SSH 断开，就再也改不了文件 —— 死锁。
#
#     🟢 **用 `--dry-run` 就不存在这个问题**：它走 StopInstance 自带的 DryRun
#        预检，实例根本不会停，SSH 一直在。**新演练一律用 `--dry-run`。**
#
#     以下是无 `--dry-run` 时可用的解锁流程，**仅作故障排查参考**：
#       不可靠的做法：
#         * `nohup/setsid + reboot`     → SSH 会话关闭会回收整个进程树，不生效
#         * 抢在护闸首次判定前 SSH 进去 → 护闸启动初判只要几秒，sshd 未必更早可用
#       可用的解法 —— 在 OSS 上放「月份不符」的诱饵对象：
#         护闸 load_state() 取本地双文件与 OSS 三者 acc 的最大者；
#         写 {"month": "<上个月>", "acc": <远大于本地>} → 它被选中，但 main()
#         发现 month 不符 → 走跨月重置分支 → acc 与 cdt_floor 双双归零 →
#         eff = 0 → 绝不触发阈值，于是可以从容 SSH 进去改文件。
#         代价：本地累计被清零，事后必须手动恢复。
#       ⚠️ **诱饵是未纳管的后门**（见第 22 条），用完必须显式删除并验证。
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════
# OpenRC 服务定义
# ═══════════════════════════════════════════════════════════════════════
# ⚠️⚠️ **服务定义不在本文件里，它是一个独立文件：`scripts/cdtguard.initd`。**
#     本仓库早期版本把它**只以注释形式**放在这里 —— 读者按文档走会卡住
#     （`rc-service cdtguard status` 报「service not found」，部署脚本的
#      验收步骤整段失败）。已抽出为真实文件，那份才是唯一权威。
#     部署：见 `scripts/deploy-cdtguard.sh` 的 STEP 1.5（自动安装）。
#     为什么那些选择是那样定的（supervise-daemon 清空环境变量、
#     `-u` 关缓冲、两个日志指向同一文件）—— 都写在 `cdtguard.initd` 里。
#
# 另需 /etc/conf.d/cdtguard：
#   rc_ulimit="-n 65535"      ← **可选**。守护只用少量连接，
#                               不设也能跑；设它是为了留余量。
#
# 常用命令：
#   rc-service cdtguard status | restart
#   tail -f /var/log/cdtguard.log
#
# ── 改这个文件的安全动线（务必照做）──────────────────────────────────
#   1) 改动先在**副本**上写：/root/cdtguard.py.new
#   2) python3 -m py_compile /root/cdtguard.py.new      ← 校验不过就别往下走
#   3) cp -p /root/cdtguard.py /root/cdtguard.py.bak.$(date +%Y%m%d-%H%M%S)
#   4) 用 `--dry-run` 在副本上验证阈值路径真的被触达（不会真停机）
#   5) 覆盖并 rc-service cdtguard restart
#   6) 观察日志：应出现「实例身份核对通过」+「HYBRID 基准已从本地恢复」
#   ⚠️ 重启会中断计量约 10 秒（重启后立即重新载入基准，不丢累计）
#   ⚠️ 排查进程时不要用 `pkill -f cdtguard` —— 当前 shell 的 cmdline 里
#      就含这个字符串，会把自己杀掉。按 /proc/<pid>/exe 识别。
#
# ── 回滚 ─────────────────────────────────────────────────────────────
#   cp -p /root/cdtguard.py.bak.<时间戳> /root/cdtguard.py
#   rc-service cdtguard restart
# ═══════════════════════════════════════════════════════════════════════
