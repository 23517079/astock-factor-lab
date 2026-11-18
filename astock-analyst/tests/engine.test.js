'use strict';
/* tests/engine.test.js — 因子计算与中美联动判定（node tests/engine.test.js） */

var assert = require('assert');
var E = require('../lib/engine.js');

var t = 0, fails = 0;
function ok(name, fn) {
  t++;
  try { fn(); console.log('  ✓ ' + name); }
  catch (e) { fails++; console.log('  ✗ ' + name + ' → ' + e.message); }
}
function section(s) { console.log('\n' + s); }

/* 构造 K 线：可指定收盘序列 */
function mkKline(closes, vols) {
  var rows = [];
  for (var i = 0; i < closes.length; i++) {
    rows.push({
      date: '2026-' + String(1 + (i % 28)).padStart(2, '0') + '-15',
      open: closes[i] * 0.995, close: closes[i], high: closes[i] * 1.01, low: closes[i] * 0.99,
      volume: vols ? vols[i] : 1000000, amount: closes[i] * 1000000,
      changePct: i > 0 ? ((closes[i] - closes[i - 1]) / closes[i - 1] * 100) : null, turnover: 1.2
    });
  }
  return { rows: rows, source: '测试日K', asOf: '2026-11-12' };
}

/* 连续上涨 40 日 */
var up = []; for (var i = 0; i < 40; i++) up.push(100 + i * 0.5);
var down = []; for (var j = 0; j < 40; j++) down.push(120 - j * 0.5);
var flat = []; for (var k = 0; k < 40; k++) flat.push(100 + (k % 2 ? 0.3 : -0.3));

section('[1] 技术面计算');
ok('上行趋势被正确识别', function () {
  var r = E.computeTechnical({ kline: mkKline(up) });
  assert.strictEqual(r.available, true);
  assert.ok(r.trend === '上行', '趋势判定为 ' + r.trend);
  assert.ok(r.slope20 > 0, '斜率应为正');
  assert.ok(r.aboveMa.length > 0, '应识别出站上均线');
});
ok('下行趋势被正确识别', function () {
  var r = E.computeTechnical({ kline: mkKline(down) });
  assert.strictEqual(r.trend, '下行');
  assert.ok(r.slope20 < 0);
});
ok('震荡被识别为震荡而非单边', function () {
  var r = E.computeTechnical({ kline: mkKline(flat) });
  assert.ok(r.trend.indexOf('震荡') >= 0, '趋势为 ' + r.trend);
});
ok('K线不足时明确标为不可用并说明缺什么', function () {
  var r = E.computeTechnical({ kline: { rows: [{ date: 'x', close: 1, high: 1, low: 1, volume: 1 }], source: 's' } });
  assert.strictEqual(r.available, false);
  assert.ok(r.missing[0].indexOf('日K') >= 0, 'missing 说明异常：' + r.missing[0]);
});
ok('均线、支撑压力与波动率都有具体数值', function () {
  var r = E.computeTechnical({ kline: mkKline(up) });
  assert.ok(E.isNum(r.ma5) && E.isNum(r.ma20));
  assert.ok(E.isNum(r.support20) && E.isNum(r.resistance20));
  assert.ok(E.isNum(r.volatilityAnnualPct) && r.volatilityAnnualPct > 0);
  assert.ok(E.isNum(r.ret20));
});
ok('量价形态给出可解释的判定与依据', function () {
  var vols = [];
  for (var i = 0; i < 40; i++) vols.push(i < 35 ? 500000 : 3000000);
  var closes = []; for (var j = 0; j < 40; j++) closes.push(100 + (j > 35 ? 3 : 0.1 * j));
  var r = E.computeTechnical({ kline: mkKline(closes, vols) });
  assert.ok(r.pattern && r.patternNote, '缺形态判定');
  assert.ok(r.patternNote.indexOf('%') >= 0 || r.patternNote.indexOf('倍') >= 0, '依据里应有数值：' + r.patternNote);
});
ok('成交密集区被识别且给出占比', function () {
  var r = E.computeTechnical({ kline: mkKline(up) });
  assert.ok(r.denseZone, '未识别密集区');
  assert.ok(E.isNum(r.denseZone.sharePct));
  assert.ok(r.denseZone.lower < r.denseZone.upper);
});
ok('趋势依据里同时含数值与时间', function () {
  var r = E.computeTechnical({ kline: mkKline(up) });
  assert.ok(r.trendBasis.length > 0);
  assert.ok(r.trendBasis.join(' ').indexOf('斜率') >= 0);
});

