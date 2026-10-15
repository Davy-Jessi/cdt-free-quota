'use strict';
// cdt-keeper FC —— 判断"实例该不该被拉起"
//
// 触发（三个，均由 FC 侧维护）：
//   ① `daily-0005-cst`      timer       `0 5 16 * * *`（= UTC+8 每日 00:05）payload {}
//   ② `ecs-statechange`     eventbridge filter type =
//        ["ecs:Instance:StateChange", "ecs:Instance:PreemptibleInstanceInterruption"]
//      ⚠️ EventBridge target 端点里的伪服务名 `services/__FC3` —— FC 3.0 没有 service
//         概念，但这个占位符是必需的；直接用 `...:functions/<name>` 会被拒。
//   ③ `guard-liveness-8min` timer       `0 */8 * * * *` payload {"liveness": 1}
//      dead-man's switch：每 8 分钟叫醒 FC 检查护闸是否还活着。
//
// 决策顺序（自上而下，先命中先返回）：
//   ⓪ isPreempt          → 抢占预警通知（不做任何操作）
//   ① s === null         → fail-loud：查不到实例（ID 可能已过期），推送告警
//   ② s !== 'Stopped'    → noop；若 st.ts 超 STALE_MIN 分钟 → 推「护闸失联」
//                          （存活巡检在这一层被静默跳过，见 isLiveness）
//   ③ halted             → skip-halted（本月已因达阈值停机，绝不拉起）
//                          ⚠️ 存活巡检下**不推送**（稳态，见 pushThrottled）
//   ④ 熔断：1 小时内已拉起 >= LOOP_MAX 次 → 拒绝拉起并告警
//   ⑤ 拉起              → start-under-threshold / start-failed
//
// ⚠️ **所有会反复出现的告警都走 `pushThrottled`（1 小时冷却）。**
//    判据不变、`action` 不变、`last-run.json` 照常记录 —— 少掉的只是重复通知。
//    为什么必须有：存活巡检 8 分钟一次，没有冷却就是约 180 条/天。
//    ⚠️ 冷却状态**三层存储**（OSS + 模块内存 + ECS 实例 Tag）：缺任何一层都有洞。
//       最需要冷却的「OSS 读不到」场景恰好读不到 OSS，而本函数未开预留实例、
//       8 分钟一次很可能落在冷实例上 —— 所以必须有一条**与 OSS 不同故障域**的
//       持久路径，即 ECS Tag（读它是免费的：`instInfo()` 本来就拉回了全部 Tag）。
//       实测过前两层的洞，详见 `alertAllowed` 上方那段。
//    ⚠️ 一次性/低频的告警（每日巡检、拉起成功、spot 预警、FC 异常）
//       仍用裸 `push()` —— 它们本来就不会每 8 分钟重复，加冷却反而可能
//       把一次真实事件吞掉。
//
// ⚠️⚠️ 修复（详见本文件内的注释）：
//   A) `ossHalted` 必须**绑定月份**。旧写法 `!!st.halted` 只看布尔 —— 而护闸
//      达阈值停机后会随实例一起消失，OSS 对象永久冻结在 halted=1；次月 1 日
//      FC 看到 tag 是上月的（tagHalted=false）但 ossHalted 仍 true ->
//      **整月拒绝拉起，实例再也回不来**，而推送还写着"等待次月额度重置"。
//   B) `instInfo()` 查不到实例时**必须 fail-loud**。旧代码静默返回 status:null，
//      让 decide() 走 `s !== 'Stopped'`（null !== 'Stopped' 成立）-> noop-null。
//   C) dead-man's switch：实例 Running 但护闸 >STALE_MIN 分钟没写状态 -> 告警。
//   D) `StartInstance` 失败不再假报"已拉起"（spot + 节省停机下可能 NoStock）。
//   E) `VER` 常量：部署脚本调用一次并断言返回值，杜绝"以为部署成功、
//      实际热实例还在跑旧代码"（见 docs/pitfalls.md 第 26 条）。
//   F) 拉起熔断：防"FC 拉起 -> 护闸开机 -> 立刻又停机 -> FC 又拉起"的死循环。
//   G) spot 抢占预警：EventBridge 的 `PreemptibleInstanceInterruption` 事件。
//
// ⚠️ OSS **不是**「可选审计」—— 上面 F 与下面那两条告警都依赖它。
//    早期这里写着「删掉 OSS 桶不影响任何关键功能」，**是错的**，已改。实测代码里：
//      · **拉起熔断**（F 项，LOOP_MAX）读 START_HIST_KEY（见 recentStarts）；
//        没有桶就永远返回空数组 ⇒ **熔断永不触发**，死循环失去保护。
//      · **「护闸疑似失联」告警**取自 OSS 状态对象的 ts（stTsMs）；
//        没有桶则 stTsMs=0，staleMs 被算成 0 ⇒ **该告警永不触发**。
//        而文件开头把这条告警称为「此前没有任何外部观测者能发现『护闸已死』」的
//        **唯一**手段 —— 少了它，护闸死掉时 FC 侧完全无声。
//      · push_fail_streak、心跳写入同样静默失效。
//    只有停机判据本身（HALT_TAG → tagVal）不依赖 OSS。
//    **结论：OSS 桶是必需依赖，不是可选组件。**
//
// ── 踩过的坑（务必保留）────────────────────────────────────────────
// * FC 3.0 无论何种触发器都把事件序列化成 Buffer 传入，必须先解码再判断，
//   否则 event.triggerTime / event.body 永远读不到（详见 toObj）。
// * 阿里云的错误响应同样带 RequestId，用 `r.RequestId || r.Code` 会把错误码吞掉，
//   看起来像成功（详见 describe）。
// * 单个函数最多 50 个触发器；单条事件总线上最多 50 条事件规则。
//   ⚠️ 别照抄老资料里的「10 个」—— 那是**单 OSS Bucket 的原生触发器**上限，不是函数级上限。
// * 云产品事件触发器不支持通过 Serverless Devs 创建；但 **SDK 可以**
//   （`CreateTrigger` 建 timer、`UpdateTrigger` 改 eventbridge filter）。
// * `update_trigger(function_name, trigger_name, request)` 第一个位置参数是**函数名**，
//   不是 trigger ID。误传会得到 `FunctionNotFound: function '<id>' does not exist`。
// * **SDK 默认读超时 10 秒** —— `list_triggers` 跨地域调用（例如从非中国内地发起）容易超时，且异常只显示
//   `UnretryableException(<RetryPolicyContext>)`，真实原因被吞。要
//   `e.retry_policy_context.exception` 才能看到 "Read timed out"。
//   解决：Config(read_timeout=60000, connect_timeout=20000)。
// * ⚠️⚠️ **定时器的 payload 是字符串，不是解析好的对象**。FC 实际传入的形状：
//     {"triggerTime":"...","triggerName":"guard-liveness-8min",
//      "payload":"{\"liveness\": 1}"}
//   所以 `!!ev.liveness` 恒为 undefined，会让 8 分钟一次的
//   存活巡检会走「每日巡检报告」分支 —— 真的会推一条，于是变成
//   180 条/天。**新增定时器后必须等它自主触发一次再收工** —— 手工构造事件很容易
//   构造出与真实形状不符的输入，从而"验证通过"一个实际会出错的判据。
// * ⚠️ **抢占事件的实例 ID 在 `data.instanceId`，不是 `resourceId`**。
//   且**不要把 `subject` 放进候选** —— 它是 `acs.ecs:<region>:<uid>:<数字>`，
//   不含 `i-xxx`，放进去会每次误报"身份漂移"。
// * `environmentVariables` 是**整体覆盖**语义 —— UpdateFunction 漏一个键 = 删一个键。
//   部署必须"先 Get 再合并写回"，绝不硬编码清单。
// ──────────────────────────────────────────────────────────────────
//   ⚠️ **判断线上跑的是不是你要的那版，用 `VER` 常量，不要用文件哈希。**
//      部署脚本会调用一次并断言返回值（见 `deploy-fc.py`）——
//      那比哈希可靠，因为它来自**运行时**自报。
//      文件哈希随注释改动就变，本来就脆；而且 FC 的热实例可能仍在跑旧代码，
//      拿本地哈希去比线上源码，结论往往是错的。
const crypto = require('crypto');

