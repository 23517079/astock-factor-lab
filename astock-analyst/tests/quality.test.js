/**
 * tests/quality.test.js — 状态契约、字段级对账与故障注入
 *
 * 不依赖已启动的服务；真实网络部分会实际拉腾讯/新浪，可反复跑。
 * 故障注入的目的：证明「把数字改错」会被拦住，而不是只验证 happy path。
 * 复现的是 2026-11-13 的真实 bug：把今开当昨收，英伟达 -4.34% 被算成 +10.06%。
 */
const CT = require('../lib/contract.js');
const RC = require('../lib/reconcile.js');

let pass = 0, fail = 0;
const fails = [];
function ok(name, cond, extra) {
  if (cond) { pass++; console.log('PASS', name, extra || ''); }
  else { fail++; fails.push(name); console.log('FAIL', name, extra || ''); }
}

const pctRule = {
  scope: 'test', field: 'nvda.changePct', label: '英伟达 涨跌幅',
  tolerance: 0.3, toleranceMode: 'absolute', kind: 'independent'
};
const good = RC.compare({ source: '新浪', value: -4.34 }, { source: '腾讯', value: -4.34 }, pctRule);
ok('两源一致 → match', good.result === 'match', good.reason);

/* 故障注入：主源被错误解析成 +10.06%，第二源腾讯给出真实 -4.34% */
const bad = RC.compare({ source: '新浪(错误解析)', value: 10.06 },
  { source: '腾讯', value: -4.34 }, pctRule);
ok('故障注入：错误涨跌幅被判 mismatch', bad.result === 'mismatch', 'delta=' + bad.delta);
ok('故障注入：保留两源数值', bad.primary.value === 10.06 && bad.secondary.value === -4.34);
ok('故障注入：差值可核对', Math.abs(bad.delta - 14.4) < 1e-6);

const single = RC.compare({ source: '新浪', value: -4.34 }, null,
  Object.assign({}, pctRule, { noSecondReason: '腾讯无该标的报价' }));
ok('无第二源 → unverifiable', single.result === 'unverifiable');
ok('未核对写明原因', /腾讯无该标的报价/.test(single.reason));
ok('主源无值不算已核对', RC.compare(null, { source: '腾讯', value: 1 }, pctRule).result === 'unverifiable');

const idxRule = {
  scope: 'test', field: 'dji.price', label: '道琼斯 点位',
  tolerance: 0.02, toleranceMode: 'relative', relative: 0.005, kind: 'independent'
};
ok('道指 52297.09 vs 52297.0898 → match',
  RC.compare({ value: 52297.09 }, { value: 52297.0898 }, idxRule).result === 'match');
ok('过期快照 46247.29 vs 52297.09 → mismatch',
  RC.compare({ value: 46247.29 }, { value: 52297.09 }, idxRule).result === 'mismatch');

const legacy = { ok: true, cards: [{ code: '510300', price: 4.083 }], errors: [], asOf: '2026-11-13' };
const wrapped = CT.attach(legacy, { scope: 'etf', checks: [good] });
ok('旧字段零改动', wrapped.ok === true && wrapped.cards.length === 1 && wrapped.cards[0].price === 4.083);
ok('新增 status / issues / verification', ['status', 'issues', 'verification'].every(k => k in wrapped));
ok('无冲突 → status=ok', wrapped.status === 'ok' && wrapped.verification.matched === 1);
ok('原对象未被修改', legacy.status === undefined && legacy.issues === undefined);

const degraded = CT.attach(legacy, { scope: 'etf', checks: [bad] });
ok('冲突 → status=degraded', degraded.status === 'degraded');
ok('冲突写进 issues 且带两个来源',
  degraded.issues.some(i => i.code === 'SOURCE_CONFLICT' && i.sources.length === 2));
ok('冲突并列保留两源数值',
  degraded.verification.conflicts[0].primary.value === 10.06 &&
  degraded.verification.conflicts[0].secondary.value === -4.34);
ok('冲突时旧 ok 布尔不被篡改（数字照出）', degraded.ok === true);

ok('必需数据缺 → incomplete', CT.attach(legacy, { scope: 'x', requiredMissing: 2 }).status === 'incomplete');
ok('主流程失败 → failed', CT.attach({ ok: false }, { scope: 'x' }).status === 'failed');
ok('fatal 优先于 degraded',
  CT.attach({ ok: true }, { scope: 'x', fatal: true, checks: [bad] }).status === 'failed');

const unver = CT.attach(legacy, { scope: 'industry', checks: [single] });
ok('单源计入 unverified 而非 matched',
  unver.verification.unverified === 1 && unver.verification.matched === 0);
ok('单源只产生 info 提示、不误降级', unver.status === 'ok');
ok('单源提示写明不计入已验证',
  unver.issues.some(i => i.code === 'NO_SECOND_SOURCE' && /不计入/.test(i.message)));

ok('覆盖清单含独立双源 / 单源 / 新鲜度三类',
  ['independent', 'single-source', 'freshness'].every(k => RC.CATALOG.some(c => c.kind === k)));
