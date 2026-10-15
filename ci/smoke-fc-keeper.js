'use strict';
// ci/smoke-fc-keeper.js —— 用桩环境**真的跑一遍** fc-keeper 的决策分支。
//
// 用法：node ci/smoke-fc-keeper.js
//   退出码 0 = 全部场景符合预期；1 = 有场景不符（会打印实际 vs 期望）
//
// ═══ 为什么需要它 ═══
// 本仓库真实踩过两次，**两次 CI 都是绿的**：
//
//   1) 把「自己再读一次 OSS」改成「复用外层已有的 st」时，那个 `st` 是写在
//      `try { const st = ... }` **块内**的 —— 块外的分支每次执行都抛
//      `ReferenceError`，被最外层 catch 兜住 ⇒ **静默退化成一条 error**。
//      `node --check` 查不出（语法完全合法）。
//
//   2) OSS 对象读到了、但没有可解析的 `ts` 时，失联时长被算成 0 ⇒
//      **「护闸疑似失联」永不触发**，而读取是成功的 ⇒ 全程静默。
//
// 两次都是**只有真的执行到那条分支才会暴露**的问题。所以这道闸的方式是：
// **桩掉所有外部 IO，把每个决策分支都真的跑一遍，断言返回的 action**。
//
// ⚠️ 判据是「**断言 action**」，不是「不抛异常」——
//    上面第 1 条那个 bug 恰恰是被 catch 吞掉、返回体里只有个 error 字段。
//
// ═══ 它覆盖什么 ═══
// OSS 新鲜/过期/无 ts/垃圾 ts/读失败；实例 Running/Stopped；停机锁（本月/上月）；
// 实例查不到；拉起失败；存活巡检 vs 每日巡检 vs 事件触发。
//
// ═══ 它不覆盖什么 ═══
// 真实的阿里云 API 语义、真实 OSS、真实推送。它验的是**本文件的决策逻辑**，
// 不是「线上能跑」。要验后者得用 `--dry-run` 演练 + 部署后 `ver` 断言。
const path = require('path');
const crypto = require('crypto');

// ── 环境变量：在 require 目标文件之前设好（它是模块级读取的）──────────
process.env.PUSH_TOKEN = 'smoke-token';
process.env.OSS_BUCKET = 'smoke-bucket';
process.env.ECS_INSTANCE_ID = 'i-smoke';
process.env.ALIYUN_AK = 'smoke-ak';
process.env.ALIYUN_SK = 'smoke-sk';
process.env.ALIYUN_UID = 'smoke-uid-placeholder';
process.env.REGION_ID = 'cn-hongkong';

// ⚠️ **必须 resolve 成绝对路径再 require。**
//    `require('scripts/fc-keeper/index.js')` 会被当成**模块名**去找 node_modules，
//    报 `Cannot find module` —— 而不是「文件不存在」。
//    这会让人以为文件丢了。传相对路径是合理的用法（用来验某个改动过的副本），
//    所以这里显式 resolve；resolve 失败时给出能看懂的错误。
const TARGET = path.resolve(
  process.argv[2] || path.join(__dirname, '..', 'scripts', 'fc-keeper', 'index.js')
);
if (!require('fs').existsSync(TARGET)) {
  console.log(`X 找不到要检查的文件：${TARGET}`);
  process.exit(1);
}

// ── 可编程的桩状态 ──────────────────────────────────────────────────
const S = {
  ossBody: null,      // 主状态对象（cdtguard/state.json）的正文
  ossStatus: 200,     // 主状态对象的 GET 状态码
  ossTries: 0,        // 主状态对象被请求了几次（验重试用）
  // ⚠️ 告警冷却状态是**另一个键**（cdt-keeper/alert-state.json）。
  //    早先桩件对所有 OSS URL 返回同一个 body，于是冷却读取会拿到
  //    **主状态的正文** —— 里面没有冷却时间戳，冷却等于没生效，
  //    断言全挂在「推了 0 条」上。桩件必须**按 key 区分**，
  //    否则测的不是真实行为。
  alertBody: null,    // 冷却状态的正文（null = 当作不存在/404）
  alertStatus: 404,   // 冷却状态默认不存在（真实首次运行时就是这样）
  alertThrows: false, // 让冷却状态的读取抛网络错误（测 fail-open）
  // 让**整个桶**读不到（主状态 + 冷却状态一起 403）—— 真实的
  // 403/DNS 故障就是这样，而这正是第三层存储（ECS Tag）要补的场景。
  ossDown: false,
  // 「冷却状态读被拒、但写得进」的非对称场景 —— ossUsable 判据的判别用例
  alertReadDenied: false,
  alertWrites: 0,     // 冷却状态被成功写了几次
  instStatus: 'Running',
  instEmpty: false,   // DescribeInstances 返回空数组
  tagValue: null,     // 实例上的 cdt-halted-month
  // ⚠️ 告警冷却用的 Tag（`cdt-fc-alert-*`）。它存在**实例**上，
  //    所以必须能被 DescribeInstances 读回、被 TagResources 写入 ——
  //    否则测不出「冷实例 + OSS 挂掉」时第三层存储到底有没有生效。
  alertTags: {},      // key -> 时间戳字符串
  startResp: {},      // StartInstance 的返回（带 Code 表示失败）
  pushed: [],
};