// PUSH_TOKEN 缺失时 fail-loud，不再静默降级。
// 原实现是 `process.env.PUSH_TOKEN || ''` + `push()` 里 `if (!PUSH_TOKEN) return 'no-token';`
// —— 结果是「护闸在跑、告警永不发」这种最难发现的失效。
function requireEnv(name) {
  const v = process.env[name];
  if (!v) throw new Error(`缺少环境变量 ${name}，拒绝以静默降级方式启动`);
  return v;
}

const AK = process.env.ALIYUN_AK;
const SK = process.env.ALIYUN_SK;
const REGION = process.env.REGION_ID || 'cn-hongkong';
const INSTANCE_ID = process.env.ECS_INSTANCE_ID;
const INST_NAME = process.env.ECS_NAME || '<YOUR_INSTANCE_NAME>';
const EIP = process.env.EIP || '';
const ZONE = process.env.ZONE || 'cn-hongkong-c';
// 兜底默认值 200 —— **只在读不到护闸发布值时才用**。
// ⚠️ 200 等于免费额度本身，**没有安全余量** —— 这是刻意的取舍：可用流量最大化。
//    代价：护闸一旦滞后就直接超支，没有缓冲（混合口径已把原来的 ~5% 缓冲吃掉了）。
//    ⚠️ 本值**只用于告警文案**，没有任何判定依赖它 —— 真正的停机判据
//    在护闸（ECS 侧）里，它按**实测地域**取额度（非内地 200 / 内地 20）。
//    所以这里不能写死 200 就当"两侧一致"：内地部署时文案会写 200，
//    而护闸其实在 20 就停了。**优先读护闸发布的 threshold_gb。**
const THRESHOLD_GB_DEFAULT = parseFloat(process.env.TRAFFIC_THRESHOLD_GB || '200');
let THRESHOLD_GB = THRESHOLD_GB_DEFAULT;
const OSS_BUCKET = process.env.OSS_BUCKET;
const OSS_ENDPOINT = process.env.OSS_ENDPOINT || 'oss-cn-hongkong.aliyuncs.com';
// ⚠️ 版本标识。**必须存在** —— handler 返回体里有 `ver: VER`，
//    而部署脚本 `deploy-fc.py` 会调用一次并断言这个值，
//    用来确认「线上跑的确实是刚部署的那版」（FC 的热实例可能还在跑旧代码）。
//    删了它 = 每次调用都抛 ReferenceError = 整条 FC 兜底静默失效。
//    改动时**递增**，别把日期写进来。
//    本值不描述任何具体部署环境 —— 它是一个纯粹的功能版本号。
//    ⚠️ **递增是硬要求**：`deploy-fc.py` 的部署后断言是「运行时自报的 ver == 源码里的 ver」。
//       改了代码却不递增，这个断言**必然通过** —— 却证明不了新代码在跑（热实例问题）。
//       v3：存活巡检分支不再重复读 OSS（复用已读到的 `st`）。
//       v4：修 v3 引入的 ReferenceError（`st` 的块作用域跨不过 try）。
//       v5：修一个**实测确认过的 fail-open** —— OSS 对象读到了但没有可用 `ts` 时，
//           失联判据被算成 0、「护闸疑似失联」永不触发，且全程静默。
//           现在走独立的 `error-oss-ts-missing` 响铃分支。
//       v6：三个改动，都来自一次**线上误报的复核**：
//           ① `ossGet` 加**超时 + 重试**。原实现是裸 `fetch`：没有超时
//              （对端不回包会吃光函数超时，连告警都发不出去），
//              也没有重试（一次瞬时抖动就被判成"读取失败"）。
//           ② `errDetail()`：把 `fetch failed` 背后的 `e.cause.code` 挖出来。
//              原来只存 `String(e)`，导致故障**原理上不可归因** ——
//              分不清 EAI_AGAIN(DNS) / ECONNRESET / ETIMEDOUT。
//           ③ **告警冷却**（1 小时/类型）。原来 `push()` 没有任何去重，
//              任何持续状况都会每 8 分钟响一次（约 180 条/天）。
//              实测确认的重复源有三个：OSS 读不到、护闸失联、
//              以及 **skip-halted（额度用尽的设计稳态，一个月里最大宗）**。
//       v7：三个改动，全部来自**第二条线上告警**（2026-10-15 01:48）的复核。
//           那条告警的正文是 `cause=ENOTFOUND ... oss-cn-hongkong.aliyuncs.com`
//           —— **DNS 解析失败**，而当时护闸正在 ECS 上正常读写同一个桶。
//           ① **DNS 类故障的重试间隔单列**（600ms → 5s）。原重试对 DNS 是无效的：
//              两次尝试落在同一秒内，第二次必然命中解析器的同一份负缓存。
//              实测那条告警自己写着"已重试 2 次"，两次都在 1 秒内 —— 等于只试了一次。
//           ② **告警文案不再替读者下结论**。旧文案写「已重试 2 次…所以这意味着
//              **持续**失败」—— 600ms 的窗口证明不了"持续"，本次就是这样被
//              错报成持续性故障的。改为如实报出"试了几次、共跨多久、哪一类错"。
//           ③ **冷却补第三条存储**（ECS 实例 Tag）。v6 的「OSS + 内存」两层
//              在最需要它时都失效：OSS 读不到时 OSS 那层必然失效，而本函数
//              未开预留实例（实测 target=0）、8 分钟一次大概率是冷实例，
//              内存那层也是空的。**实测过这个洞**：模拟 OSS 全程不可达，
//              冷实例 4 次调用推 4 条。Tag 那条与 OSS 是不同故障域，
//              且读它零成本（DescribeInstances 本来就返回全部 Tag）。
const VER = 'v7-dns-retry-cooldown-tag';
const STATE_KEY = 'cdtguard/state.json';
const PUSH_TOKEN = requireEnv('PUSH_TOKEN');

// ── OSS 访问的健壮性参数 ────────────────────────────────────────────
// 🔴 这两个值的来历是一次**线上误报**：13:32 收到「！！ CDT FC 读不到 OSS 状态」，
//    而前后 3.5 小时 OSS 一直可达、FC 连打 35 次 0 失败。
//    也就是说：**一次瞬时抖动被升格成了一条系统性故障告警。**
//    判据（读不到就响铃）是对的，缺的是「瞬时 vs 持续」的区分。
// 超时：单次尝试的上限。60 秒是函数总超时，留足余量给重试 + 推送。
const OSS_TIMEOUT_MS = 8000;
// 重试间隔：必须要 —— 立即重试常常撞上同一个瞬时故障（同一个连接池 / 同一次抖动）。
const OSS_RETRY_DELAY_MS = 600;
// 重试次数（总尝试次数，不是"额外重试次数"）。
const OSS_ATTEMPTS = 2;

// ⚠️⚠️ **DNS 类故障的重试间隔必须另算 —— 600ms 对它是无效的。**
//    🔴 来历：2026-10-15 01:48 又收到一条「读不到 OSS 状态」，正文里带着
//       `cause=ENOTFOUND getaddrinfo ENOTFOUND ...oss-cn-hongkong.aliyuncs.com`。
//       那是 **DNS 解析失败**，而当时护闸（在 ECS 上）正一路正常读写同一个桶 ——
//       01:32 / 01:42 / 01:52 / 02:02 四条采样齐整，全天 0 错误。
//       也就是说：**OSS 没挂，是 FC 侧这一瞬间解析不出域名。**
//
//    ⚠️ 关键在于**时间尺度**：连接类抖动（ECONNRESET 等）是毫秒级的，
//       600ms 重试有意义；而 DNS 失败背后通常是**解析器的负缓存**，
//       按**秒**计。在同一秒内重试两次，第二次几乎必然命中同一份负缓存 ——
//       等于**只试了一次**。所以 600ms 的重试对这次故障完全不起作用。
//       （实测证据：那条告警里写着"已自动重试 2 次"，两次都落在同一秒内。）
//
//    ⇒ 判据不变（读不到仍然响铃），只是让重试**跨过 DNS 的时间尺度**。
//    ⚠️ 上限被 60 秒函数超时钉死：2 次尝试 × 8s 超时 + 5s 间隔 ≈ 21s 最坏情况，
//       给后面的 ECS 调用 / 推送 / 心跳留够余量。
//       **不要再加尝试次数** —— 3 次就是 34s，会把函数超时吃掉。
const OSS_RETRY_DELAY_DNS_MS = 5000;