section('[2] 资金面 / 基本面 / 情绪 / 宏观');
ok('资金面聚合多项数据并标出缺失项', function () {
  var r = E.computeCapital({
    quote: { amount: 5506971989, turnoverPct: 5.05, asOf: '2026-11-12 15:34', source: '新浪' },
    mainFlow: { mainNet: 265629680, mainNet5d: 1200000000, date: '2026-11-12', source: '东财' },
    margin: { marginBalance: 9465945541, marginChange5dPct: 1.14, marginBuy5d: 1583028523, date: '2026-11-11', source: '东财两融' },
    northbound: { holdShares: 22000341, holdPctOfFloat: 1.0997, tradeDate: '2026-09-30', source: '东财北向' }
  });
  assert.strictEqual(r.available, true);
  assert.strictEqual(r.items.length, 4);
  r.items.forEach(function (it) {
    assert.ok(it.detail.indexOf('%') >= 0 || it.detail.indexOf('亿') >= 0 || it.detail.indexOf('元') >= 0,
      '缺数值：' + it.factor + ' → ' + it.detail);
  });
});
ok('两融与北向缺失时进入 missing 清单', function () {
  var r = E.computeCapital({ quote: null, mainFlow: null, margin: null, northbound: null });
  assert.strictEqual(r.available, false);
  assert.ok(r.missing.length >= 3, 'missing 数量不足：' + r.missing.length);
});
ok('基本面输出业绩同比与估值绝对值', function () {
  var r = E.computeFundamental({
    fundamentals: { reportDate: '2026-09-30', revenue: 60618050000, revenueYoy: 22.4349, netProfit: 7314195000,
      netProfitYoy: 91.6, roe: 4.6, grossMargin: 25.5895, prevNetProfitYoy: 94.2, prevReportDate: '2026-06-30', source: '东财业绩' },
    valuation: { peTtm: 132.72, pbMrq: 5.53, pePercentile: null, totalMarketCap: 956595715244, tradeDate: '2026-11-12', source: '东财估值' },
    sector: { sectorName: '半导体', asOf: '2026-11-12', source: '东财' }
  });
  assert.strictEqual(r.available, true);
  assert.ok(r.items.length >= 2);
  var d = r.items[0].detail;
  assert.ok(d.indexOf('%') >= 0, '业绩缺百分比');
  assert.ok(d.indexOf('22.43') >= 0, '同比未做小数位收敛：' + d);
  assert.ok(r.earningsTrend === '同比增速放缓', '业绩趋势判定异常：' + r.earningsTrend);
});
ok('估值分位缺失时如实说明，不用绝对值冒充分位', function () {
  var r = E.computeFundamental({ valuation: { peTtm: 30, pbMrq: 2, pePercentile: null, source: 'x' } });
  assert.ok(r.valuationNote.indexOf('未返回 PE 历史分位') >= 0, '说明缺失：' + r.valuationNote);
});
ok('情绪打分区分正负并给出命中词', function () {
  var r = E.scoreNewsSentiment([
    { title: '公司业绩增长超预期，订单大幅增加', media: 'm', date: 'd', url: 'u' },
    { title: '公司业绩增长超预期，产能扩产', media: 'm', date: 'd', url: 'u' },
    { title: '公司业绩下滑，遭机构减持', media: 'm', date: 'd', url: 'u' }
  ]);
  assert.strictEqual(r.available, true);
  assert.ok(r.score > 0, '分数应为正：' + r.score);
  assert.ok(r.positiveWords.length > 0 && r.negativeWords.length > 0);
  assert.ok(r.note.indexOf('非事实判断') >= 0, '缺启发式声明');
});
ok('无新闻时情绪标为不可用', function () {
  var r = E.scoreNewsSentiment([]);
  assert.strictEqual(r.available, false);
});
ok('宏观缺失政策原文时明确声明不判断政策取向', function () {
  var r = E.computeMacro({ usdcny: { value: 6.6682, changePct: -0.0869, date: '2026-11-12' },
    oil: { price: 86.6, changePct: 1.2, asOf: '22:45' }, gold: null, asOf: 'x' });
  assert.strictEqual(r.policyAvailable, false);
  var pol = r.items.filter(function (x) { return x.factor.indexOf('政策') >= 0; })[0];
  assert.ok(pol && pol.value.indexOf('未接入政策原文') >= 0);
});

