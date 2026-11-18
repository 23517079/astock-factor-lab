#!/usr/bin/env node
/**
 * tools/calibrate_tolerance.js — 用真实行情样本校准 lib/reconcile.js 的对账容差
 *
 * 背景与诚实边界（重要，别把这份脚本的产出读成「历史三源偏差」）：
 *   本地 sequoia_v2.db 只有腾讯前复权这一种口径，库里不存在三源历史报价，
 *   因此**无法**从本地库统计跨源历史偏差序列。本地库的真实用途是：
 *   按最新交易日的历史收盘价，把 5030 只票分成价格量级，从每一档
 *   确定性抽样，保证样本覆盖 2 元低价股到 1400 元高价股，而不是随手挑几只蓝筹。
 *   跨源偏差本身来自「当前时点三源实时横截面」+「美股双源横截面」。
 *
 * 采集内容：
 *   A 股 / ETF：腾讯 qt.gtimg.cn、新浪 hq.sinajs.cn、东财 push2（fetchQuoteA）
 *   美股指数 / 龙头 / 中概：新浪 gb_$* / gb_*、腾讯 us*
 *   每对源逐标的记录 price 绝对/相对偏差、prevClose 绝对偏差、changePct 百分点偏差。
 *
 * 推导（工程边界不是统计结论，写进输出文件）：
 *   tolerance = clamp(P99(偏差) × 安全系数 3, FLOOR, CAP)
 *   FLOOR/CAP 是防止「收盘静态样本偏差全为 0」导致阈值过紧、
 *   以及防止高价标的相对容差失控的双重保险。
 *
 * 已知未覆盖：收盘后三源报价时间已对齐，样本偏差往往接近 0；
 *   **盘中因更新时差产生的动态偏差必须在交易时段复跑本脚本才能覆盖**。
 *
 * 用法：
 *   node tools/calibrate_tolerance.js                 # 校准并写 data/tolerance.json
 *   node tools/calibrate_tolerance.js --sample 80     # 每档多抽一些
 *   node tools/calibrate_tolerance.js --dry-run       # 只打印统计，不写文件
 *
 * 依赖：Node >= 22.5（node:sqlite，实验特性会打一条 warning，不影响结果）
 */
'use strict';

var fs = require('fs');
var path = require('path');
var S = require('../lib/sources.js');
var RC = require('../lib/reconcile.js');
var { DatabaseSync } = require('node:sqlite');

var ROOT = path.join(__dirname, '..');
var DB = path.join(ROOT, '..', 'sequoia-service', 'data', 'sequoia_v2.db');
var OUT = path.join(ROOT, 'data', 'tolerance.json');

var SAFETY = 3;
var FLOOR = { priceAbs: 0.02, priceRel: 0.0002, priceMax: 0.10, pctAbs: 0.05 };
var CAP = { priceAbs: 0.10, priceRel: 0.001, priceMax: 0.50, pctAbs: 0.30 };

/* 内置回退值：与校准前保持同一量级，但补上 priceMax 上限，避免高价股容差失控 */
var FALLBACK = { priceAbs: 0.02, priceRel: 0.005, priceMax: 0.50, pctAbs: 0.30 };

var BANDS = [
  { name: '超低价 <5 元', min: 0, max: 5 },
  { name: '低价 5-20 元', min: 5, max: 20 },
  { name: '中价 20-100 元', min: 20, max: 100 },
  { name: '高价 100-500 元', min: 100, max: 500 },
  { name: '超高价 >500 元', min: 500, max: Infinity }
];

function clamp(v, lo, hi) { return Math.min(Math.max(v, lo), hi); }
function r(v, d) { var p = Math.pow(10, d); return Math.round(v * p) / p; }
function finite(v) { return typeof v === 'number' && isFinite(v); }