// DNS 类错误码。Node 把它们挂在 `e.cause.code` 上（见 errDetail 上方的说明）。
//   ENOTFOUND / EAI_NODATA : 域名解析不到（本次线上遇到的就是 ENOTFOUND）
//   EAI_AGAIN              : 解析器临时失败（超时/不可达），通常**很短命**
//   EAI_FAIL               : 解析器返回了不可恢复的错误
const DNS_ERR_CODES = ['ENOTFOUND', 'EAI_NODATA', 'EAI_AGAIN', 'EAI_FAIL', 'ENODATA'];
function isDnsErr(e) {
  if (!e) return false;
  const c = e.cause || {};
  // 有的实现把 code 放在 cause 上，有的放顶层 —— 两处都看
  const codes = [c.code, c.errno, e.code].filter(Boolean).map(String);
  return codes.some(function (x) { return DNS_ERR_CODES.indexOf(x) >= 0; });
}

// 最近一次 OSS 读取的归因（供告警正文**如实**说明"试了几次、跨了多久"）。
let OSS_LAST = { attempts: 0, windowMs: 0, errClass: '' };

// ── 告警冷却 ────────────────────────────────────────────────────────
// 🔴 为什么要它：`push()` 本身**没有任何去重或冷却**（实测：连续调用 6 次，
//    6 次都推）。而存活巡检是 8 分钟一次 ⇒ OSS 只要持续不可达超过 8 分钟，
//    就会**每 8 分钟响一次**，直到恢复。
//    ⚠️ 这不是「读不到 OSS」这一条特有的 —— 「护闸疑似失联」同样每次都推。
//       两条都需要冷却。
//    冷却状态存 OSS（FC 是无状态的），键后缀带告警类型，
//    这样两类告警各自计时、互不遮挡。
//    ⚠️ 冷却窗口内**照常判定、照常返回 action**，只是**不发推送** ——
//       观测能力不下降（last-run.json 里仍能看到每次的 action），
//       下降的只有重复通知。
const ALERT_COOLDOWN_MS = 3600 * 1000;   // 1 小时
const ALERT_STATE_KEY = 'cdt-keeper/alert-state.json';
const HALT_TAG = 'cdt-halted-month';
const NL = String.fromCharCode(10);
// 部署校验用：部署脚本调用一次函数并断言返回的 ver 等于期望值。
// 不带这个断言，你会在「以为部署成功、实际热实例还在跑旧代码」的状态下收工
//   （见 docs/pitfalls.md 第 26 条）。
// 护闸失联判定阈值（分钟）
const STALE_MIN = 15;
// 拉起熔断：1 小时内 >= LOOP_MAX 次拉起 -> 判定疑似死循环，停止拉起并告警。
//   场景：tag_halted() 与 oss_put(halted=1) **都**写失败但 StopInstance 成功时，
//   FC 两条停机判据全为 false -> 立刻拉起 -> 护闸开机又停机 -> FC 又拉起……
//   每轮都在烧流量、每轮都推一条"已拉起"通知。窗口窄但后果重。
const LOOP_MAX = 3;
const LOOP_WINDOW_MS = 3600 * 1000;
const START_HIST_KEY = 'cdt-keeper/start-history.json';

function now8() {
  const d = new Date(Date.now() + 8 * 3600 * 1000);
  return d.toISOString().replace('T', ' ').slice(0, 19);
}
function curMonthCST() {
  const d = new Date(Date.now() + 8 * 3600 * 1000);
  return d.toISOString().slice(0, 7);
}
async function push(title, content) {
  try {
    const r = await fetch('https://www.pushplus.plus/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: PUSH_TOKEN, title: title, content: content,
                             template: 'txt', channel: 'wechat' })
    });
    return (await r.text()).slice(0, 110);
  } catch (e) { return 'ERR ' + String(e).slice(0, 90); }
}
function pct(s) {
  return encodeURIComponent(String(s))
    .split('!').join('%21').split(String.fromCharCode(39)).join('%27')
    .split('(').join('%28').split(')').join('%29')
    .split('*').join('%2A').split('%7E').join('~');
}
function buildQuery(p) {
  const cqs = Object.keys(p).sort().map(k => pct(k) + '=' + pct(p[k])).join('&');
  const sig = crypto.createHmac('sha1', SK + '&')
    .update('GET&%2F&' + pct(cqs)).digest('base64');
  return cqs + '&Signature=' + pct(sig);
}
async function aliCall(action, version, params, endpoint) {
  const ts = new Date().toISOString().slice(0, 19) + 'Z';
  const p = Object.assign({
    AccessKeyId: AK, Action: action, Format: 'JSON',
    SignatureMethod: 'HMAC-SHA1', SignatureNonce: crypto.randomUUID(),
    SignatureVersion: '1.0', Version: version, Timestamp: ts
  }, params || {});
  const r = await fetch('https://' + endpoint + '/?' + buildQuery(p));
  const t = await r.text();
  try {
    const j = JSON.parse(t);
    if (j.Code) j.status = r.status;
    return j;
  } catch (e) { return { parseError: t.slice(0, 200), status: r.status }; }
}
// ── 错误归因：把 fetch 的真实原因挖出来 ──────────────────────────────
// 🔴 为什么需要它：实测收到过一条线上告警，正文里只有
//      【OSS 错误】TypeError: fetch failed
//    而 `fetch failed` 是 Node undici 的**外层包装**，真实原因在 `e.cause` 里：
//      · cause.code = 'EAI_AGAIN'   → DNS 解析失败
//      · cause.code = 'ECONNRESET'  → 连接被重置（对端断、中间设备 RST）
//      · cause.code = 'ETIMEDOUT'   → 超时
//      · cause.code = 'ENOTFOUND'   → 域名不存在
//    丢掉 cause ⇒ **这个故障在原理上就不可归因**：不管谁去查，
//    都只能停在「网络抖了一下」，无法区分「DNS 出问题」还是「对端拒绝」。
//    这不是"信息不够"，是**观测点缺失** —— 所以补在这里。
//
//    ⚠️ 只用于**展示**，不参与任何判定：判定仍只看「读到 / 没读到」。
function errDetail(e) {
  if (!e) return '(未知错误)';
  const parts = [String(e)];
  // undici 把真实原因挂在 e.cause；有些实现还会再包一层
  const c = e.cause;
  if (c) {
    const code = c.code || c.errno || '';
    parts.push('cause=' + (code ? code + ' ' : '') + (c.message || String(c)));
    if (c.cause) {
      const c2 = c.cause;
      parts.push('cause2=' + (c2.code || c2.message || String(c2)));
    }
  }
  return parts.join(' | ').slice(0, 200);
}

// 最近一次 OSS 访问的诊断信息（HTTP 状态码或错误归因）。
// 为什么用模块级变量而不是返回值：`ossGet` 有两个调用点，返回信封会改动
// 两处的契约；而调用方真正需要的只是「为什么没读到」，塞进 out.ossErr 即可。
let OSS_DIAG = '';

async function ossFetchOnce(url, opts, ms) {
  // ⚠️ 必须有超时。裸 `fetch` **没有默认超时** —— 对端不回包时会一直挂着，
  //    直到把 FC 的整个 60 秒函数超时吃光，那时连告警都发不出去
  //    （函数被杀，push 根本没机会执行）。
  //    实测那次故障是**快速失败**（fetch failed），不是挂起 —— 但两种都要防。
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), ms);
  try {
    return await fetch(url, Object.assign({ signal: ac.signal }, opts));
  } finally {
    clearTimeout(timer);
  }
}