// 由 main() 注入 —— 让 resetState 也能清掉模块级冷却态。
// ⚠️ 必须声明在 resetState **之前**：`let` 在初始化前是 TDZ，
//    而 resetState 里用 `typeof reloadFn` 读它 —— 那时会抛 ReferenceError
//    （`typeof` 对 TDZ 变量**不**返回 undefined）。顺序错了就是反例。
let reloadFn = null;

function resetState() {
  S.ossBody = null; S.ossStatus = 200; S.ossTries = 0;
  S.alertBody = null; S.alertStatus = 404; S.alertThrows = false;
  S.ossDown = false; S.alertWrites = 0; S.alertReadDenied = false;
  S.instStatus = 'Running';
  S.instEmpty = false; S.tagValue = null; S.alertTags = {};
  S.startResp = {}; S.pushed = [];
  // ⚠️ 内存冷却态在模块作用域里，只能靠**重新 require** 清掉（模拟冷启动）。
  if (reloadFn) reloadFn();
}

// 实例上的全部 Tag：停机锁 + 告警冷却（`cdt-fc-alert-*`）。
// ⚠️ 两者都必须返回 —— 前者是权威停机判据，后者是第三层冷却存储。
function tagList() {
  const t = [];
  if (S.tagValue) t.push({ TagKey: 'cdt-halted-month', TagValue: S.tagValue });
  Object.keys(S.alertTags).forEach(function (k) {
    t.push({ TagKey: k, TagValue: S.alertTags[k] });
  });
  return t;
}

global.fetch = async (url, opt) => {
  const u = String(url);
  if (u.includes('pushplus')) {
    S.pushed.push(opt && opt.body ? String(opt.body) : '');
    return { status: 200, text: async () => '{"code":200}' };
  }
  if (u.includes('alert-state')) {
    // 冷却状态：独立于主状态
    // ⚠️ `S.ossDown` 模拟**整个桶都读不到**（真实的 403/DNS 故障会同时影响两个键）。
    //    只把主状态设成 403、却让 alert-state 照常 200，测出来的就不是
    //    「OSS 挂了」而是「主状态缺了」—— 那样 Tag 那层永远不会被触发。
    if (S.ossDown) return { status: 403, text: async () => '', headers: new Map() };
    // ⚠️ PUT（写入）必须回 **200**：它不存在时 PUT 就是"创建"。
    //    若这里跟着 GET 一起回 404，`alertAllowed` 会以为写入失败，
    //    于是正常路径也会去写 Tag —— 那会掩盖"Tag 只在必要时才写"这个性质。
    const isPut = opt && opt.method === 'PUT';
    // ⚠️ `S.alertReadDenied`：**只读不到、但写得进**的非对称场景。
    //    这是 `ossUsable` 判据唯一真正的判别用例（见 ⑱f）：
    //    冷却若写进一个读不回来的地方 = 等于没写（下次照样以为没告警过）。
    //    正确行为是**根本别去写它**，改用可读的 Tag。
    if (S.alertReadDenied) {
      return isPut
        ? { status: 200, text: async () => '', headers: new Map() }   // 写：成功
        : { status: 403, text: async () => '', headers: new Map() };  // 读：被拒
    }
    if (isPut && !S.alertThrows) {
      S.alertBody = opt.body; S.alertStatus = 200;
      S.alertWrites = (S.alertWrites || 0) + 1;
      return { status: 200, text: async () => '', headers: new Map() };
    }
    if (S.alertThrows) throw new TypeError('fetch failed');
    return { status: S.alertStatus, text: async () => S.alertBody, headers: new Map() };
  }
  if (u.includes('smoke-bucket')) {
    // ⚠️ 只统计**主状态对象的 GET**。
    //    早先写成「任何 smoke-bucket 请求都 +1」，于是 handler 末尾那笔
    //    `ossPut('last-run.json')` 也被算进去 —— 断言"只请求 1 次"就永远
    //    得到 2。统计口径写宽了，断言就在测一件别的事。
    const isStateGet = u.includes('state.json') && (!opt || !opt.method || opt.method === 'GET');
    if (isStateGet) S.ossTries++;
    return { status: S.ossStatus, text: async () => S.ossBody, headers: new Map() };
  }
  if (u.includes('aliyuncs.com')) {
    // 云监控 / CDT 之类：给个空的成功响应
    if (!u.includes('ecs.aliyuncs.com')) {
      return { status: 200, text: async () => '{}', headers: new Map() };
    }
    // ── TagResources：第三层冷却存储的写入路径 ──────────────────────
    //    ⚠️ 必须**真的写进 S.alertTags**（而不是回个 OK 就算），
    //       否则「冷实例靠 Tag 冷却」那条断言就是在测空气。
    if (u.includes('Action=TagResources')) {
      const km = /Tag\.1\.Key=([^&]*)/.exec(u);
      const vm = /Tag\.1\.Value=([^&]*)/.exec(u);
      if (km && vm) {
        S.alertTags[decodeURIComponent(km[1])] = decodeURIComponent(vm[1]);
        return { status: 200, text: async () => JSON.stringify({ RequestId: 'smoke-tag' }),
                 headers: new Map() };
      }
      return { status: 200, text: async () => JSON.stringify({
        Code: 'InvalidParameter', Message: 'Tag.1.Key missing' }), headers: new Map() };
    }
    const inst = S.instEmpty ? [] : [{
      Status: S.instStatus,
      OperationLocks: { LockReason: [] },
      Tags: { Tag: tagList() },
    }];
    return { status: 200, text: async () => JSON.stringify({
      Instances: { Instance: inst }, TotalCount: inst.length,
    }), headers: new Map() };
  }
  return { status: 200, text: async () => '{}', headers: new Map() };
};