function quantile(arr, p) {
  if (!arr.length) return 0;
  var a = arr.slice().sort(function (x, y) { return x - y; });
  var i = Math.ceil(p * a.length) - 1;
  return a[Math.min(Math.max(i, 0), a.length - 1)];
}
function stat(arr, digits) {
  var d = digits == null ? 6 : digits;
  return {
    n: arr.length,
    p50: r(quantile(arr, 0.50), d),
    p95: r(quantile(arr, 0.95), d),
    p99: r(quantile(arr, 0.99), d),
    max: arr.length ? r(Math.max.apply(null, arr), d) : 0
  };
}

/** 本地历史库按价格量级确定性抽样（用历史收盘价，不用随机，保证可复跑） */
function pickSample(perBand) {
  var db = new DatabaseSync(DB);
  var rows = [];
  try {
    rows = db.prepare(
      'SELECT symbol, close FROM stock_daily ' +
      'WHERE date = (SELECT MAX(date) FROM stock_daily) AND close > 0 ' +
      'ORDER BY symbol'
    ).all();
  } finally { db.close(); }
  var bands = BANDS.map(function (b) {
    return { name: b.name, list: rows.filter(function (x) {
      return x.close >= b.min && x.close < b.max;
    }) };
  });
  var out = [];
  bands.forEach(function (b) {
    var n = Math.min(perBand, b.list.length);
    for (var i = 0; i < n; i++) {
      /* 等间隔取，确保不总落在同一个字母段 */
      var idx = Math.floor((i + 0.5) * b.list.length / n);
      var row = b.list[Math.min(idx, b.list.length - 1)];
      out.push({ code: String(row.symbol), band: b.name, histClose: row.close });
    }
  });
  /* ETF 不在 hs_a 清单里，单独补几个低价品种 */
  ['510300', '510500', '159915', '512880'].forEach(function (c) {
    out.push({ code: c, band: 'ETF（补充）', histClose: null });
  });
  return {
    codes: out,
    bands: bands.map(function (b) { return { name: b.name, universe: b.list.length }; })
  };
}

async function mapLimit(items, limit, fn) {
  var out = new Array(items.length), cursor = 0;
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, async function () {
    while (cursor < items.length) {
      var i = cursor++;
      try { out[i] = await fn(items[i]); } catch (e) { out[i] = null; }
    }
  }));
  return out;
}

/**
 * 东财单只行情。
 * 不用 fetchQuoteA：它请求的 A_QUOTE_FIELDS（f2/f3/...）在该端点实测只返回
 * f62/f184，价格字段缺失，主源会悄悄降级到新浪 —— 拿它做第三源会变成同源自比。
 * 这里改用实测确实返回价格的字段组合 f43(现价)/f60(昨收)/f170(涨跌幅)。
 * 该发现已单独记录，本轮不改生产主源（会影响个股分析 PE/量比等字段）。
 */
function emSecid(code) {
  if (/^(60|68|11|5|6)/.test(code)) return '1.' + code;
  return '0.' + code;
}
async function eastMoneyMap(codes) {
  var vals = await mapLimit(codes, 6, async function (c) {
    try {
      var url = 'https://push2.eastmoney.com/api/qt/stock/get?fltt=2&invt=2&secid=' +
        emSecid(c) + '&fields=f43,f57,f58,f60,f169,f170';
      var r = await S.raw(url, { timeoutMs: 12000 });
      if (!r.ok) return null;
      var d = JSON.parse(r.text).data;
      var price = parseFloat(d && d.f43), prev = parseFloat(d && d.f60);
      if (!finite(price) || !finite(prev) || price <= 0) return null;
      /* 字段对齐 RC.tencentAMap / sinaAMap 的 {value, prevClose}，collectA 才能统一处理 */
      return { source: '东方财富 push2（f43/f60）', value: price,
        prevClose: prev, name: String((d && d.f58) || '') };
    } catch (e) { /* 单只失败不影响整轮 */ }
    return null;
  });
  var out = {};
  codes.forEach(function (c, i) { if (vals[i]) out[c] = vals[i]; });
  return out;
}