async function ossGet(key, opt) {
  if (!OSS_BUCKET) return null;
  const url = 'https://' + OSS_BUCKET + '.' + OSS_ENDPOINT + '/' + key;
  const o = opt || {};

  // ⚠️ 重试一次，用来吸收**单次网络抖动**。
  //    动机是一次真实误报：OSS 抖了一下 → 判定"读不到" → 推了一条
  //    「！！ CDT FC 读不到 OSS 状态」。而那个判据的语义是**持续不可达**，
  //    不是"这一毫秒没成功"。判据不变（仍然读不到就响铃），
  //    只是把**瞬时**失败和**持续**失败区分开。
  //
  //    ⚠️ 只重试**网络错误**，不重试 HTTP 非 200：
  //       403/404 是确定性结果，重试只是浪费时间、把函数超时往前推。
  //
  //    ⚠️ `attempts: 1` 是给**尽力而为**的读取用的（例如告警冷却状态）：
  //       那种读取失败不该把函数超时预算再吃掉一份。
  const attempts = o.attempts || OSS_ATTEMPTS;
  const timeoutMs = o.timeoutMs || OSS_TIMEOUT_MS;
  let lastErr = null;
  const startedAt = Date.now();
  for (let i = 0; i < attempts; i++) {
    if (i) {
      // ⚠️ 间隔按**上一次失败的类别**选：DNS 类要跨过解析器的负缓存（秒级），
      //    连接类抖动用短的即可。见 OSS_RETRY_DELAY_DNS_MS 上方的实测来历。
      const wait = isDnsErr(lastErr) ? OSS_RETRY_DELAY_DNS_MS : OSS_RETRY_DELAY_MS;
      await new Promise((r) => setTimeout(r, wait));
    }
    try {
      const date = new Date().toUTCString();
      const sts = ['GET', '', '', date, '/' + OSS_BUCKET + '/' + key].join(NL);
      const sig = crypto.createHmac('sha1', SK).update(sts).digest('base64');
      const r = await ossFetchOnce(url,
        { headers: { Date: date, Authorization: 'OSS ' + AK + ':' + sig } },
        timeoutMs);
      if (r.status !== 200) {
        // 确定性失败：记下状态码，不重试
        OSS_DIAG = 'HTTP ' + r.status;
        OSS_LAST = { attempts: i + 1, windowMs: Date.now() - startedAt, errClass: 'http' };
        return null;
      }
      try {
        const j = JSON.parse(await r.text());
        OSS_DIAG = 'HTTP 200';
        OSS_LAST = { attempts: i + 1, windowMs: Date.now() - startedAt, errClass: '' };
        return j;
      } catch (e) {
        OSS_DIAG = 'HTTP 200 但正文不是合法 JSON: ' + String(e).slice(0, 80);
        OSS_LAST = { attempts: i + 1, windowMs: Date.now() - startedAt, errClass: 'parse' };
        return null;
      }
    } catch (e) {
      lastErr = e;
      OSS_DIAG = errDetail(e);
      // 归因：试了几次、跨了多久、哪一类错 —— 告警正文靠它说实话
      OSS_LAST = {
        attempts: i + 1,
        windowMs: Date.now() - startedAt,
        errClass: isDnsErr(e) ? 'dns' : 'network',
      };
    }
  }
  // 全部尝试都失败 ⇒ 抛出去，让调用方按「读不到」处理（fail-loud）
  throw lastErr || new Error('ossGet 失败');
}
async function ossPut(key, obj) {
  if (!OSS_BUCKET) return 'no-bucket';
  const content = JSON.stringify(obj);
  const date = new Date().toUTCString();
  const md5 = crypto.createHash('md5').update(content).digest('base64');
  const ctype = 'application/json';
  const sts = ['PUT', md5, ctype, date, '/' + OSS_BUCKET + '/' + key].join(NL);
  const sig = crypto.createHmac('sha1', SK).update(sts).digest('base64');
  const r = await fetch('https://' + OSS_BUCKET + '.' + OSS_ENDPOINT + '/' + key,
    { method: 'PUT',
      headers: { Date: date, 'Content-MD5': md5, 'Content-Type': ctype,
                 Authorization: 'OSS ' + AK + ':' + sig },
      body: content });
  return r.status;
}

// ── 告警冷却 ────────────────────────────────────────────────────────
// 判据：同一个告警类型在 ALERT_COOLDOWN_MS 内只发一次。
//
// 🔴 为什么需要它：`push()` 本身**没有任何去重或冷却**（实测：连续调用 6 次，
//    6 次都推）。而存活巡检是 8 分钟一次 ⇒ 任何**持续**状况都会
//    每 8 分钟响一次（约 180 条/天）。实测确认的重复源至少有三个：
//      · OSS 读不到（本次收到的那条告警）
//      · 「护闸疑似失联」
//      · **skip-halted** —— 额度用尽后的**设计稳态**，一个月里可能长期如此
//
// ⚠️⚠️ **三层存储，各管一段，缺一不可。**
//    每一层补的都是上一层的失效场景：
//
//    ① **OSS**（跨实例、跨冷启动）—— 主力。
//       但它在**最需要冷却的场景**里恰好不可用：OSS 读不到的时候，
//       冷却状态也读不到。所以必须有下面两层。
//
//    ② **模块级内存**（同一热实例内）—— OSS 不可用时兜底。
//       ⚠️ 但它只在**热实例**上有效，而本函数未开预留实例
//       （实测 GetProvisionConfig: target=0）⇒ 8 分钟一次的调用
//       很可能每次都是冷实例，内存态是空的。
//       **实测过这个洞**：模拟「OSS 全程不可达」，热实例 2 次调用推 1 条（对），
//       冷实例 4 次调用推 4 条（等于没有冷却）。
//
//    ③ **ECS 实例 Tag**（跨实例、跨冷启动，且**与 OSS 是不同的故障域**）——
//       补的就是 ② 那个洞。
//       · 读是**免费的**：`instInfo()` 每次调用都已经拉回了实例的全部 Tag，
//         冷却时间戳直接从里面取，**零额外 API 调用**。
//       · 写只在告警时发生（一条 `TagResources`），频率极低。
//       · ⚠️ 关键性质：**OSS 挂掉时 ECS API 很可能仍然通**。
//         这不是假设 —— 2026-10-15 那条告警的正文里带着「【实例状态】Running」，
//         那正是 `DescribeInstances` 成功返回的结果：**OSS 域名解析失败的同时，
//         ECS 域名解析是好的**。两个独立的故障域，这才使得它有意义。
//         （反之若整个解析器都坏了，三层全失效 ⇒ 仍然告警，见下。）
//
//    取三者的**较新值**判断。
//    ⚠️ 三层全拿不到时**仍然告警** —— 宁可多推，绝不静默。
//       这与「读不到 OSS 就响铃」的方向一致。
const _memAlertAt = Object.create(null);   // kind -> ms（热实例内有效）

// ECS Tag 上的冷却键前缀。每个告警类型一个键（便于人工在控制台看出是什么）。
// ⚠️ 一个实例最多几十个 Tag，本函数只用到 3 个以内，不会顶到上限。
const ALERT_TAG_PREFIX = 'cdt-fc-alert-';

// 从**已经取回的**实例 Tag 里读冷却时间戳（零额外 API 调用）。
// ⚠️ 只认能解析成正整数的值 —— 手工写坏的 Tag 不该被当成"刚刚告警过"
//    而把真告警吞掉（那是 fail-silent，方向错误）。
function tagCooldownAt(tags, kind) {
  if (!tags) return 0;
  const n = parseInt(tags[ALERT_TAG_PREFIX + kind], 10);
  return (Number.isFinite(n) && n > 0) ? n : 0;
}

// 尽力写一次冷却 Tag。返回状态串供返回体透出（写失败不影响告警本身）。
//
// ⚠️ **参数名必须是 `ResourceId.1` / `Tag.1.Key` 这种点号形式**（阿里云 RPC 风格
//    的可重复参数）。已实测确认这条路可行，两点值得记：
//      · 用**命令行/MCP 工具**调 `TagResources` 传点号参数会**被拒**：
//        `OpenApiParamUnknown: parameter 'ResourceId.1' is not listed in OpenMeta`。
//        那是**工具侧的元数据校验**，不是 ECS API 拒收 —— 别据此以为这条路不通。
//      · 真实 API 接受这种形状：实测写 `cdt-fc-alert-__selftest` → 200 +
//        RequestId，`DescribeInstances` 立刻读得回来；`UntagResources` 也认
//        `TagKey.1`。（自检 Tag 已清理。）
//    ⇒ 所以这里的手写签名请求（aliCall）是可行路径，而工具链不是。
async function tagSetCooldown(kind, ms) {
  try {
    const r = await aliCall('TagResources', '2014-05-26', {
      RegionId: REGION, ResourceType: 'instance',
      'ResourceId.1': INSTANCE_ID,
      'Tag.1.Key': ALERT_TAG_PREFIX + kind,
      'Tag.1.Value': String(ms)
    }, 'ecs.aliyuncs.com');
    if (r && r.Code) return 'ERR ' + r.Code;
    if (r && r.parseError) return 'ERR parse';
    return 'OK';
  } catch (e) { return 'ERR ' + String(e).slice(0, 60); }
}