ok('旧 errors[] 转 issues 后原数组仍在',
  CT.attach({ ok: true, errors: ['东财份额不可用'] }, { scope: 'etf', errorLevel: 'info' })
    .issues[0].level === 'info');

console.log('\n=== 容差校准：把「数字错了却判一致」的漏报堵住 ===');
const tol = RC.toleranceInfo();
ok('容差来自真实样本校准', tol.source === 'calibrated', 'observations=' + tol.sample);
ok('相对容差已按真实 P99 收紧（两个时段 profile 都远严于原值）',
  RC.TOLERANCE.priceRel <= 0.001 && RC.TOLERANCE.priceRel < RC.FALLBACK.priceRel,
  'priceRel=' + RC.TOLERANCE.priceRel);
ok('高价标的容差设了绝对上限', RC.TOLERANCE.priceMax <= 0.5,
  'priceMax=' + RC.TOLERANCE.priceMax);
const leak = RC.compare({ source: '新浪', value: 1435.26 }, { source: '腾讯', value: 1436.00 },
  RC.priceRule('t', 'p', '茅台 现价'));
ok('旧容差漏报的 0.74 元偏差现在被拦（原容差 7.18 元）',
  leak.result === 'mismatch', 'tol=' + leak.toleranceUsed);
const oneTick = RC.compare({ source: '新浪', value: 1435.26 }, { source: '腾讯', value: 1435.27 },
  RC.priceRule('t', 'p', '茅台 一个最小变动单位'));
ok('正常情况下一个最小变动单位不误报', oneTick.result === 'match',
  'tol=' + oneTick.toleranceUsed);
ok('过期快照（46247 vs 52297）仍被拦',
  RC.compare({ value: 46247.29 }, { value: 52297.09 },
    RC.priceRule('t', 'p', '道指')).result === 'mismatch');
ok('指数小数位微差仍判一致',
  RC.compare({ value: 52297.09 }, { value: 52297.0898 },
    RC.priceRule('t', 'p', '道指')).result === 'match');
const fb = RC.loadTolerance(require('path').join(__dirname, '..', 'data', '__not_exist__.json'));
ok('校准文件缺失时明示 fallback 而非伪装已校准',
  fb.source === 'fallback' && RC.TOLERANCE.priceRel === RC.FALLBACK.priceRel);
ok('重新加载可恢复 calibrated', RC.loadTolerance().source === 'calibrated');

console.log('\n=== 行情时点错位判据（可控样本，不依赖当前市场时段）===');
function fakeItems(n, pct) {
  return Array.from({ length: n }, function (_, i) {
    return { symbol: 'gb_x' + i, name: 'X' + i, price: 100 + i,
      prevClose: pct === 0 ? 100 + i : 99, changePct: pct };
  });
}
function fakeSec(items, pct) {
  var m = {};
  items.forEach(function (x) { m[x.symbol] = { source: '腾讯', value: x.price, changePct: pct }; });
  return m;
}
var pre = fakeItems(18, 0);
ok('盘前整批（主源全 0 且现价=昨收、第二源非零）识别为时点错位',
  RC.detectTimeSkew(pre, fakeSec(pre, -0.36)) === true);
var normal = fakeItems(18, -0.36);
ok('常态整批不误判', RC.detectTimeSkew(normal, fakeSec(normal, -0.36)) === false);
var mixed = fakeItems(18, -0.36);
mixed[0].changePct = 0; mixed[0].prevClose = mixed[0].price;
ok('个别标的异常不被洗成「时点差」', RC.detectTimeSkew(mixed, fakeSec(mixed, -0.36)) === false);
var tiny = fakeItems(2, 0);
ok('样本过少不触发判据', RC.detectTimeSkew(tiny, fakeSec(tiny, -0.36)) === false);
/* 真实踩坑：盘前「现价=昨收」只占 15/18，改错一只价格就曾让整批判据翻转，
   把 18 项不可比误报成冲突。判据改以「涨跌幅归零」为准后必须稳定。 */
var tampered = fakeItems(18, 0);
tampered[0].price += 0.74;
ok('单个标的价格被篡改不翻转整批时点判据',
  RC.detectTimeSkew(tampered, fakeSec(tampered, -0.36)) === true);
var skewItem = { scope: 't', field: 'x.pct', label: 'X 涨跌幅', result: 'unverifiable',
  primary: { source: '新浪', value: 0 }, secondary: { source: '腾讯', value: -0.36 },
  delta: 0.36, toleranceUsed: 0.05, comparability: 'time-skew', reason: '时点不同' };
var skewAtt = CT.attach({ ok: true }, { scope: 't', checks: [skewItem] });
ok('时点错位状态降级但不算数据冲突',
  skewAtt.status === 'degraded' && skewAtt.verification.mismatched === 0 &&
  skewAtt.verification.timeSkew === 1);
ok('时点错位有专属 issue 且不混入 NO_SECOND_SOURCE',
  skewAtt.issues.some(i => i.code === 'TIME_SKEW_UNCOMPARABLE') &&
  !skewAtt.issues.some(i => i.code === 'NO_SECOND_SOURCE'));