function pctOf(x) {
  if (!finite(x.price) || !finite(x.prevClose) || !x.prevClose) return null;
  return (x.price - x.prevClose) / x.prevClose * 100;
}

function pushObs(obs, pairName, field, a, b, band) {
  if (!finite(a) || !finite(b)) return;
  var d = Math.abs(a - b);
  obs.push({ pair: pairName, field: field, band: band, delta: d,
    rel: (field === 'price' && Math.abs(a) > 0) ? d / Math.abs(a) : null });
}

/** A 股三源横截面 */
async function collectA(sample) {
  var codes = sample.codes.map(function (x) { return x.code; });
  var [tencent, sina, em] = await Promise.all([
    RC.tencentAMap(codes), RC.sinaAMap(codes), eastMoneyMap(codes)
  ]);
  var obs = [];
  var coverage = { tencent: 0, sina: 0, eastmoney: 0 };
  sample.codes.forEach(function (s) {
    var c = s.code, t = tencent[c], n = sina[c], e = em[c];
    if (t) coverage.tencent++;
    if (n) coverage.sina++;
    if (e) coverage.eastmoney++;
    var pairs = [
      ['腾讯↔新浪', t, n], ['腾讯↔东财', t, e], ['新浪↔东财', n, e]
    ];
    pairs.forEach(function (p) {
      if (!p[1] || !p[2]) return;
      pushObs(obs, p[0], 'price', p[1].value, p[2].value, s.band);
      pushObs(obs, p[0], 'prevClose', p[1].prevClose, p[2].prevClose, s.band);
      pushObs(obs, p[0], 'changePct', pctOf({ price: p[1].value, prevClose: p[1].prevClose }),
        pctOf({ price: p[2].value, prevClose: p[2].prevClose }), s.band);
    });
  });
  return { obs: obs, coverage: coverage };
}

/** 美股双源横截面（新浪 ↔ 腾讯） */
async function collectUs() {
  var us = await S.fetchUsMarket();
  var items = (us.indices || []).concat(us.leaders || [], us.adr || []);
  var sec = await RC.tencentUsMap(items.map(function (x) { return x.symbol; }));
  var obs = [], coverage = { sina: 0, tencent: 0 };
  items.forEach(function (x) {
    if (!finite(x.price)) return;
    coverage.sina++;
    var s = sec[x.symbol];
    if (!s || !finite(s.value)) return;
    coverage.tencent++;
    pushObs(obs, '新浪↔腾讯', 'price', x.price, s.value, '美股');
    pushObs(obs, '新浪↔腾讯', 'prevClose', x.prevClose, s.prevClose, '美股');
    pushObs(obs, '新浪↔腾讯', 'changePct', x.changePct, s.changePct, '美股');
  });
  return { obs: obs, coverage: coverage, errors: us.errors || [] };
}

/* 与运行时共用同一个时段判定，避免采样时点与生效时点错配 */
var SESSION_LABEL = {
  closed: '收盘后静态时点（A 股与美股均非交易时段）',
  intraday: '交易时段（A 股或美股盘中，两源报价时间可能差数秒）'
};
function marketState() { return SESSION_LABEL[RC.sessionKey()]; }