section('[3] 中美联动判定');
var usUp = { indices: [{ name: '纳斯达克', changePct: 2.5, price: 25000, asOf: 'x', source: '新浪' },
  { name: '标普500', changePct: 1.8, price: 7500, asOf: 'x', source: '新浪' }], adr: [], source: '新浪' };
var usDown = { indices: [{ name: '纳斯达克', changePct: -2.5, price: 25000, asOf: 'x', source: '新浪' }], adr: [], source: '新浪' };
var leadersUp = { leaders: [{ name: '英伟达', changePct: 3.2, price: 180, asOf: 'x', source: '腾讯' },
  { name: '台积电', changePct: 2.8, price: 200, asOf: 'x', source: '腾讯' }], source: '腾讯' };
var leadersDown = { leaders: [{ name: '英伟达', changePct: -4.0, price: 170, asOf: 'x', source: '腾讯' }], source: '腾讯' };

function mkPack(changePct, sectorName) {
  return { quote: { price: 111.72, changePct: changePct, asOf: '2026-11-12', source: '新浪' },
    kline: mkKline(up), sector: { sectorName: sectorName || '半导体' } };
}
ok('美股涨 A股涨且幅度接近 → 判定联动', function () {
  var r = E.computeUsChinaLink(usUp, leadersUp, mkPack(2.4, '半导体'), null);
  var rel = r.relations.filter(function (x) { return x.type.indexOf('联动') >= 0; })[0];
  assert.ok(rel, '未产出联动关系');
  assert.ok(rel.text.indexOf('联动') >= 0);
});
ok('美股跌 A股涨 → 判定背离', function () {
  var r = E.computeUsChinaLink(usDown, leadersDown, mkPack(2.0, '半导体'), null);
  var rel = r.relations.filter(function (x) { return x.type === '背离'; })[0];
  assert.ok(rel, '未判定背离，实际：' + JSON.stringify(r.relations.map(function (x) { return x.type; })));
  assert.ok(rel.text.indexOf('背离') >= 0);
});
ok('方向一致但幅度差大 → 判定联动但幅度背离', function () {
  var r = E.computeUsChinaLink(usUp, leadersUp, mkPack(8.0, '半导体'), null);
  var rel = r.relations.filter(function (x) { return x.type.indexOf('幅度背离') >= 0; })[0];
  assert.ok(rel, '未判定幅度背离');
});
ok('传导路径覆盖估值/情绪/外资/产业链四项', function () {
  var r = E.computeUsChinaLink(usUp, leadersUp, mkPack(2.4, '半导体'), null);
  assert.ok(r.channels.length >= 4, '传导路径不足：' + r.channels.length);
  ['估值传导', '情绪传导', '外资偏好', '产业链映射'].forEach(function (n) {
    assert.ok(r.channels.some(function (c) { return c.name === n; }), '缺传导路径：' + n);
  });
});
ok('美股数据缺失时明确列入 missing，不硬编结论', function () {
  var r = E.computeUsChinaLink({ indices: [], adr: [], source: 'x' }, { leaders: [] }, mkPack(1.0), null);
  assert.ok(r.missing.length >= 2, 'missing 不足：' + r.missing.length);
  assert.ok(!r.relations.length, '无数据时不应产出关系判定');
});
ok('中概股数据参与联动结论', function () {
  var usWithAdr = { indices: usUp.indices, adr: [{ name: '阿里巴巴', changePct: -7.2, price: 94.3, asOf: 'x', source: '新浪' }], source: '新浪' };
  var r = E.computeUsChinaLink(usWithAdr, leadersUp, mkPack(2.4, '半导体'), null);
  var rel = r.relations.filter(function (x) { return x.type === '中概映射'; })[0];
  assert.ok(rel, '未产出中概映射');
  assert.ok(rel.text.indexOf('-7.2') >= 0, '中概数值未引用：' + rel.text);
});