async function alertAllowed(kind, instTags) {
  const now = Date.now();
  let ossLast = 0;
  let ossState = null;
  // ⚠️⚠️ `ossUsable` 必须表示「**OSS 这个存储能不能用**」，
  //    而**不是**「刚才那次调用有没有抛异常」。
  //    实测踩到的区别：`ossGet` 对 HTTP 403 是 `return null`（**不抛**）——
  //    若把"没抛"当成"OSS 可用"，那么 OSS 返回 403 时我们会以为
  //    「冷却已经写进 OSS 了」，于是**永远不去写 Tag**，
  //    第三层存储在最需要它的场景里根本不会启用（等于白加）。
  //    判据改成看 `OSS_DIAG`：只有「对象存在(200)」或「对象不存在(404，
  //    首次告警前就是这样)」才算可用；403/网络错误/解析失败都算不可用。
  let ossUsable = false;
  try {
    ossState = await ossGet(ALERT_STATE_KEY, { attempts: 1, timeoutMs: 4000 });
    ossUsable = (OSS_DIAG === 'HTTP 200' || OSS_DIAG === 'HTTP 404');
    if (ossUsable) ossLast = (ossState && ossState[kind]) || 0;
  } catch (e) {
    ossUsable = false;
  }
  const memLast = _memAlertAt[kind] || 0;
  const tagLast = tagCooldownAt(instTags, kind);
  const last = Math.max(ossLast, memLast, tagLast);

  if (last && (now - last) < ALERT_COOLDOWN_MS) {
    return { allowed: false, lastAt: last, by: ossLast >= last ? 'oss'
               : (tagLast >= last ? 'tag' : 'mem') };
  }

  // 先记时间戳再返回"允许"。记录失败**不阻止**本次告警（见上）。
  _memAlertAt[kind] = now;
  const wrote = [];
  if (ossUsable) {
    try {
      const merged = Object.assign({}, ossState || {});
      merged[kind] = now;
      const st = await ossPut(ALERT_STATE_KEY, merged);
      // ⚠️ 必须看**状态码**：`ossPut` 对非 200 不抛，只看"没抛"会假报写成功。
      if (st === 200) wrote.push('oss');
    } catch (e) { /* 写不进不影响本次告警 */ }
  }
  // ⚠️ 只在**冷却没能写进 OSS**时才去动实例 Tag ——
  //    正常路径（OSS 健康）不产生任何额外 API 调用。
  //    这正是「OSS 读不到」那个场景：OSS 那层必然写不进，于是落到这里。
  let tagWrite = null;
  if (wrote.indexOf('oss') < 0) {
    tagWrite = await tagSetCooldown(kind, now);
    if (tagWrite === 'OK') wrote.push('tag');
  }
  return { allowed: true, lastAt: last, wrote: wrote, tagWrite: tagWrite };
  // 两个存储都没用上 ⇒ 显式标记，便于事后解释"为什么又推了"
  const bypassed = !ossOk && !memLast;
  return { allowed: true, lastAt: last, bypassed };
}

// 带冷却的推送。
//
// ⚠️ **返回值仍然是字符串**（与 `push()` 一致）—— 因为 `out.push` 一直是
//    「推送结果字符串」，被写进 last-run.json。改成对象会悄悄改变那个契约，
//    而 last-run.json 正是排查时读的东西。
//    被冷却时返回一句**显式说明**，而不是 null/空串：
//    空串会与「推送失败」混淆。三种情况必须能区分：
//      推送成功（{"code":200,...}） / 推送失败（ERR ...） / 被冷却跳过（下面这句）
//
// ⚠️ `silentWhenLiveness` 用于**稳态**：存活巡检每 8 分钟来一次，
//    对「本来就该如此」的状态（额度用尽）不该重复通知。
//    判据与 action 照常，只是**不推送** —— 观测能力不下降。
//
//    ⚠️ 存活与否必须由**参数**传入，不要读 `out.evIsLiveness` ——
//       那个字段是 decide() **返回之后**才赋值的，在 decide() 内部读到的是
//       undefined，会让这个判断静默失效（正是本仓库反复栽的那类顺序依赖）。
async function pushThrottled(kind, title, content, out, isLiveness, opts) {
  const o = opts || {};
  if (o.silentWhenLiveness && isLiveness) {
    if (out) { out.pushSkipped = kind + '(liveness 静默)'; }
    return '(存活巡检：稳态，按设计不推送；状态仍写入 last-run.json)';
  }
  // ⚠️ `instTags` 由调用方传入 —— 它是**已经取回的**实例 Tag（零额外 API 调用），
  //    作为 OSS 之外的第二条持久化冷却路径。见 alertAllowed 上方的三层说明。
  const g = await alertAllowed(kind, o.instTags);
  if (!g.allowed) {
    if (out) {
      out.pushSkipped = kind;
      out.pushCooldownMin = Math.round(ALERT_COOLDOWN_MS / 60000);
      out.pushCooldownBy = g.by || 'unknown';   // 哪一层挡住了（便于核实三层是否都在工作）
    }
    return '(冷却中：同一告警在 ' + Math.round(ALERT_COOLDOWN_MS / 60000)
             + ' 分钟内已推过一次，本次未重复推送)';
  }
  if (out && g.tagWrite) {
    // 只有真的走了 Tag 那条路才记录，避免正常路径上多出无意义的字段
    out.cooldownTagWrite = g.tagWrite;
  }
  return await push(title, content);
}
async function instInfo() {
  const d = await aliCall('DescribeInstances', '2014-05-26',
    { RegionId: REGION, InstanceIds: JSON.stringify([INSTANCE_ID]) }, 'ecs.aliyuncs.com');
  const ins = (d.Instances && d.Instances.Instance) || [];
  if (!ins.length) {
    // 查不到实例不是正常情况：说明 ECS_INSTANCE_ID 很可能已过期
    //   （实例重建后 ID 会变）。旧代码静默返回 status:null，让 decide()
    //   走 s !== 'Stopped'（null !== 'Stopped' 成立）-> noop-null ->
    //   什么都不做、也不告警。必须把阿里云的 Code/Message 透出来。
    return { status: null, tags: {}, notFound: true,
             code: d.Code || null, message: d.Message || null,
             apiStatus: d.status || null };
  }
  const tags = {};
  const tl = (ins[0].Tags && ins[0].Tags.Tag) || [];
  for (let i = 0; i < tl.length; i++) { tags[tl[i].TagKey] = tl[i].TagValue; }
  return { status: ins[0].Status, tags: tags };
}
async function startInst() {
  return await aliCall('StartInstance', '2014-05-26', { InstanceId: INSTANCE_ID }, 'ecs.aliyuncs.com');
}
function describe(r) {
  if (!r) return 'null';
  if (r.parseError) return 'parseError: ' + r.parseError;
  if (r.Code) return r.Code + ': ' + (r.Message || '') + ' (HTTP ' + (r.status || '?') + ')';
  return 'OK ' + (r.RequestId || '');
}
function bodyText(s, effGb, halted, tagVal, extra) {
  let b = '【实例】' + INST_NAME + ' (' + INSTANCE_ID + ')' + NL +
    '【公网IP】' + EIP + NL +
    '【地域】' + REGION + ' / ' + ZONE + NL +
    '【已用流量】' + (effGb === null ? '未知（OSS 已停用）' : effGb.toFixed(3) + ' GB') + NL +
    '【阈值】' + THRESHOLD_GB + ' GB' + NL +
    '【计费口径】共享带宽 每小时 max(入,出) 按月累计' + NL +
    '【实例状态】' + s + NL +
    '【停机锁】' + (halted ? '已锁定' : '未锁定') +
      (tagVal ? '（Tag: ' + HALT_TAG + '=' + tagVal + '）' : '') + NL +
    '【时间】' + now8() + ' (UTC+8)';
  if (extra) b += NL + extra;
  return b;
}