function derive(obs) {
  var priceAbs = obs.filter(function (x) { return x.field === 'price'; })
    .map(function (x) { return x.delta; });
  var priceRel = obs.filter(function (x) { return x.field === 'price' && x.rel != null; })
    .map(function (x) { return x.rel; });
  var pct = obs.filter(function (x) { return x.field === 'changePct'; })
    .map(function (x) { return x.delta; });
  var prev = obs.filter(function (x) { return x.field === 'prevClose'; })
    .map(function (x) { return x.delta; });

  var s = {
    priceAbs: stat(priceAbs, 6), priceRel: stat(priceRel, 8),
    pctAbs: stat(pct, 6), prevCloseAbs: stat(prev, 6)
  };
  var tolerance = {
    priceAbs: r(clamp(s.priceAbs.p99 * SAFETY, FLOOR.priceAbs, CAP.priceAbs), 4),
    priceRel: r(clamp(s.priceRel.p99 * SAFETY, FLOOR.priceRel, CAP.priceRel), 6),
    priceMax: r(clamp(s.priceAbs.p99 * SAFETY * 5, FLOOR.priceMax, CAP.priceMax), 4),
    pctAbs: r(clamp(s.pctAbs.p99 * SAFETY, FLOOR.pctAbs, CAP.pctAbs), 4)
  };
  var byBand = {};
  obs.forEach(function (x) {
    if (x.field !== 'price') return;
    (byBand[x.band] = byBand[x.band] || []).push(x.delta);
  });
  Object.keys(byBand).forEach(function (k) { byBand[k] = stat(byBand[k], 6); });
  return { stats: s, byBand: byBand, tolerance: tolerance };
}