// StartInstance 走的是同一套 rpc → fetch，返回体带 Code 即为失败
const origFetch = global.fetch;
global.fetch = async (url, opt) => {
  const u = String(url);
  if (u.includes('Action=StartInstance')) {
    return { status: 200, text: async () => JSON.stringify(S.startResp), headers: new Map() };
  }
  return origFetch(url, opt);
};

// ── 断言 ────────────────────────────────────────────────────────────
let pass = 0, fail = 0;
const failures = [];

function check(name, actual, expected) {
  const ok = actual === expected;
  if (ok) { pass++; console.log(`  ✓ ${name}`); }
  else {
    fail++; failures.push(`${name}: 期望 ${expected}，实际 ${actual}`);
    console.log(`  ✖ ${name}\n      期望 action=${expected}  实际 action=${actual}`);
  }
}

function checkTruthy(name, actual) {
  const ok = !!actual;
  if (ok) { pass++; console.log(`  ✓ ${name}`); }
  else { fail++; failures.push(`${name}: 期望为真，实际 ${actual}`); console.log(`  ✖ ${name}（实际 ${actual}）`); }
}

// ── 场景驱动 ────────────────────────────────────────────────────────
async function main() {
  // ⚠️ 被检查文件的 handler 末尾有一句 `console.log('cdt-keeper', JSON.stringify(r))`
  //    —— 那是**给线上日志用的**，在 CI 里每个场景都会打一整行 600+ 字符的 JSON，
  //    把断言结果淹掉。
  //
  //    ⚠️ 实现方式要注意：**不能**用 `console.log = ...` 全局替换
  //    —— 断言函数 check()/checkTruthy() 也用 console.log，
  //    一替换就把 ✓ 行一起吞了（实测踩到：输出只剩一行）。
  //    正确做法是只把**被检查文件**的日志转存：拦截 require 期间，
  //    也就是只在 handler 被调用前后临时接管，调用完立刻还原。
  const silent = [];
  const realLog = console.log;
  const mute = () => { console.log = (...a) => { silent.push(a.map(String).join(' ')); }; };
  const unmute = () => { console.log = realLog; };

  // ⚠️⚠️ **每个场景都要「冷启动」一次。**
  //    fc-keeper 有一份**模块级内存态**（告警冷却，见 `_memAlertAt`）——
  //    那在线上是**刻意的**（热实例内兜底），但同一个 Node 进程里跑完所有
  //    场景时，前一个场景设的冷却会挡住后一个：
  //    实测症状就是「期望推 1 条、实际 0 条」，而且只在**同类告警**之间发生
  //    （⑮ 与 ⑱ 都是 guard-stale）。
  //
  //    解法是删 require 缓存重新加载 —— 等价于 FC 的一次冷启动，
  //    于是每个场景都在干净的状态上跑。**不给生产代码加测试专用的 reset 钩子**：
  //    那种钩子会进线上包，而且它本身就是一个"只有测试会走的路径"。
  let handler = null;
  const reload = () => {
    delete require.cache[require.resolve(TARGET)];
    mute();
    try { handler = require(TARGET).handler; } finally { unmute(); }
  };
  reloadFn = reload;
  reload();

  const callHandler = async (ev) => {
    mute();
    try { return await handler(ev, {}); }
    finally { unmute(); }
  };

  const nowIso = () => new Date().toISOString();
  const minsAgo = (m) => new Date(Date.now() - m * 60000).toISOString();
  const month = new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 7);

  const livenessEv = () => ({
    triggerTime: nowIso(), triggerName: 'guard-liveness-8min',
    payload: JSON.stringify({ liveness: 1 }),
  });
  const dailyEv = () => ({ triggerTime: nowIso(), triggerName: 'daily-0005-cst', payload: '{}' });

  console.log(`\n=== fc-keeper 决策分支冒烟（${path.basename(TARGET)}）===\n`);

  console.log('【存活巡检：OSS 时间戳的各种形态】');
  // ① 新鲜 ts：静默（不该响）
  resetState();
  S.ossBody = JSON.stringify({ month, eff: 1e9, halted: 0, ts: nowIso() });
  check('① ts 新鲜 → liveness-ok（不响铃）', (await callHandler(livenessEv())).action, 'liveness-ok');
  check('① 且没有发推送', S.pushed.length, 0);

  // ② 过期 ts：应报「护闸失联」
  resetState();
  S.ossBody = JSON.stringify({ month, eff: 1e9, halted: 0, ts: minsAgo(30) });
  check('② ts 过期 30 分钟 → error-guard-stale', (await callHandler(livenessEv())).action, 'error-guard-stale');
  check('② 且发了推送', S.pushed.length, 1);

  // ③ 对象存在但**没有 ts** —— 修复前这里是静默的 fail-open
  resetState();
  S.ossBody = JSON.stringify({ month, eff: 1e9, halted: 0 });
  check('③ 无 ts 字段 → error-oss-ts-missing（不得静默）', (await callHandler(livenessEv())).action, 'error-oss-ts-missing');
  check('③ 且发了推送', S.pushed.length, 1);

  // ④ ts 是垃圾串
  resetState();
  S.ossBody = JSON.stringify({ month, eff: 1e9, halted: 0, ts: 'not-a-date' });
  check('④ ts 不可解析 → error-oss-ts-missing', (await callHandler(livenessEv())).action, 'error-oss-ts-missing');

  // ⑤ ts 是空串
  resetState();
  S.ossBody = JSON.stringify({ month, eff: 1e9, halted: 0, ts: '' });
  check('⑤ ts 空串 → error-oss-ts-missing', (await callHandler(livenessEv())).action, 'error-oss-ts-missing');

  // ⑥ 诱饵对象（cdtguard.py 文档里那个形状：只有 month + acc，没有 ts）
  resetState();
  S.ossBody = JSON.stringify({ month: '2020-01', acc: 999 * 1e9 });
  check('⑥ 诱饵对象（无 ts）→ error-oss-ts-missing', (await callHandler(livenessEv())).action, 'error-oss-ts-missing');

  // ⑦ OSS 读失败
  resetState();
  S.ossStatus = 403; S.ossBody = '';
  check('⑦ OSS 403 → error-oss-unreadable', (await callHandler(livenessEv())).action, 'error-oss-unreadable');
  check('⑦ 且发了推送', S.pushed.length, 1);

  console.log('\n【实例状态 / 停机锁】');
  // ⑧ 本月停机锁 + 实例已停 → 不拉起
  //    ⚠️ 必须把实例设成 Stopped：决策顺序是「先判实例状态、再判停机锁」，
  //       Running 时走的是上面的 noop 分支，根本到不了 halted 判断。
  //       这一点本身就是个值得固化的顺序约束。
  resetState();
  S.tagValue = month;
  S.instStatus = 'Stopped';
  S.ossBody = JSON.stringify({ month, eff: 2e11, halted: 1, ts: nowIso() });
  check('⑧ 本月 tag 锁 + Stopped → skip-halted，不拉起', (await callHandler(dailyEv())).action, 'skip-halted');

  // ⑧b 只靠 OSS 的 halted（tag 丢了）也必须锁住 —— 这是 tag 打标失败时的唯一兜底
  resetState();
  S.tagValue = null;
  S.instStatus = 'Stopped';
  S.ossBody = JSON.stringify({ month, eff: 2e11, halted: 1, ts: nowIso() });
  check('⑧b 仅 OSS halted（当月）→ skip-halted', (await callHandler(dailyEv())).action, 'skip-halted');

  // ⑨ 上月锁 + 上月 OSS halted → 必须能拉起（跨月自锁回归）
  resetState();
  S.tagValue = '2020-01';
  S.instStatus = 'Stopped';
  S.ossBody = JSON.stringify({ month: '2020-01', eff: 2e11, halted: 1, ts: nowIso() });
  check('⑨ 上月锁（月份不匹配）→ start-under-threshold', (await callHandler(dailyEv())).action, 'start-under-threshold');

  // ⑩ 实例查不到 → fail-loud
  resetState();
  S.instEmpty = true;
  check('⑩ 实例查不到 → error-instance-not-found', (await callHandler(dailyEv())).action, 'error-instance-not-found');

  // ⑪ StartInstance 失败 → 不假报成功
  resetState();
  S.instStatus = 'Stopped';
  S.ossBody = JSON.stringify({ month, eff: 1e9, halted: 0, ts: nowIso() });
  S.startResp = { Code: 'OperationDenied.NoStock' };
  check('⑪ 拉起失败 → start-failed（不假报已拉起）', (await callHandler(dailyEv())).action, 'start-failed');

  // ⑫ 每日巡检报告（非存活巡检）
  resetState();
  S.ossBody = JSON.stringify({ month, eff: 1e9, halted: 0, ts: nowIso() });
  const daily = await callHandler(dailyEv());
  check('⑫ 每日巡检 → noop-Running', daily.action, 'noop-Running');
  check('⑫ 且发了日报推送', S.pushed.length, 1);

  // ⑬ 每次返回体都带 ver（deploy-fc.py 靠它做部署后断言）
  resetState();
  S.ossBody = JSON.stringify({ month, eff: 1e9, halted: 0, ts: nowIso() });
  const withVer = await callHandler(livenessEv());
  checkTruthy('⑬ 返回体带 ver', withVer.ver);
  check('⑬ ver 非空', typeof withVer.ver === 'string' && withVer.ver.length > 0, true);

  // ═══ ④ 冷却 / 重试 / 可归因（v6 新增）═══════════════════════════════
  // 🔴 这一组防的是**重复推送**，而它的发现过程本身就是一条教训：
  //    线上收到一条「读不到 OSS」告警，复核时发现——**那次是瞬时抖动**，
  //    而这条判据的语义是"持续不可达"。顺着一查，发现 `push()` 根本没有
  //    任何冷却，于是**任何持续状况都会每 8 分钟响一次**（约 180 条/天）。
  //    最严重的一条不是 OSS，而是 `skip-halted`：
  //    「额度用尽 ⇒ 停机」是**设计稳态**，可能持续整月。
  //
  // ⚠️ 判据仍然是 action（不变），这里断言的是**推送次数**。
  console.log('\n【告警冷却：持续状况不得反复推送】');

  // ⑭ 存活巡检连打 5 次（模拟 40 分钟）—— OSS 一直读不到，应只推 1 条
  resetState();
  S.ossDown = true;
  const actSeen = [];
  for (let i = 0; i < 5; i++) actSeen.push((await callHandler(livenessEv())).action);
  check('⑭ OSS 持续读不到 ×5 → action 每次都仍是 error-oss-unreadable',
        actSeen.every((a) => a === 'error-oss-unreadable'), true);
  check('⑭ 但只推送 1 条（冷却生效，不是 5 条）', S.pushed.length, 1);
  // ⚠️ 热实例靠内存态就够了（OSS 那层写不进去）。这条**不能**证明 Tag 那层在工作，
  //    它证明的只是"同一个热实例内不会重复推"。Tag 那层由 ⑱c 专门验。
  check('⑭ OSS 写不进时应已回落到 Tag 那层', S.alertTags['cdt-fc-alert-oss-unreadable'] !== undefined, true);

  // ⑮ 同样的冷却必须覆盖「护闸失联」—— 它才是最该被反复提醒却又最吵的
  resetState();
  S.ossBody = JSON.stringify({ month, eff: 1e9, halted: 0, ts: minsAgo(60) });
  for (let i = 0; i < 5; i++) await callHandler(livenessEv());
  check('⑮ 护闸失联 ×5 → 只推送 1 条', S.pushed.length, 1);

  // ⑯ **skip-halted 稳态在存活巡检下完全不推** —— 这条是最大宗（约 180 条/天）
  //    ⚠️ 判据与 action 都不变，只是不推送。
  resetState();
  S.tagValue = month; S.instStatus = 'Stopped';
  S.ossBody = JSON.stringify({ month, eff: 2e11, halted: 1, ts: nowIso() });
  for (let i = 0; i < 5; i++) {
    const r = await callHandler(livenessEv());
    if (r.action !== 'skip-halted') { check('⑯ 存活巡检应落进 skip-halted', r.action, 'skip-halted'); }
  }
  check('⑯ 存活巡检下 skip-halted ×5 → 推送 0 条（稳态静默）', S.pushed.length, 0);

  // ⑯b 但**每日巡检**仍要推 —— 否则「今天停机了」就再也没人告知
  resetState();
  S.tagValue = month; S.instStatus = 'Stopped';
  S.ossBody = JSON.stringify({ month, eff: 2e11, halted: 1, ts: nowIso() });
  await callHandler(dailyEv());
  check('⑯b 每日巡检的 skip-halted 仍推 1 条', S.pushed.length, 1);

  // ⑰ 冷却**不得吞掉一次性事件**：每日巡检报告不属于那几类持续告警
  resetState();
  S.ossBody = JSON.stringify({ month, eff: 1e9, halted: 0, ts: nowIso() });
  await callHandler(dailyEv());
  await callHandler(dailyEv());
  check('⑰ 每日巡检报告不受冷却影响（连打 2 次推 2 条）', S.pushed.length, 2);

  // ⑱ 冷却状态读不到时**必须仍然告警**（宁可多推，绝不静默）
  //    ⚠️ 这是双存储设计里「OSS 那一路失效」的分支。
  resetState();
  S.alertThrows = true;
  S.ossBody = JSON.stringify({ month, eff: 1e9, halted: 0, ts: minsAgo(60) });
  const r18 = await callHandler(livenessEv());
  check('⑱ 冷却状态读不到 → 仍然推送（不静默）', S.pushed.length, 1);
  check('⑱ action 仍是 error-guard-stale', r18.action, 'error-guard-stale');
  // ⚠️ 断言的是"**确实退到了 Tag 那层**"，而不是某个自我描述的布尔字段。
  //    前者是行为，后者只是实现的自述 —— 实现写错了它照样为真。
  check('⑱ 且确实退到了 Tag 那层（可事后核实）', r18.cooldownTagWrite, 'OK');

  // ⑱b 内存冷却必须真的兜底：冷却状态一直读不到时，第 2 次也不该再推
  //     （这验证「内存态」不是摆设 —— 它正是 OSS 挂掉时的那一路）
  S.pushed = [];
  await callHandler(livenessEv());
  check('⑱b 冷却状态持续读不到 → 内存态兜底，第 2 次不推', S.pushed.length, 0);

  // ═══ ⑱c：**第三层存储（ECS Tag）的专项验证** ═══════════════════════
  // 🔴 这条是 v6 那个洞的直接回归。洞的形状：
  //      OSS 读不到 ⇒ 冷却状态读不到也写不进（那是同一件事）
  //      本函数没开预留实例（实测 target=0）⇒ 8 分钟一次的调用大概率是**冷实例**
  //      ⇒ 内存态是空的 ⇒ **每一次都推**。
  //    实测过：模拟 OSS 全程不可达，冷实例 4 次调用推 4 条。
  //    所以必须有一条**与 OSS 不同故障域**的持久冷却路径 —— ECS Tag。
  //    ⚠️ 它之所以可行，是因为 OSS 挂掉时 ECS API 很可能仍通：
  //       2026-10-15 那条告警的正文里带着「【实例状态】Running」，
  //       那正是 DescribeInstances 成功返回的 —— 两个独立故障域。
  console.log('\n【第三层存储：冷实例 + OSS 读不到时，靠实例 Tag 冷却】');
  resetState();
  S.ossDown = true;
  const rTag1 = await callHandler(livenessEv());
  check('⑱c 冷实例 + OSS 读不到 → 第 1 次仍要告警（绝不静默）', S.pushed.length, 1);
  check('⑱c 且真的写进了实例 Tag', rTag1.cooldownTagWrite, 'OK');
  checkTruthy('⑱c Tag 里确实有了冷却时间戳',
              S.alertTags['cdt-fc-alert-oss-unreadable'] !== undefined);

  // ⚠️⚠️ 关键一步：**模拟冷启动**（重新 require 模块 ⇒ 内存态清空），
  //    但**保留 OSS 不可达与实例 Tag**。这正是真实故障里的情形：
  //    下一个 8 分钟刻度落在一个全新的实例上，它没有内存态，
  //    只能靠 Tag 知道自己一小时前刚响过。
  S.pushed = [];
  reload();
  const rTag2 = await callHandler(livenessEv());
  check('⑱c 冷启动后第 2 次 → **靠 Tag 挡住，不再推**（v6 的洞已补）', S.pushed.length, 0);
  check('⑱c 且注明是 tag 那层挡的', rTag2.pushCooldownBy, 'tag');

  // ⑱d 对照：把 Tag 拿掉，同样冷启动 ⇒ 必须**又推**（证明挡住它的是 Tag，不是别的东西）
  //     ⚠️ 没有这条对照，⑱c 有可能是因为别的原因"恰好"没推 —— 那就测了个寂寞。
  S.pushed = [];
  S.alertTags = {};
  reload();
  await callHandler(livenessEv());
  check('⑱d 对照：Tag 清掉后同样的冷启动 → 又推 1 条', S.pushed.length, 1);

  // ⑱e OSS 健康时**不该**去写 Tag（正常路径零额外 API 调用）
  resetState();
  S.ossBody = JSON.stringify({ month, eff: 1e9, halted: 0, ts: minsAgo(60) });
  const rTag3 = await callHandler(livenessEv());
  check('⑱e OSS 健康 → 不写实例 Tag（正常路径无额外开销）',
        Object.keys(S.alertTags).length, 0);
  check('⑱e 且冷却确实写进了 OSS', S.alertWrites > 0, true);
  check('⑱e 返回体不带 cooldownTagWrite', rTag3.cooldownTagWrite, undefined);

  // ⑱f **「读被拒但写得进」—— `ossUsable` 判据的判别用例。**
  //     🔴 这条是反向验证逼出来的：把 `ossUsable` 强行改成 true，
  //        **其它断言全都照样通过** —— 说明它们都没有区分这两种情况。
  //        而这两种情况的行为差别是实质性的：
  //          · 误判为"可用"⇒ 冷却被写进一个**读不回来的地方**，
  //            下次仍然以为没告警过 ⇒ 继续刷屏，且 Tag 那层永不启用。
  //          · 正确判为"不可用"⇒ 干脆不写 OSS，改用可读的 Tag。
  //     ⚠️ 触发条件少见（桶策略只给 PutObject 不给 GetObject、对象 ACL 异常），
  //        但**少见正是它需要被钉住的原因** —— 否则这个判据等于没写。
  resetState();
  S.alertReadDenied = true;
  S.ossBody = JSON.stringify({ month, eff: 2e11, halted: 1, ts: nowIso() });
  S.tagValue = month; S.instStatus = 'Stopped';
  const rReadDenied = await callHandler(dailyEv());
  check('⑱f 读被拒时不得把冷却写进 OSS（写了也读不回来）', S.alertWrites, 0);
  check('⑱f 且必须改用实例 Tag', rReadDenied.cooldownTagWrite, 'OK');
  checkTruthy('⑱f Tag 里确实落了时间戳',
              S.alertTags['cdt-fc-alert-skip-halted'] !== undefined);

  console.log('\n【OSS 读取健壮性】');

  // ⑲ 首次失败、第二次成功 → **不应告警**（单次抖动必须被吸收）
  //    这是本次真实误报的直接回归：判据仍是"读不到就响铃"，
  //    但"读得到"就不该响 —— 哪怕第一次尝试失败了。
  resetState();
  S.ossStatus = 200;
  S.ossBody = JSON.stringify({ month, eff: 1e9, halted: 0, ts: nowIso() });
  {
    // 只让**第 1 次**主状态请求抛错
    const inner = global.fetch;
    let n = 0;
    global.fetch = async (url, opt) => {
      if (String(url).includes('smoke-bucket')) {
        n++;
        if (n === 1) throw new TypeError('fetch failed');
      }
      return inner(url, opt);
    };
    const r19 = await callHandler(livenessEv());
    global.fetch = inner;
    check('⑲ 首次抖动、重试成功 → liveness-ok（误报被吸收）', r19.action, 'liveness-ok');
    check('⑲ 且没有推送', S.pushed.length, 0);
    checkTruthy('⑲ 确实重试过（尝试次数 > 1）', n > 1);
  }

  // ⑳ 两次都失败 → 仍必须告警，且**错误可归因**（带 cause 的 code）
  resetState();
  {
    const inner = global.fetch;
    global.fetch = async (url, opt) => {
      if (String(url).includes('smoke-bucket')) {
        const e = new TypeError('fetch failed');
        e.cause = Object.assign(new Error('getaddrinfo EAI_AGAIN'), { code: 'EAI_AGAIN' });
        throw e;
      }
      return inner(url, opt);
    };
    const t0 = Date.now();
    const r20 = await callHandler(livenessEv());
    const elapsed20 = Date.now() - t0;
    global.fetch = inner;
    check('⑳ 两次都失败 → error-oss-unreadable', r20.action, 'error-oss-unreadable');
    checkTruthy('⑳ 且 ossErr 带得上真实原因（EAI_AGAIN）',
                r20.ossErr && r20.ossErr.includes('EAI_AGAIN'));
    // ⚠️⚠️ **DNS 类重试必须真的跨过"秒"这个量级。**
    //    这是本次线上告警的核心修复点，所以**必须用真实耗时钉住**，
    //    而不是只断言"重试了 2 次"—— 后者在 600ms 间隔下**同样成立**，
    //    而那正是出问题的配置（两次尝试落在同一秒内 = 等于只试一次）。
    //    实测：反向验证把间隔改回 600ms，这条会立刻变红。
    //    ⚠️ 代价：这条断言会让套件多花约 5 秒（真实等待，无法短路）。
    //       值与所得：它钉住的是一次**已发生过的线上误报**，值得。
    checkTruthy('⑳ DNS 类重试真的等到秒级（≥4s，证明不是 600ms 的假重试）',
                elapsed20 >= 4000);
    // 告警正文必须**如实**报出跨了多久 —— 这是 v7 改的第二件事：
    // 旧文案写「已重试 2 次…所以这意味着**持续**失败」，而 600ms 的窗口
    // 证明不了"持续"，本次就是这样被错报成持续性故障的。
    const body20 = S.pushed.length ? S.pushed[S.pushed.length - 1] : '';
    checkTruthy('⑳ 告警正文如实报出尝试次数与耗时（不再自称"持续失败"）',
                body20.includes('已尝试') && body20.includes('累计耗时'));
    check('⑳ 且不再出现"持续失败"这个超出证据的结论',
          body20.includes('意味着'), false);
  }

  // ⑳b 反向对照：**非 DNS** 的网络错误不该等 5 秒（否则白吃函数超时预算）
  //     没有这条，"把所有重试都改成 5 秒"也能让 ⑳ 通过。
  resetState();
  {
    const inner = global.fetch;
    global.fetch = async (url, opt) => {
      if (String(url).includes('smoke-bucket')) {
        const e = new TypeError('fetch failed');
        e.cause = Object.assign(new Error('read ECONNRESET'), { code: 'ECONNRESET' });
        throw e;
      }
      return inner(url, opt);
    };
    const t0 = Date.now();
    await callHandler(livenessEv());
    const elapsed20b = Date.now() - t0;
    global.fetch = inner;
    checkTruthy('⑳b 非 DNS 错误走短间隔（<2s，不浪费函数超时预算）', elapsed20b < 2000);
  }

  // ㉑ 非 200（确定性失败）不该白重试 —— 只打一次 OSS 请求
  resetState();
  S.ossStatus = 403; S.ossBody = '';
  const r21 = await callHandler(livenessEv());
  check('㉑ HTTP 403 → error-oss-unreadable', r21.action, 'error-oss-unreadable');
  check('㉑ 确定性失败只请求 1 次（不重试）', S.ossTries, 1);
  // ⚠️ 这条断言来自一个**实测踩到的缺陷**：`ossGet` 对非 200 是 `return null`
  //    （不抛），所以 catch 不执行 ⇒ 状态码永远不会进 ossErr ⇒
  //    告警正文那行「【OSS 错误】」永远显示「(无)」。
  //    最有诊断价值的 400/403/404 反而最没信息。
  checkTruthy('㉑ 且 ossErr 记录了状态码（403）',
              r21.ossErr && r21.ossErr.includes('403'));

  console.log(`\n通过 ${pass}，失败 ${fail}`);
  if (fail) {
    console.log('\n!! 有场景不符合预期 —— 这些正是 node --check 看不见的那类缺陷。');
    failures.forEach((f) => console.log('   - ' + f));
    // 回放被检查文件自己打的日志，便于定位是哪个场景
    if (silent.length) {
      console.log('\n--- 被检查文件的运行日志（末 6 行）---');
      silent.slice(-6).forEach((l) => console.log('   ' + l.slice(0, 220)));
    }
    return 1;
  }
  console.log('OK  所有决策分支符合预期');
  return 0;
}

main().then((c) => process.exit(c)).catch((e) => {
  console.log('\n!! 冒烟脚本自身抛错（可能是被检查文件的缺陷）：');
  console.log('   ' + (e && e.stack ? e.stack.split('\n').slice(0, 4).join('\n   ') : e));
  process.exit(1);
});
