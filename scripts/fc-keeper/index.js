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
//   ④ 熔断：1 小时内已拉起 >= LOOP_MAX 次 → 拒绝拉起并告警
//   ⑤ 拉起              → start-under-threshold / start-failed
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
const VER = 'v2-oss-unreadable-merged';
const STATE_KEY = 'cdtguard/state.json';
const PUSH_TOKEN = requireEnv('PUSH_TOKEN');
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
async function ossGet(key) {
  if (!OSS_BUCKET) return null;
  const date = new Date().toUTCString();
  const sts = ['GET', '', '', date, '/' + OSS_BUCKET + '/' + key].join(NL);
  const sig = crypto.createHmac('sha1', SK).update(sts).digest('base64');
  const r = await fetch('https://' + OSS_BUCKET + '.' + OSS_ENDPOINT + '/' + key,
    { headers: { Date: date, Authorization: 'OSS ' + AK + ':' + sig } });
  if (r.status !== 200) return null;
  try { return JSON.parse(await r.text()); } catch (e) { return null; }
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
    try {
      const st = await ossGet(STATE_KEY);
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
      }
    } catch (e) { out.ossErr = String(e).slice(0, 80); ossUnreadable = !!OSS_BUCKET; }
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
      if (ossUnreadable) {
        // ⚠️ 读不到 OSS 不能"当作没有记录" —— 那样失联检测会静默失效。
        //    OSS_BUCKET 已配置却读不到，本身就是需要响铃的故障。
        out.action = 'error-oss-unreadable';
        out.push = await push('！！ CDT FC 读不到 OSS 状态',
          bodyText(s, effGb, halted, tagVal,
            '【动作】未做任何操作' + NL +
            '【说明】OSS_BUCKET 已配置但读取失败（403/404/网络/解析失败）。' + NL +
            '在此期间**失联检测与审计同时失效** —— 故本判据改为响铃。' + NL +
            '护闸的停机逻辑不受影响（停机锁 Tag 仍是权威判据）。' + NL +
            '【OSS 错误】' + (out.ossErr || '(无)') + NL +
            '【OSS 桶】' + OSS_BUCKET));
      } else if (staleMs > STALE_MIN * 60 * 1000) {
        const mins = Math.round(staleMs / 60000);
        out.guardStaleMin = mins;
        out.action = 'error-guard-stale';
        out.push = await push('！！ CDT 护闸疑似失联',
          bodyText(s, effGb, halted, tagVal,
            '【动作】未做任何操作 — 实例在跑，但护闸已 ' + mins + ' 分钟没写状态' + NL +
            '【OSS 状态时间】' + (out.ossTs || '(无)') + NL +
            '超过 ' + STALE_MIN + ' 分钟即判失联。护闸可能崩溃循环 / 卡死 /' + NL +
            'AK 失效 / 只读根分区。注意 rc-service status 仍可能显示 started。'));
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
      out.push = await push('CDT 实例停机中（流量已达阈值）',
        bodyText(s, effGb, halted, tagVal,
          '【动作】不拉起 — 等待次月 1 日 00:00 (UTC+8) 额度重置'));
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
      out.push = await push('!! CDT 拉起已熔断（疑似死循环）',
        bodyText(s, effGb, halted, tagVal,
          '【动作】拒绝拉起 — 1 小时内已拉起 ' + recent.length + ' 次' + NL +
          '【说明】tag 与 OSS 两条停机判据可能都在写失败，导致：' + NL +
          'FC 拉起 -> 护闸开机 -> 立刻又停机 -> FC 又拉起……' + NL +
          '每轮都在烧流量、每轮都推一条通知。' + NL +
          '请人工检查护闸的 tag_halted() 与 oss_put() 是否正常。'));
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