async function main() {
  var argv = process.argv.slice(2);
  var dry = argv.indexOf('--dry-run') >= 0;
  var si = argv.indexOf('--sample');
  var perBand = si >= 0 ? parseInt(argv[si + 1], 10) : 8;
  if (!finite(perBand) || perBand < 1 || perBand > 60) perBand = 8;

  console.log('读取本地历史库选样：' + DB);
  var sample = pickSample(perBand);
  console.log('  价格分档：' + sample.bands.map(function (b) {
    return b.name + '=' + b.universe + '只';
  }).join(' / '));
  console.log('  抽样 ' + sample.codes.length + ' 只（含 4 只 ETF）');

  console.log('拉取三源实时横截面…');
  var a = await collectA(sample);
  console.log('  A 股覆盖：腾讯 ' + a.coverage.tencent + ' / 新浪 ' + a.coverage.sina +
    ' / 东财 ' + a.coverage.eastmoney + '，观测 ' + a.obs.length + ' 条');
  var u = await collectUs();
  console.log('  美股覆盖：新浪 ' + u.coverage.sina + ' / 腾讯 ' + u.coverage.tencent +
    '，观测 ' + u.obs.length + ' 条' + (u.errors.length ? '，源错误：' + u.errors.join('；') : ''));

  var obs = a.obs.concat(u.obs);
  if (obs.length < 30) {
    console.error('观测样本不足（' + obs.length + ' 条），拒绝写入容差文件，避免把失败采样当成校准结果。');
    process.exit(3);
  }
  var d = derive(obs);
  console.log('\n偏差分位数（真实数据）：');
  console.log('  现价绝对差   ' + JSON.stringify(d.stats.priceAbs));
  console.log('  现价相对差   ' + JSON.stringify(d.stats.priceRel));
  console.log('  涨跌幅差(pp) ' + JSON.stringify(d.stats.pctAbs));
  console.log('  昨收绝对差   ' + JSON.stringify(d.stats.prevCloseAbs));
  console.log('\n推导容差：' + JSON.stringify(d.tolerance));
  console.log('  对照原值：' + JSON.stringify(FALLBACK));
  console.log('  高价股容差上限示例（1435.26 元）：原 ' +
    r(1435.26 * FALLBACK.priceRel, 3) + ' 元 → 现 ' +
    r(Math.min(Math.max(d.tolerance.priceAbs, 1435.26 * d.tolerance.priceRel), d.tolerance.priceMax), 3) + ' 元');

  var session = RC.sessionKey();
  var existing = {};
  try { existing = JSON.parse(fs.readFileSync(OUT, 'utf8')); } catch (e) { /* 首次运行 */ }
  var profiles = Object.assign({}, existing.profiles || {});
  /* 兼容本脚本改版前的单套结果：登记为它当时采样时点的 profile */
  if (!Object.keys(profiles).length && existing.tolerance) {
    profiles[existing.session || 'closed'] = {
      state: existing.marketState, tolerance: existing.tolerance,
      observations: existing.sample && existing.sample.observations,
      generatedAt: existing.generatedAt, stats: existing.stats
    };
  }
  profiles[session] = {
    state: marketState(), generatedAt: new Date().toISOString(),
    tolerance: d.tolerance, stats: d.stats, byBand: d.byBand,
    observations: obs.length,
    sample: { aShareCodes: sample.codes.length, perBand: perBand,
      aShareCoverage: a.coverage, usCoverage: u.coverage }
  };

  var payload = {
    version: 2,
    generatedAt: new Date().toISOString(),
    session: session,
    activeProfile: session,
    marketState: marketState(),
    method: '本地历史库按收盘价格量级确定性抽样 → 三源实时横截面偏差分位数；按市场时段分 profile',
    safetyFactor: SAFETY,
    floors: FLOOR, caps: CAP, fallback: FALLBACK,
    derivation: '各字段容差 = clamp(P99(真实跨源偏差) × 3, FLOOR, CAP)；' +
      'priceMax 另外限制高价标的相对容差失控；FLOOR/CAP 是工程安全边界而非统计结论',
    sample: {
      aShareCodes: sample.codes.length,
      bands: sample.bands,
      perBand: perBand,
      aShareCoverage: a.coverage,
      usCoverage: u.coverage,
      observations: obs.length,
      byBand: d.byBand,
      sources: ['腾讯 qt.gtimg.cn', '新浪 hq.sinajs.cn',
        '东财 push2（f43/f60 独立字段组合）',
        '新浪美股 gb_$* / gb_*', '腾讯美股 us*']
    },
    stats: d.stats,
    tolerance: d.tolerance,   /* 兼容只读顶层 tolerance 的调用方 */
    profiles: profiles,
    caveats: [
      '本地 sequoia_v2.db 只有腾讯前复权单一口径，无法提供跨源历史偏差序列；其用途是按历史收盘价挑选覆盖各价格量级的代表性样本',
      '容差按市场时段分 profile 保存：收盘静态时点两源报价时间对齐、偏差接近 0，容差可严；盘中两源更新时间可能差数秒，必须用盘中采样单独定标。运行时按 sessionKey 自动切换，无需重启',
      '本次采样时点为「' + marketState() + '」；若另一时段尚未采样，运行时会借用已有 profile 并在 toleranceInfo 标 profileFallback=true，绝不静默伪装成该时段已校准',
      '样本为有限横截面，样本外标的、涨跌停、除权日、开盘集合竞价、极端行情未覆盖',
      'FLOOR / CAP 为工程经验边界，不是统计推断结果；每次重新校准后需重跑故障注入与全量 e2e',
      '东财样本走独立字段组合 f43/f60：fetchQuoteA 所用的 A_QUOTE_FIELDS 在该端点实测只返回 f62/f184、价格字段缺失，主源会降级到新浪；若拿它当第三源会变成同源自比。此发现本轮只记录未改生产代码',
      '东财解析失败或返回无效价格的标的直接排除，不以 0 或其它源的数值填充'
    ].concat(a.coverage.eastmoney === 0 ? [
      '本次东财 f43/f60 无有效返回（盘中限流或字段缺失），A 股三源校准退化为腾讯↔新浪两源；' +
      '该时段 profile 的统计依据弱于收盘 profile，收盘后建议复跑'
    ] : [])
  };

  console.log('\n时段 profile：' + Object.keys(profiles).map(function (k) {
    return k + '=' + JSON.stringify(profiles[k].tolerance);
  }).join('  '));
  if (dry) { console.log('\n--dry-run：未写入文件'); return; }
  fs.mkdirSync(path.dirname(OUT), { recursive: true });
  fs.writeFileSync(OUT, JSON.stringify(payload, null, 2), 'utf8');
  console.log('\n已写入 ' + OUT + '（当前时段 ' + session + '）');
  console.log('重跑：node tools/calibrate_tolerance.js --sample ' + perBand);
}

main().catch(function (e) { console.error('校准失败：' + e.stack); process.exit(2); });
