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
  ossBody: null,      // OSS GET 返回的正文
  ossStatus: 200,     // OSS GET 的状态码
  instStatus: 'Running',
  instEmpty: false,   // DescribeInstances 返回空数组
  tagValue: null,     // 实例上的 cdt-halted-month
  startResp: {},      // StartInstance 的返回（带 Code 表示失败）
  pushed: [],
};

function resetState() {
  S.ossBody = null; S.ossStatus = 200; S.instStatus = 'Running';
  S.instEmpty = false; S.tagValue = null; S.startResp = {}; S.pushed = [];
}

global.fetch = async (url, opt) => {
  const u = String(url);
  if (u.includes('pushplus')) {
    S.pushed.push(opt && opt.body ? String(opt.body) : '');
    return { status: 200, text: async () => '{"code":200}' };
  }
  if (u.includes('smoke-bucket')) {
    return { status: S.ossStatus, text: async () => S.ossBody, headers: new Map() };
  }
  if (u.includes('aliyuncs.com')) {
    // 云监控 / CDT 之类：给个空的成功响应
    if (!u.includes('ecs.aliyuncs.com')) {
      return { status: 200, text: async () => '{}', headers: new Map() };
    }
    const inst = S.instEmpty ? [] : [{
      Status: S.instStatus,
      OperationLocks: { LockReason: [] },
      Tags: { Tag: S.tagValue ? [{ TagKey: 'cdt-halted-month', TagValue: S.tagValue }] : [] },
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

  mute();
  const { handler } = require(TARGET);
  unmute();

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