async function decide(source, evKeys, evResId, isPreempt, isLiveness) {
  const out = { ts: now8(), tz: 'UTC+8', src: source, evKeys: evKeys, ver: VER };
  try {
    const cur = curMonthCST();
    const info = await instInfo();
    const s = info.status;
    const tagVal = info.tags[HALT_TAG] || null;
    // 权威判据：实例自身的 Tag
    const tagHalted = (tagVal === cur);
    out.instStatus = s;
    out.tagValue = tagVal;
    out.tagHalted = tagHalted;
    // 身份漂移探测：EventBridge 的 ECS 状态变化事件自带 resourceId（零额外 API）。
    //   事件说 i-new、env 说 i-old -> ECS_INSTANCE_ID 已过期。
    //   注意：ID 过期时 instInfo() 必然查不到，所以上面 s === null 的
    //   fail-loud 分支本来就会触发；这个字段只是让告警直接指明漂移。
    out.evResourceId = evResId || null;
    if (evResId && evResId !== INSTANCE_ID) {
      out.idDrift = { env: INSTANCE_ID, event: evResId };
    }

    // ⚠️ 数据源涉及 OSS —— **不是**「可选审计」，见文件头那条说明。
    //    下面这段拿到的 stTsMs 决定「护闸疑似失联」告警能不能触发；
    //    没有 OSS 桶时 stTsMs=0 ⇒ 告警静默失效。（决策本身仍只依据实例状态与 Tag。）
    let effGb = null, ossHalted = false, stTsMs = 0, ossUnreadable = false;
    // ⚠️ 与 ossUnreadable 分开：那种情况读取是**成功**的，只是对象里没有可用 ts。
    //    文案必须能区分这两件事，否则告警自己就在说谎。
    let ossTsMissing = false;
    // ⚠️⚠️ `st` 必须在**内层 try 之外**声明。
    //    写成 `const st = ...` 放 try 里，则块级作用域只到那个 try 为止；
    //    而下面的 `isLiveness` 分支（同一个函数、但已出了那个块）还要用它
    //    → **每次存活巡检都抛 ReferenceError: st is not defined**
    //    → 被 decide() 外层的 catch 兜住 → 整个 8 分钟巡检静默退化成 error。
    //    实测：`let` 声明于 try 外、try 内赋值，跨块可见。
    let st = null;
    try {
      st = await ossGet(STATE_KEY);
      if (!st && OSS_BUCKET) {
        // ⚠️ ossGet 对「未配置 bucket」（设计允许）与「读取失败」（故障）都返回
        //    null。必须区分 —— 否则读不到 OSS 时 stTsMs 恒为 0、staleMs 恒为 0
        //    → 下面那条失联检测被**静默关闭**（fail-open）。
        //    读不到就响铃，不当成"无记录、不告警"。
        ossUnreadable = true;
      }
      if (st) {
        effGb = (st.eff || 0) / 1e9;   // 十进制 GB —— 与护闸的阈值口径一致（额度按 GB 计）
        // 必须绑定月份：
        //   旧写法 ossHalted = !!st.halted 只看布尔、不看月份 —— 而护闸达阈值
        //   停机后会随实例一起消失，OSS 对象就永久冻结在 halted=1。
        //   次月 1 日 FC 定时任务看到 tag 是上月的（tagHalted=false），
        //   但 ossHalted 仍为 true -> 整月拒绝拉起 -> 实例再也回不来，
        //   而推送还写着「等待次月 1 日额度重置」—— 主动误导。
        //   这不是概率问题，是「系统按设计工作」的必然结果。
        ossHalted = !!st.halted && st.month === cur;
        // 阈值以**护闸发布的实测值**为准（它按地域取：非内地 200 / 内地 20）。
        // 本文件里的默认值只作兜底，不能当成"两侧一致"的证据。
        if (st.threshold_gb) THRESHOLD_GB = st.threshold_gb;
        out.ossHaltedRaw = !!st.halted;
        out.ossMonth = st.month || null;
        out.ossTs = st.ts || null;
        stTsMs = Date.parse(st.ts || '') || 0;
        // 🔴🔴 **对象读到了、但拿不到可用时间戳 ⇒ 同样必须响铃。**
        //    这是一个**实测确认过的 fail-open**：`staleMs` 的定义是
        //        const staleMs = stTsMs ? (Date.now() - stTsMs) : 0;
        //    于是 stTsMs=0 时 staleMs 恒为 0，**「护闸疑似失联」永远不触发**，
        //    而 ossUnreadable 是 false（读取本身成功了）—— 全程静默。
        //
        //    可达性不是理论问题：**诱饵对象的形状就是没有 ts**
        //    （cdtguard.py 里那个 `{"month":"<上月>","acc":<巨大>}`），
        //    而那份文档写明「实测出现过 FC 运行读到诱饵的情况」。
        //    也就是说这个分支在真实环境里**被触发过** —— 只是当时没人发现它不响铃。
        //
        //    判据：**「时间戳不可得」与「读不到对象」对失联检测是同一件事** ——
        //    两者都回答不了「护闸还在写吗」。所以两者都必须响铃。
        //    （这也是 fc-keeper 规格里那条
        //      「状态对象没有可用时间戳 → 该告警 SHALL NOT 被静默跳过」的要求。）
        //
        //    ⚠️ 用**独立标志**而不是复用 ossUnreadable：那种情况下读取是成功的，
        //    文案必须如实说「读到了、但里面没有可用时间戳」，否则告警自己在说谎。
        if (!stTsMs) {
          ossTsMissing = true;
        }
      }
    } catch (e) {
      // ⚠️ 用 `errDetail` 而不是 `String(e)` —— 详见 errDetail 上方那段说明：
      //    裸 `fetch failed` 丢掉了 `e.cause`，会让故障**无法归因**。
      out.ossErr = errDetail(e);
      ossUnreadable = !!OSS_BUCKET;
    }
    // 🔴 **`ossErr` 必须在两条路径上都设置。**
    //    实测踩到：`ossGet` 对 **HTTP 非 200** 是 `return null`（不抛），
    //    所以上面那个 catch **根本不会执行** ⇒ `out.ossErr` 永远是 undefined
    //    ⇒ 告警正文里那行「【OSS 错误】」永远显示「(无)」——
    //    400/403/404 这些**最有诊断价值**的失败反而最没有信息。
    //    （与原来那个 `fetch failed` 丢 cause 是同一类缺陷：观测点在错的路径上。）
    //    这里统一从 `OSS_DIAG` 补上。
    if (!out.ossErr && OSS_DIAG && OSS_DIAG !== 'HTTP 200') {
      out.ossErr = OSS_DIAG;
    }
    const halted = tagHalted || ossHalted;   // 任一来源说已停机，就不拉起
    out.effGb = (effGb === null) ? null : Number(effGb.toFixed(3));
    out.halted = halted ? 1 : 0;

    // ⓪ spot 抢占中断预警（阿里云通常提前约 5 分钟发）
    //    这是**独立事件类型**，与 StateChange 不同，必须单独的 EventBridge 规则。
    if (isPreempt) {
      out.action = 'preempt-warning';
      out.push = await push('!! CDT 实例即将被 spot 回收',
        bodyText(s, effGb, halted, tagVal,
          '【动作】仅通知，未做任何操作' + NL +
          '【说明】阿里云已发出抢占中断预警，实例通常还能再跑约 5 分钟。' + NL +
          (halted ? '本月额度已用尽（停机锁已置位），回收后 FC 不会拉起。'
                  : '额度未用尽，回收后 FC 会自动拉起（视为意外停机）。')));
      return out;
    }
    // ① 实例查不到 -> fail-loud，绝不能静默 noop
    if (s === null || s === undefined) {
      out.action = 'error-instance-not-found';
      out.push = await push('！！ CDT FC：查不到实例（ID 可能已过期）',
        bodyText(s, effGb, halted, tagVal,
          '【动作】未做任何操作 — 无法确认实例状态' + NL +
          '【ECS_INSTANCE_ID】' + INSTANCE_ID + NL +
          '【事件 resourceId】' + (out.evResourceId || '(无，本次非事件触发)') + NL +
          '【阿里云返回】' + (info.code || '(空)') + ' / ' + (info.message || '(空)') + NL +
          '若实例刚重建，ID 已变 —— 必须同时更新 FC 的 ECS_INSTANCE_ID' + NL +
          '与护闸内嵌的 INSTANCE_ID，否则整条保护链静默失效。'));
      return out;
    }
    if (s !== 'Stopped') {
      out.action = 'noop-' + s;
      // ② dead-man's switch：实例 Running 但护闸长时间没写状态
      //    此前没有任何外部观测者能发现「护闸已死」：定时器每天只跑一次，
      //    事件只在状态变化时来。这正是「重建后 load_state 缺 last_rx 导致
      //    KeyError 卡死」那类故障的兜底 —— 那种情况下 rc-service status
      //    仍显示 started，但零计量、零写入、零停机。
      const staleMs = stTsMs ? (Date.now() - stTsMs) : 0;
      out.ossUnreadable = ossUnreadable;
      out.ossTsMissing = ossTsMissing;
      if (ossUnreadable) {
        // ⚠️ 读不到 OSS 不能"当作没有记录" —— 那样失联检测会静默失效。
        //    OSS_BUCKET 已配置却读不到，本身就是需要响铃的故障。
        out.action = 'error-oss-unreadable';
        // ⚠️ 带冷却：这条判据是**持续**故障，而定时器 8 分钟一次 ——
        //    没有冷却就会每 8 分钟响一次，直到 OSS 恢复（约 180 条/天）。
        //    本次真实误报（一次瞬时抖动）正是缺这个区分的表现。
        out.push = await pushThrottled('oss-unreadable', '！！ CDT FC 读不到 OSS 状态',
          bodyText(s, effGb, halted, tagVal,
            '【动作】未做任何操作' + NL +
            '【说明】OSS_BUCKET 已配置但读取失败（403/404/网络/解析失败）。' + NL +
            // ⚠️ **只陈述事实，不做超出证据的推断。**
            //    旧文案写「已重试 2 次…所以走到这里意味着**持续**失败」——
            //    而两次尝试只隔 600ms，**证明不了"持续"**。实测正是如此：
            //    2026-10-15 那次是 DNS 瞬时失败（ENOTFOUND），被这句话
            //    错误地描述成了持续性故障。
            //    ⇒ 改为如实报出：试了几次、**总共跨了多久**、哪一类错误。
            //      读的人据此自己判断，而不是被文案替他去判断。
            '⏱ 已尝试 ' + (OSS_LAST.attempts || OSS_ATTEMPTS) + ' 次，累计耗时约 '
              + Math.round((OSS_LAST.windowMs || 0) / 1000) + ' 秒'
              + (OSS_LAST.errClass === 'dns'
                  ? '（含 DNS 解析重试间隔 ' + Math.round(OSS_RETRY_DELAY_DNS_MS / 1000) + 's）'
                  : '') + NL +
            '【性质】'
              + (OSS_LAST.errClass === 'dns'
                  ? 'DNS 解析失败 —— 通常是**短暂**的解析器故障，不必然是 OSS 故障。'
                    + NL + '　　⚠️ 判据是"读不到就响"，所以瞬时解析失败也会触发本条。'
                  : '网络/HTTP 失败。') + NL +
            '在此期间**失联检测与审计同时失效** —— 故本判据改为响铃。' + NL +
            '护闸的停机逻辑不受影响（停机锁 Tag 仍是权威判据）。' + NL +
            '【OSS 错误】' + (out.ossErr || '(无)') + NL +
            '【OSS 桶】' + OSS_BUCKET), out, isLiveness, { instTags: info.tags });
      } else if (ossTsMissing) {
        // 🔴 与上面那条刻意分开：**读取是成功的**，只是对象里没有可用的时间戳。
        //    这种情况下 staleMs 会被算成 0、失联告警永远不响 —— 是个 fail-open。
        //    文案必须如实说「读到了，但里面没有时间戳」，不能冒充「读不到」。
        //    动作名也用独立的 —— 便于从 last-run.json / 日志里区分这两种故障。
        out.action = 'error-oss-ts-missing';
        // ⚠️ 带冷却：与上一条同理 —— 这是**持续**状况（对象缺 ts 不会自己好），
        //    8 分钟定时器下没有冷却就是刷屏。
        out.push = await pushThrottled('oss-ts-missing',
          '！！ CDT FC：OSS 对象里没有可用时间戳',
          bodyText(s, effGb, halted, tagVal,
            '【动作】未做任何操作' + NL +
            '【说明】状态对象**读到了**，但 `ts` 字段缺失或无法解析。' + NL +
            '这会让「护闸疑似失联」的判据失效（失联时长会被算成 0）——' + NL +
            '而它是「护闸已死」唯一的外部观测手段。故本判据改为响铃。' + NL +
            '⚠️ 常见成因：对象是被手工写入的**诱饵**，或写入被中途截断。' + NL +
            '【实际读到】' + JSON.stringify(out.ossTs) + NL +
            '【对象里的字段】' + Object.keys(st || {}).join(', ')), out, isLiveness,
          { instTags: info.tags });
      } else if (staleMs > STALE_MIN * 60 * 1000) {
        const mins = Math.round(staleMs / 60000);
        out.guardStaleMin = mins;
        out.action = 'error-guard-stale';
        // ⚠️ 带冷却。这条尤其重要：护闸一旦真的死掉，它会**一直**是失联状态，
        //    而它是「护闸已死」唯一的外部观测手段 —— 8 分钟响一次没有额外信息，
        //    只会让人把这条最重要的告警**静音**掉（那才是真正的损失）。
        //    冷却不降低观测能力：last-run.json 里每次的 action 照常记录。
        out.push = await pushThrottled('guard-stale', '！！ CDT 护闸疑似失联',
          bodyText(s, effGb, halted, tagVal,
            '【动作】未做任何操作 — 实例在跑，但护闸已 ' + mins + ' 分钟没写状态' + NL +
            '【OSS 状态时间】' + (out.ossTs || '(无)') + NL +
            '超过 ' + STALE_MIN + ' 分钟即判失联。护闸可能崩溃循环 / 卡死 /' + NL +
            'AK 失效 / 只读根分区。注意 rc-service status 仍可能显示 started。'), out, isLiveness,
          { instTags: info.tags });
      } else if (source === 'timer' && !isLiveness) {
        // ⚠️ 必须排除存活巡检 —— 它每 8 分钟来一次，推报告会变成刷屏。
        //    存活巡检是**静默**的：只有失联（上面那个分支）才说话。
        out.push = await push('CDT 每日巡检报告',
          bodyText(s, effGb, halted, tagVal, '【动作】无需操作，实例状态 ' + s));
      } else if (isLiveness) {
        out.action = 'liveness-ok';
        // ⚠️ 复用上面已经读到的 st —— **不要在这里再 ossGet 一次**。
        //    这是 8 分钟一次的热路径，多一次 OSS 往返纯属浪费；而且
        //    ossGet 读失败时返回 null，`|| {}` 会把「读失败」静默显示成 streak=0，
        //    恰好掩盖了它要报告的那个东西。
        out.ossPushFailStreak = (st && st.push_fail_streak) || 0;
        out.ossUnreadable = ossUnreadable;
      }
      return out;
    }
    if (halted) {
      out.action = 'skip-halted';
      // 🔴 **这条曾经是最大的重复推送源：（实测）存活巡检下每次都推，
      //    外推到约 180 条/天、一个月约 5400 条。**
      //
      //    而「额度已用尽 ⇒ 实例停机」是**设计上的稳态**，不是故障 ——
      //    它可能持续整月。8 分钟一条通知不带来任何新信息，
      //    只会让人把通知静音（那才是真正的损失）。
      //
      //    ⚠️ 这个 bug 的形状值得记：**当日志分支刻意排除了存活巡检，
      //       却漏了这条稳态分支** —— 上面「每日巡检报告」那条写着
      //       「必须排除存活巡检…否则会变成刷屏」，而同在 s!=='Stopped'
      //       之外的这一条没享受到同样的处理。
      //       实测发现方式是：模拟「额度用尽」稳态连打 12 次，数推送条数。
      //
      //    判据与 action 都不变，只是存活巡检下**不推送**；
      //    每日巡检（daily-0005-cst）和事件触发时照常推 ——
      //    所以「今天有没有停机」这件事每天仍会被主动告知一次。
      out.push = await pushThrottled('skip-halted', 'CDT 实例停机中（流量已达阈值）',
        bodyText(s, effGb, halted, tagVal,
          '【动作】不拉起 — 等待次月 1 日 00:00 (UTC+8) 额度重置'),
        out, isLiveness, { silentWhenLiveness: true, instTags: info.tags });
      return out;
    }
    // ── 拉起熔断：1 小时内已拉起 >= LOOP_MAX 次就不拉了 ──────────────
    //    场景：tag_halted() 与 oss_put(halted=1) 都写失败但 StopInstance 成功时，
    //    FC 两条停机判据全为 false -> 立刻拉起 -> 护闸开机又停机 -> FC 又拉起……
    //    每轮都在烧流量、每轮都推一条通知。窗口窄但后果重。
    const recent = await recentStarts();
    out.recentStarts = recent.length;
    if (recent.length >= LOOP_MAX) {
      out.action = 'start-blocked-loop';
      // ⚠️ 也带冷却。这条本身已经有「1 小时内 ≥ LOOP_MAX 次」的窗口，
      //    所以它**天然不会**每 8 分钟都成立（要连续拉起失败才触发）；
      //    但一旦成立就会持续成立 —— 加冷却避免它变成第二个刷屏源。
      //    熔断行为本身完全不变（仍然拒绝拉起），只是不重复通知。
      out.push = await pushThrottled('start-blocked-loop',
        '!! CDT 拉起已熔断（疑似死循环）',
        bodyText(s, effGb, halted, tagVal,
          '【动作】拒绝拉起 — 1 小时内已拉起 ' + recent.length + ' 次' + NL +
          '【说明】tag 与 OSS 两条停机判据可能都在写失败，导致：' + NL +
          'FC 拉起 -> 护闸开机 -> 立刻又停机 -> FC 又拉起……' + NL +
          '每轮都在烧流量、每轮都推一条通知。' + NL +
          '请人工检查护闸的 tag_halted() 与 oss_put() 是否正常。'),
        out, isLiveness, { instTags: info.tags });
      return out;
    }
    const r = await startInst();
    // StartInstance 是异步接口，且 spot + 节省停机模式下可能因库存不足被拒
    //   （OperationDenied.NoStock）。旧代码无论成败都推「已拉起」，
    //   是典型的假成功报告 —— 必须按返回码分叉。
    const started = !(r && r.Code);
    if (started) await recordStart();
    out.action = started ? 'start-under-threshold' : 'start-failed';
    out.startResp = describe(r);
    out.push = await push(started ? 'CDT 实例已拉起（意外停机）'
                                  : '！！ CDT 实例拉起失败',
      bodyText(s, effGb, halted, tagVal,
        '【动作】已执行 StartInstance（疑似抢占回收）' + NL +
        '【返回】' + describe(r) + NL +
        (started ? '【说明】该接口是异步的，稍后请确认真的到了 Running。'
                 : '拉起失败。spot + 节省停机下可能因库存不足被拒，请人工介入。')));
    return out;
  } catch (e) {
    out.error = String(e).slice(0, 300);
    out.push = await push('CDT FC 异常',
      '【错误】' + out.error + NL + '【时间】' + now8() + ' (UTC+8)');
  }
  return out;
}