section('[4] 汇总与完整性');
ok('buildAnalysis 输出五维并统计覆盖率', function () {
  var pack = {
    code: '688981', name: '中芯国际', collectedAt: '2026-11-12 22:00:00', elapsedMs: 100, errors: [],
    quote: { price: 111.72, changePct: 2.2, asOf: '2026-11-12 15:34', source: '新浪' },
    kline: mkKline(up), mainFlow: { mainNet: 1e8, mainNet5d: 1e9, date: '2026-11-12', source: '东财' },
    margin: { marginBalance: 9.4e9, marginChange5dPct: 1.14, date: '2026-11-11', source: '东财' },
    northbound: { holdShares: 2.2e7, holdPctOfFloat: 1.1, tradeDate: '2026-09-30', source: '东财' },
    fundamentals: { reportDate: '2026-09-30', revenue: 6e10, revenueYoy: 22, netProfit: 7e9, netProfitYoy: 91, roe: 4.6, source: '东财' },
    valuation: { peTtm: 132, pbMrq: 5.5, source: '东财' }, sector: { sectorName: '半导体', source: '东财' }
  };
  var a = E.buildAnalysis(pack, { us: usUp, usTencent: leadersUp, macro: { usdcny: { value: 6.66, date: '2026-11-12' } } },
    [{ title: '业绩增长', media: 'm', date: '2026-11-12', url: 'u' }]);
  assert.strictEqual(a.dims.length, 5);
  assert.ok(a.coverage.covered >= 4, '覆盖率偏低：' + a.coverage.covered);
  assert.ok(a.usChinaLink.relations.length > 0);
});

ok('ETF 不适用项不进缺失清单，缺失清单只剩真实缺口', function () {
  var pack = {
    code: '159206', name: '卫星ETF', collectedAt: '2026-11-12 22:00:00', elapsedMs: 100, errors: [],
    quote: { price: 1.206, changePct: -2.99, asOf: '2026-11-12 15:34', source: '新浪' },
    kline: mkKline(up), mainFlow: { mainNet: 1e8, mainNet5d: 1e9, date: '2026-11-12', source: '东财' },
    margin: null, northbound: null, fundamentals: null, valuation: null, sector: null,
    notApplicable: ['业绩报表（ETF 无财务报表）', '估值 PE/PB（ETF 不适用）', '行业分类（按基金跟踪指数判断）', '北向持股（ETF 不适用）'],
    notApplicableMap: {
      '业绩报表': '业绩报表（ETF 无财务报表，不属于数据缺失）',
      '估值分析': '估值 PE/PB（ETF 不适用，不属于数据缺失）',
      '行业分类': '行业分类（按基金跟踪指数判断，不属于数据缺失）',
      '北向持股': '北向持股（ETF 不适用，不属于数据缺失）'
    }
  };
  var a = E.buildAnalysis(pack, { us: usUp, usTencent: leadersUp, macro: { usdcny: { value: 6.66, date: '2026-11-12' } } },
    [{ title: '卫星互联网获批', media: 'm', date: '2026-11-12', url: 'u' }]);
  var missing = a.coverage.missing.join('；');
  assert.ok(missing.indexOf('业绩报表') < 0, '不适用项混进了缺失清单：' + missing);
  assert.ok(missing.indexOf('估值分析') < 0, '不适用项混进了缺失清单：' + missing);
  assert.ok(missing.indexOf('北向持股') < 0, '不适用项混进了缺失清单：' + missing);
  assert.ok(a.coverage.notApplicable.length === 4, '不适用清单应保留 4 项');
  assert.ok(a.coverage.notApplicableDims.indexOf('基本面') >= 0, '基本面维度应被标为不适用');
});

ok('情绪面可接收 news 对象并记录检索关键词', function () {
  var pack = {
    code: '159206', name: '卫星ETF', collectedAt: '2026-11-12 22:00:00', elapsedMs: 100, errors: [],
    quote: { price: 1.206, changePct: -2.99, asOf: '2026-11-12 15:34', source: '新浪' },
    kline: mkKline(up), mainFlow: null, margin: null, northbound: null,
    fundamentals: null, valuation: null, sector: null
  };
  var a = E.buildAnalysis(pack, { us: usUp, usTencent: leadersUp, macro: {} },
    { items: [{ title: '卫星互联网订单落地', media: 'm', date: '2026-11-12', url: 'u' }],
      source: '东方财富资讯搜索（关键词：卫星互联网）', keyword: '卫星互联网' });
  var sent = a.dims.filter(function (d) { return d.key === 'sentiment'; })[0];
  assert.ok(sent.data.available, '情绪面应可用');
  assert.strictEqual(sent.data.newsKeyword, '卫星互联网');
  assert.ok(sent.data.items[0].source.indexOf('卫星互联网') >= 0, '资讯来源应标注关键词');
});

console.log('\n结果：' + (t - fails) + '/' + t + ' 通过');
if (fails) process.exit(1);