/* 两源都归 0 时虽无时点错位，联动结论仍不可用，状态也必须降级 */
var preAtt = CT.attach({ ok: true }, { scope: 't', issues: [
  CT.issue('PREMARKET_NOT_COMPARABLE', 'warn', null, '美股盘前，联动结论暂不可用')] });
ok('盘前结论不可用必须降级，不能出现 ok 却又说不可用',
  preAtt.status === 'degraded' &&
  preAtt.issues.some(i => i.code === 'PREMARKET_NOT_COMPARABLE'));

console.log('\n=== 美股盘中动态差异：不可比 ≠ 冲突，大错仍要拦 ===');
var dyn = RC.intradaySkew({ result: 'mismatch', label: '英伟达 现价',
  primary: { value: 167.22 }, secondary: { value: 167.84 },
  delta: 0.62, toleranceUsed: 0.1 }, 'price');
ok('盘中正常时间差（0.62 元 / 0.37%）标为不可比',
  dyn.result === 'unverifiable' && dyn.comparability === 'time-skew' &&
  dyn.primary.value === 167.22 && dyn.secondary.value === 167.84);
ok('盘中过期快照（46247 vs 52297）仍判冲突',
  RC.intradaySkew({ result: 'mismatch', label: '道指',
    primary: { value: 46247.29 }, secondary: { value: 52297.09 },
    delta: 6049.8, toleranceUsed: 0.1 }, 'price').result === 'mismatch');
ok('盘中昨收算错（+10.06 vs -4.34）仍判冲突',
  RC.intradaySkew({ result: 'mismatch', label: '英伟达 涨跌幅',
    primary: { value: 10.06 }, secondary: { value: -4.34 },
    delta: 14.4, toleranceUsed: 0.05 }, 'pct').result === 'mismatch');
ok('美股时段判定可调用', typeof RC.usTradingNow() === 'boolean');

(async () => {
  console.log('\n=== 真实数据源对账（实际联网） ===');
  let real = [];
  try {
    real = await RC.checkAQuotes(['600519', '000001', '600036']);
  } catch (e) {
    ok('真实行情对账可运行', false, e.message);
  }
  const matched = real.filter(x => x.result === 'match').length;
  const mismatched = real.filter(x => x.result === 'mismatch').length;
  ok('真实三源对账返回结果', real.length >= 6, 'checks=' + real.length);
  ok('真实对账至少 4 项一致', matched >= 4, 'matched=' + matched);
  ok('真实行情两源无冲突', mismatched === 0,
    mismatched ? real.filter(x => x.result === 'mismatch').map(x => x.label).join(' | ') : 'mismatched=0');

  console.log('\n=== 本地 K 线新鲜度校验（依赖 Python 引擎） ===');
  let kl = [];
  try { kl = await RC.checkLocalKline(['600519', '000001']); }
  catch (e) { ok('新鲜度校验可运行', false, e.message); }
  ok('新鲜度校验返回 2 项', kl.length === 2, 'n=' + kl.length);
  ok('新鲜度项如实标注 kind=freshness', kl.every(x => x.kind === 'freshness'));
  ok('新鲜度项不与独立双源混淆', kl.every(x => x.comparability === 'same-source-freshness'));

  console.log('\n=== SSE 可信度事件（真实对账，非构造）===');
  let qv = null;
  try { qv = await RC.analyzeQuality(['600519']); }
  catch (e) { ok('analyzeQuality 可运行', false, e.message); }
  if (qv) {
    ok('quality 事件类型与作用域', qv.type === 'quality' && qv.scope === 'analyze');
    ok('quality 事件状态合法', ['ok', 'degraded', 'incomplete', 'failed'].includes(qv.status));
    ok('quality 事件带真实对账结果', qv.checked >= 2 && qv.matched >= 1,
      'checked=' + qv.checked + ' matched=' + qv.matched);
    ok('quality 事件保留两源数值',
      (qv.items || []).some(x => x.primary && x.secondary));
    ok('quality 事件带容差来源', qv.tolerance && qv.tolerance.source === 'calibrated');
    ok('quality 事件带抽样代码', JSON.stringify(qv.sampled) === '["600519"]');
  }
  let qv2 = null;
  try {
    qv2 = await RC.analyzeQuality(['600519', '000001', '600036', '601318', '000858',
      '002594', '300750', '600030', '601899', '000333']);
  } catch (e) { /* 下面统一判失败 */ }
  ok('超过上限时如实写明「仅抽样 8 只」', !!qv2 &&
    qv2.sampled.length === 8 && /仅抽样核对前 8 只/.test(qv2.sampleNote || ''),
    qv2 ? 'sampled=' + qv2.sampled.length : 'no result');
  let qv3 = null;
  try { qv3 = await RC.analyzeQuality([]); } catch (e) { }
  ok('无有效代码时不假装已核对', !!qv3 && qv3.status === 'incomplete' && qv3.matched === 0);

  console.log('\n========================================');
  console.log('RESULT: pass=' + pass + ' fail=' + fail);
  if (fail) console.log('失败项: ' + fails.join(' | '));
  console.log('========================================');
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('ERR', e.message); process.exit(2); });