function toObj(event) {
  let v = event;
  try {
    if (v && v.type === 'Buffer' && Array.isArray(v.data)) v = Buffer.from(v.data).toString('utf8');
    if (typeof Buffer !== 'undefined' && Buffer.isBuffer(v)) v = v.toString('utf8');
    if (typeof v === 'string') v = JSON.parse(v);
    if (v && typeof v === 'object' && v.triggerTime === undefined &&
        typeof v.body === 'string' && v.body.length) {
      try { return JSON.parse(v.body); } catch (e) { return v; }
    }
    return v;
  } catch (e) {
    return { parseErr: String(e).slice(0, 120) };
  }
}

async function recentStarts() {
  // 拉起的最近时刻列表（毫秒）。存 OSS —— FC 是无状态的。
  const h = await ossGet(START_HIST_KEY);
  const arr = (h && Array.isArray(h.t)) ? h.t : [];
  const cut = Date.now() - LOOP_WINDOW_MS;
  return arr.filter(function (x) { return x > cut; });
}
async function recordStart() {
  try {
    const cur = await recentStarts();
    cur.push(Date.now());
    await ossPut(START_HIST_KEY, { t: cur });
  } catch (e) { /* 记录失败不影响拉起本身 */ }
}

function evResIdOf(ev) {
  // EventBridge 的 ECS 状态变化事件自带 resourceId。结构可能平铺，也可能
  //   嵌在 detail/data/content 里 —— 都试一遍，拿不到就返回 null（不影响
  //   主流程，但 evSample 会把它记进 last-run.json，便于事后确认真实结构）。
  if (!ev || typeof ev !== 'object') return null;
  // 文档核实过的 EventBridge ECS 事件结构：
  //   {"source":"acs.ecs",
  //    "type":"ecs:Instance:PreemptibleInstanceInterruption",
  //    "subject":"acs.ecs:<region>:<uid>:<数字>",
  //    "data":{"action":"delete","instanceId":"<YOUR_INSTANCE_ID>"}}
  // ⚠️ 抢占事件的实例 ID 在 data.instanceId 里，**不是 resourceId** ——
  //    只查 resourceId 会漏掉它。
  // ⚠️ 不要把 subject 放进候选：它的形状是 acs.ecs:<region>:<uid>:<数字>，
  //    **不含 i-xxx**，放进来会导致每一次都误报"身份漂移"。
  const cands = [ev.resourceId,
                 ev.detail && ev.detail.resourceId,
                 ev.data && ev.data.resourceId,
                 ev.content && ev.content.resourceId,
                 ev.data && ev.data.instanceId,
                 ev.content && ev.content.instanceId,
                 ev.instanceId];
  for (let i = 0; i < cands.length; i++) if (cands[i]) return String(cands[i]);
  return null;
}

exports.handler = async (event, context) => {
  const ev = toObj(event);
  const evKeys = (ev && typeof ev === 'object') ? Object.keys(ev).slice(0, 14) : null;
  const evIsTimer = !!(ev && typeof ev === 'object' && ev.triggerTime !== undefined);
  // 存活巡检专用定时器（每 8 分钟）的 payload 是 {"liveness":1}。
  // ⚠️ 必须与"每日巡检"区分 —— 否则 8 分钟一次的定时器会把
  //    「CDT 每日巡检报告」每 8 分钟推一次（180 条/天）。
  // ⚠️⚠️ 第一版判据写成 `!!ev.liveness` 是**错的**：FC 把 payload 作为
  //    **字符串**塞在 ev.payload 里，不是解析好的对象。线上实测的事件形状：
  //      {"triggerTime":"<示例时间戳>",
  //       "triggerName":"guard-liveness-8min",
  //       "payload":"{"liveness": 1}"}
  //    所以 ev.liveness 恒为 undefined → 判据失效 → 走了每日报告分支 →
  //    自动触发时真的会推一条巡检报告（每 8 分钟一条）。
  //    修法：先把 ev.payload 当字符串 JSON.parse 出来再取字段。
  //    （下面 evIsTimer 的判据用 ev.triggerTime 是**顶层**字段，那个是对的。）
  let _lv = false;
  if (ev && typeof ev === 'object') {
    if (ev.liveness) {
      _lv = true;
    } else if (typeof ev.payload === 'string' && ev.payload) {
      try { _lv = !!JSON.parse(ev.payload).liveness; } catch (e) { _lv = false; }
    }
  }
  const isLiveness = _lv;
  // spot 抢占中断是**独立事件类型**（与 StateChange 不同）。事件结构未完全掌握，
  //   所以用文本匹配兜底：事件里只要出现 Preemptible / Interruption 就认。
  //   evSample 会把它记进 last-run.json，便于事后确认真实结构。
  const evText = JSON.stringify(ev || {});
  const isPreempt = /Preemptible|Interruption/i.test(evText);
  const r = await decide(evIsTimer ? 'timer' : 'event', evKeys, evResIdOf(ev), isPreempt, isLiveness);
  r.evIsPreempt = isPreempt;
  r.evIsLiveness = isLiveness;
  r.evKind = (typeof Buffer !== 'undefined' && Buffer.isBuffer(event)) ? 'buffer' : (typeof event);
  r.evSample = JSON.stringify(ev || {}).slice(0, 400);
  try { r.hbStatus = await ossPut('cdt-keeper/last-run.json', r); }
  catch (e) { r.hbError = String(e).slice(0, 150); }
  console.log('cdt-keeper', JSON.stringify(r));
  return r;
};
