/**
 * lib/reconcile.js — 字段级第二源对账（L2）
 *
 * 真实数据源（2026-11-13 实测，非推测）：
 *   A 股个股 / ETF / 指数：东财 push2、新浪 hq.sinajs.cn、腾讯 qt.gtimg.cn 三源均通，
 *     实测 600519=1435.26、000001=12.07、510300=4.083、000001 指数=3634.35 逐位一致。
 *   美股指数 / 龙头 / 中概：新浪 gb_$* / gb_* 与腾讯 us* 双源一致（道指 52297.09）。
 *   行业强度：新浪 newSinaHy.php 单源，无独立第二源 → unverifiable。
 *   本地 SQLite K 线：腾讯前复权回填，与线上腾讯 fqkline 比对属于「同源不同路径的
 *     新鲜度校验」，明确标注 kind=freshness，不冒充独立第二源。
 *
 * 原则：主源与第二源同源时自动换源；第二源取不到就标 unverifiable + 原因；
 * 冲突时保留双方数值与来源，由 contract.js 降级，绝不静默取舍。
 */
'use strict';

var fs = require('fs');
var path = require('path');
var S = require('./sources.js');
var CT = require('./contract.js');
var FA = require('./factors.js');

/* 未校准时的内置回退值（与校准前同量级；priceMax 补上高价标的上限） */
var FALLBACK = { priceAbs: 0.02, priceRel: 0.005, priceMax: 0.50, pctAbs: 0.30 };
var TOLERANCE = {};
Object.keys(FALLBACK).forEach(function (k) { TOLERANCE[k] = FALLBACK[k]; });

var TOL_FILE = path.join(__dirname, '..', 'data', 'tolerance.json');
var TOL_META = { source: 'fallback', values: null, reason: '尚未加载' };
var TOL_PROFILES = {};
var TOL_ACTIVE_KEY = null;

/**
 * 统一的市场时段判定（北京时间）：
 *   A 股 9:30-11:30 / 13:00-15:00，美股 21:30-次日 05:00（覆盖夏令/冬令）。
 * 校准脚本与运行时共用这一份，避免「采样时点」与「生效时点」口径不一致。
 * 周六凌晨仍算 intraday（周五美股延续），周日一律 closed。
 */
function sessionKey(now) {
  var d = new Date((now || Date.now()) + 8 * 3600 * 1000);
  var m = d.getUTCHours() * 60 + d.getUTCMinutes();
  var wd = d.getUTCDay();
  if (wd === 0) return 'closed';
  if ((m >= 570 && m <= 690) || (m >= 780 && m <= 900)) return 'intraday';
  if (m >= 1290 || m <= 300) return 'intraday';
  return 'closed';
}
var SESSION_LABEL = {
  closed: '收盘后静态时点（A 股与美股均非交易时段）',
  intraday: '交易时段（A 股或美股盘中，两源报价时间可能差数秒）'
};

/** 美股是否处于交易时段（北京时间 21:30-次日 05:00，覆盖夏令/冬令） */
function usTradingNow(now) {
  var d = new Date((now || Date.now()) + 8 * 3600 * 1000);
  var m = d.getUTCHours() * 60 + d.getUTCMinutes();
  var wd = d.getUTCDay();
  if (wd === 0) return false;
  if (wd === 6) return m <= 300;          /* 周六凌晨 = 周五夜盘延续 */
  return m >= 1290 || m <= 300;
}

/**
 * 美股盘中：两源报价时间戳差几秒，价格/涨跌幅本就不该做静态一致性判定。
 * 只把「正常动态范围」内的差异转成 time-skew（未核对、保留两源数值）。
 * 硬红线：价格相对差 >5%、涨跌幅差 >2pp 一律保持 mismatch —— 过期快照（11%）
 * 和昨收算错（14pp）这两类真实 bug 在盘中也不能被洗掉。
 */
function intradaySkew(item, field) {
  if (!item || item.result !== 'mismatch' || !item.primary || !item.secondary) return item;
  var a = Math.abs(item.primary.value || 0), d = item.delta || 0;
  var within = field === 'price' ? (a > 0 && d / a <= 0.05) : d <= 2;
  if (!within) return item;
  item.result = 'unverifiable';
  item.comparability = 'time-skew';
  item.timeSkew = true;
  item.reason = '当前处于美股盘中交易时段，两源报价时间可能存在数秒差异' +
    '（按时段窗口推断，未逐条解析比对两源时间戳）；差异 ' + d +
    ' 落在正常动态范围内，静态一致性判定不适用，未判为冲突也不计入已验证' +
    '（超过 5% 或 2 个百分点仍会判冲突）';
  return item;
}

/**
 * 加载 tools/calibrate_tolerance.js 生成的容差配置。
 * 文件缺失/损坏/字段非法一律回退内置值，并把 source 明写为 fallback，
 * 绝不把「没校准」伪装成「已校准」。导出给测试验证回退路径。
 */
var TOL_KEYS = ['priceAbs', 'priceRel', 'priceMax', 'pctAbs'];
var TOL_FILE_META = {};

function validTol(t) {
  return !!t && TOL_KEYS.every(function (k) {
    return typeof t[k] === 'number' && isFinite(t[k]) && t[k] > 0;
  }) && t.priceMax >= t.priceAbs;
}

/** 按当前市场时段选 profile；跨时段无需重启服务，priceRule/pctRule 每次都会调它 */
function refreshTolerance(force) {
  var key = sessionKey();
  if (!force && key === TOL_ACTIVE_KEY && TOL_META.source) return;
  TOL_ACTIVE_KEY = key;
  var prof = TOL_PROFILES[key] || TOL_PROFILES.closed;
  if (!prof || !validTol(prof.tolerance)) {
    Object.keys(FALLBACK).forEach(function (k) { TOLERANCE[k] = FALLBACK[k]; });
    TOL_META = {
      source: 'fallback', file: TOL_FILE_META.file,
      reason: '当前时段无可用的校准 profile', values: Object.assign({}, FALLBACK),
      caveats: ['请在交易时段与收盘后各跑一次 tools/calibrate_tolerance.js，生成两套 profile']
    };
    return;
  }
  TOL_KEYS.forEach(function (k) { TOLERANCE[k] = prof.tolerance[k]; });
  TOL_META = {
    source: 'calibrated', file: TOL_FILE_META.file,
    profile: key, profiles: Object.keys(TOL_PROFILES),
    profileState: prof.state || SESSION_LABEL[key],
    /* 当前时段没做过校准时，明确标出「借用另一时段的 profile」，不伪装 */
    profileFallback: !TOL_PROFILES[key],
    generatedAt: prof.generatedAt || TOL_FILE_META.generatedAt,
    sample: prof.observations || TOL_FILE_META.sample,
    derivation: TOL_FILE_META.derivation,
    caveats: TOL_FILE_META.caveats,
    values: TOL_KEYS.reduce(function (o, k) { o[k] = prof.tolerance[k]; return o; }, {})
  };
}

function loadTolerance(file) {
  var f = file || TOL_FILE;
  TOL_PROFILES = {}; TOL_ACTIVE_KEY = null; TOL_FILE_META = {};
  try {
    var cfg = JSON.parse(fs.readFileSync(f, 'utf8'));
    if (!validTol(cfg.tolerance)) throw new Error('tolerance 字段缺失或非法');
    var ps = cfg.profiles || {};
    Object.keys(ps).forEach(function (k) {
      if (validTol(ps[k] && ps[k].tolerance)) TOL_PROFILES[k] = ps[k];
    });
    /* 兼容只有单套容差的旧格式 */
    if (!Object.keys(TOL_PROFILES).length) {
      TOL_PROFILES[cfg.session || 'closed'] = {
        state: cfg.marketState, tolerance: cfg.tolerance,
        observations: cfg.sample && cfg.sample.observations,
        generatedAt: cfg.generatedAt
      };
    }
    TOL_FILE_META = {
      file: path.basename(f), generatedAt: cfg.generatedAt,
      sample: cfg.sample && cfg.sample.observations,
      derivation: cfg.derivation, caveats: cfg.caveats || []
    };
    refreshTolerance(true);
  } catch (e) {
    Object.keys(FALLBACK).forEach(function (k) { TOLERANCE[k] = FALLBACK[k]; });
    TOL_META = {
      source: 'fallback', file: path.basename(f), reason: e.message,
      values: Object.assign({}, FALLBACK),
      caveats: ['未找到有效校准文件，使用内置经验值；请复跑 tools/calibrate_tolerance.js']
    };
    TOL_ACTIVE_KEY = null;
  }
  return toleranceInfo();
}

function toleranceInfo() {
  var out = { source: TOL_META.source, values: Object.assign({}, TOLERANCE) };
  Object.keys(TOL_META).forEach(function (k) {
    if (k !== 'source' && k !== 'values') out[k] = TOL_META[k];
  });
  return out;
}

loadTolerance();

var PY_ENGINE = process.env.ASTOCK_SEQUOIA_URL || 'http://127.0.0.1:8791';

/* 因子面板缓存的清单文件（JSON，Node 直接可读，不需要碰 SQLite） */
var PANEL_META = process.env.ASTOCK_PANEL_META ||
  path.join(__dirname, '..', '..', 'sequoia-service', 'data', 'panel_cache', 'meta.json');

/* 面向用户与页面的「覆盖清单」：哪些是独立双源、哪些是单源、哪些是新鲜度校验 */
var CATALOG = [
  { scope: 'us-linkage', label: '美股指数 / 科技龙头 / 中概 ADR', primary: '新浪 gb_$* / gb_*',
    secondary: '腾讯 us*', kind: 'independent', fields: ['price', 'changePct'],
    note: '逐标的比对现价与涨跌幅；腾讯无报价的标的标为未核对' },
  { scope: 'portfolio', label: '持仓标的实时行情', primary: '东财 / 新浪 / 腾讯（按实际降级结果）',
    secondary: '与主源不同的另一家', kind: 'independent', fields: ['price', 'prevClose'],
    note: '主源与第二源同源时自动换源，避免自己对自己' },
  { scope: 'etf', label: '宽基 ETF 现价', primary: '腾讯 qt.gtimg.cn',
    secondary: '新浪 hq.sinajs.cn', kind: 'independent', fields: ['price'],
    note: '份额 / 净申购为东财单源，标为未核对' },
  { scope: 'industry', label: '行业强度与领涨股', primary: '新浪 newSinaHy.php',
    secondary: null, kind: 'single-source', fields: ['changePct', 'strength'],
    note: '本面只有新浪一个数据源，不做独立核对，也不声称已验证' },
  { scope: 'local-kline', label: '本地 K 线最新收盘', primary: '本地 SQLite（腾讯前复权回填）',
    secondary: '腾讯 fqkline 线上最新收盘', kind: 'freshness', fields: ['close'],
    note: '同源不同路径的新鲜度校验，用于发现本地库过期或损坏，不是独立第二源' },
  /* ⚠ 命名陷阱：这里的 freshness 是**对账类型**（kind），与 public/freshness.js
     里「因子数据算到哪一天」完全是两件事。所以本 scope 叫 factor-panel，
     不要改成 freshness —— 否则和 kind=freshness 混成一团。 */
  { scope: 'factor-panel', label: '因子面板缓存与因子库的覆盖一致性',
    primary: 'factor_api.json 的 freshness.dataThrough（库内 factor_panel 末端）',
    secondary: 'data/panel_cache/meta.json 的 dates 末端（缓存自己声明的末端）',
    kind: 'freshness', fields: ['dateMax', 'factors'],
    note: '同源不同路径的新鲜度校验：缓存落后于库 = 当日增量没进缓存，' +
      '名单可能基于旧面板（此时 freshness 看不出来，因为它读的是库）。不是独立第二源' },
  { scope: 'a-quote', label: '个股实时行情（个股分析页依赖）',
    primary: '新浪 hq.sinajs.cn（名义主源东财因该端点字段缺失而降级）',
    secondary: '腾讯 qt.gtimg.cn', kind: 'independent', fields: ['price', 'prevClose'],
    note: '/api/analyze 是 SSE 流不套 JSON 信封，改由 quality 事件回传真实对账结果；每次最多抽样 8 只' }
];

function num(v) { return typeof v === 'number' && isFinite(v); }
function round(v, d) { var p = Math.pow(10, d || 0); return Math.round(v * p) / p; }

function aSym(code) {
  var c = String(code || '').trim();
  if (!/^\d{6}$/.test(c)) return null;
  if (/^(6|5|1|9)/.test(c)) return 'sh' + c;
  if (/^(0|2|3)/.test(c)) return 'sz' + c;
  return null;
}

function sinaToTencentUs(sym) {
  var fixed = {
    'gb_$dji': 'usDJI', 'gb_$ixic': 'usIXIC', 'gb_$inx': 'usINX',
    'int_dji': 'usDJI', 'int_nasdaq': 'usIXIC', 'int_sp500': 'usINX'
  };
  if (fixed[sym]) return fixed[sym];
  var m = /^gb_([a-z0-9]+)$/i.exec(String(sym || ''));
  return m ? 'us' + m[1].toUpperCase() : null;
}

function unverified(scope, field, label, reason, primary, kind) {
  return {
    scope: scope, field: field, label: label, result: 'unverifiable',
    primary: primary || null, secondary: null, delta: null, toleranceUsed: null,
    reason: reason, kind: kind || 'single-source', comparability: 'none'
  };
}

/**
 * 纯函数：比对一个字段的主源值与第二源值。
 * 故障注入测试直接调这个函数，证明「改错一个数字会被拦住」。
 */
function compare(primary, secondary, rule) {
  var item = {
    scope: rule.scope || '', field: rule.field || '', label: rule.label || rule.field || '',
    result: 'unverifiable',
    primary: primary || null, secondary: secondary || null,
    delta: null, toleranceUsed: null, reason: '',
    kind: rule.kind || 'independent', comparability: rule.comparability || 'same'
  };
  if (!primary || !num(primary.value)) { item.reason = '主源无有效数值'; return item; }
  if (!secondary || !num(secondary.value)) {
    item.reason = rule.noSecondReason || '第二数据源不可用或未返回该标的';
    return item;
  }
  var d = Math.abs(primary.value - secondary.value);
  item.delta = round(d, 6);
  var tol = rule.tolerance != null ? rule.tolerance : TOLERANCE.priceAbs;
  if (rule.toleranceMode === 'relative') {
    var rel = rule.relative != null ? rule.relative : TOLERANCE.priceRel;
    tol = Math.max(tol, Math.abs(primary.value) * rel);
    /* 校准后新增：高价标的 / 指数点位不得因相对容差把容差放大到几十元以上。
       旧值 0.5% 让 1435 元的茅台容差达 7.18 元、道指达 261 点，属于漏报。 */
    var cap = rule.toleranceCap != null ? rule.toleranceCap : TOLERANCE.priceMax;
    tol = Math.min(tol, cap);
  }
  item.toleranceUsed = round(tol, 6);
  item.result = d <= tol ? 'match' : 'mismatch';
  item.reason = item.result === 'match'
    ? (rule.kind === 'freshness' ? '本地库与线上最新收盘一致（新鲜度校验）' : '两源一致')
    : '两源超出容差';
  return item;
}

function priceRule(scope, field, label) {
  /* 按当前时段选 profile，跨收盘/盘中切换无需重启服务 */
  refreshTolerance();
  return { scope: scope, field: field, label: label, tolerance: TOLERANCE.priceAbs,
    toleranceMode: 'relative', relative: TOLERANCE.priceRel,
    toleranceCap: TOLERANCE.priceMax, kind: 'independent' };
}
function pctRule(scope, field, label) {
  refreshTolerance();
  return { scope: scope, field: field, label: label, tolerance: TOLERANCE.pctAbs,
    toleranceMode: 'absolute', kind: 'independent' };
}

/* ---------------- 第二源取数 ---------------- */

async function tencentAMap(codes) {
  var syms = codes.map(aSym).filter(Boolean);
  if (!syms.length) return {};
  var r = await S.rawGbk('https://qt.gtimg.cn/q=' + syms.join(','), { timeoutMs: 10000 });
  var map = {};
  if (!r.ok) return map;
  r.text.split(/\n/).forEach(function (line) {
    var m = line.match(/v_(s[hz]\d{6})="([^"]*)"/);
    if (!m) return;
    var a = m[2].split('~');
    if (a.length < 5 || !num(parseFloat(a[3]))) return;
    map[m[1].slice(2)] = {
      source: '腾讯行情 qt.gtimg.cn', value: parseFloat(a[3]),
      prevClose: parseFloat(a[4]), asOf: a[30] || null
    };
  });
  return map;
}

async function sinaAMap(codes) {
  var syms = codes.map(aSym).filter(Boolean);
  if (!syms.length) return {};
  var r = await S.rawGbk('https://hq.sinajs.cn/list=' + syms.join(','),
    { referer: 'https://finance.sina.com.cn', timeoutMs: 10000 });
  var map = {};
  if (!r.ok) return map;
  r.text.split(/\n/).forEach(function (line) {
    var m = line.match(/hq_str_(s[hz]\d{6})="([^"]*)"/);
    if (!m) return;
    var a = m[2].split(',');
    if (a.length < 10 || !num(parseFloat(a[3]))) return;
    map[m[1].slice(2)] = {
      source: '新浪行情 hq.sinajs.cn', value: parseFloat(a[3]),
      prevClose: parseFloat(a[2]), asOf: (a[30] || '') + ' ' + (a[31] || '')
    };
  });
  return map;
}

async function tencentUsMap(sinaSymbols) {
  var wanted = {};
  sinaSymbols.forEach(function (s) {
    var t = sinaToTencentUs(s);
    if (t) wanted[t] = s;
  });
  var list = Object.keys(wanted);
  if (!list.length) return {};
  var r = await S.rawGbk('https://qt.gtimg.cn/q=' + list.join(','), { timeoutMs: 10000 });
  var map = {};
  if (!r.ok) return map;
  r.text.split(/\n/).forEach(function (line) {
    var m = line.match(/v_(us[A-Z0-9]+)="([^"]*)"/);
    if (!m) return;
    var a = m[2].split('~');
    if (a.length < 35 || !num(parseFloat(a[3]))) return;
    var pct = parseFloat(a[32]);
    map[wanted[m[1]]] = {
      source: '腾讯行情（美股）', value: parseFloat(a[3]),
      prevClose: parseFloat(a[4]), changePct: num(pct) ? round(pct, 2) : null,
      asOf: a[30] || null
    };
  });
  return map;
}

async function localKlineLatest(codes) {
  var url = PY_ENGINE + '/api/kline/latest?codes=' + encodeURIComponent(codes.join(','));
  var r = await fetch(url, { signal: AbortSignal.timeout(10000) });
  var j = await r.json();
  var out = {};
  (j && j.rows || []).forEach(function (x) { out[x.code] = x; });
  return out;
}

async function tencentKlineLatest(codes) {
  var jobs = codes.map(async function (code) {
    var sym = aSym(code);
    if (!sym) return null;
    var r = await S.raw('https://ifzq.gtimg.cn/appstock/app/fqkline/get?param=' +
      sym + ',day,,,10,qfq', { timeoutMs: 10000 });
    if (!r.ok) return null;
    var j = JSON.parse(r.text);
    var d = j && j.data && j.data[sym];
    var rows = d && (d.qfqday || d.day);
    if (!rows || !rows.length) return null;
    var last = rows[rows.length - 1];
    return {
      source: '腾讯 fqkline（前复权）', value: parseFloat(last[2]),
      date: last[0], asOf: last[0]
    };
  });
  var vals = await Promise.all(jobs);
  var out = {};
  codes.forEach(function (c, i) { if (vals[i]) out[c] = vals[i]; });
  return out;
}

/** 按主源名称挑一个不同的第二源 */
function pickSecondary(quoteSource, tencent, sina) {
  if (/腾讯/.test(quoteSource || '')) return sina;
  return tencent;
}

/* ---------------- 各数据面的 checker ---------------- */

/**
 * 「行情时点错位」条目：不是数据错，也不是已核对，而是两源不可比。
 * 保留两源数值与时点，结果记 unverifiable（comparability=time-skew）。
 */
function timeSkewItem(scope, x, s) {
  var p = { source: '新浪财经全球行情', value: x.changePct, asOf: x.usTime || null };
  var q = { source: s.source, value: s.changePct, asOf: s.asOf };
  return {
    scope: scope, field: x.symbol + '.changePct',
    label: x.name + ' (' + x.symbol + ') 涨跌幅',
    result: 'unverifiable', primary: p, secondary: q,
    delta: round(Math.abs((p.value || 0) - q.value), 6),
    toleranceUsed: TOLERANCE.pctAbs,
    reason: '两源行情时点不同：新浪已进入新交易日盘前（现价=昨收、涨跌幅 0），' +
      '腾讯仍为上一交易日收盘 ' + s.changePct + '%；涨跌幅口径不可比，未做一致性判定',
    kind: 'independent', comparability: 'time-skew', timeSkew: true
  };
}

/**
 * 整批「行情时点错位」判据（抽成纯函数便于故障注入测试）。
 * 严格三条件，避免把个别标的的真实错误洗成「时点差」：
 *   ① 有效配对 >= 5；② >=80% 主源涨跌幅为 0（盘前的本质特征）；
 *   ③ >=50% 第二源仍有明显非零涨跌幅（上一交易日收盘口径）。
 *
 * 不把「现价=昨收」当硬条件：实测盘前 18 个标的里只有 15 个满足，
 * 比例刚好贴着阈值；此时只改错一只票的价格就会让整批判据翻转，
 * 把 18 项「不可比」一次性误报成冲突。价格本身的错由价格检查单独拦。
 */
function detectTimeSkew(items, sec) {
  var paired = items.filter(function (x) { return sec[x.symbol]; });
  if (paired.length < 5) return false;
  var flat = paired.filter(function (x) { return x.changePct === 0; }).length;
  var live = paired.filter(function (x) {
    return num(sec[x.symbol].changePct) && Math.abs(sec[x.symbol].changePct) > 0.05;
  }).length;
  return flat / paired.length >= 0.8 && live / paired.length >= 0.5;
}

function compareUsItems(items, scope) {
  var syms = items.map(function (x) { return x.symbol; });
  return tencentUsMap(syms).then(function (sec) {
    var timeSkew = detectTimeSkew(items, sec);
    var intraday = usTradingNow();
    var checks = [];
    items.forEach(function (x) {
      var base = x.name + ' (' + x.symbol + ')';
      var s = sec[x.symbol];
      var p = { source: '新浪财经全球行情', value: x.price, asOf: x.usTime || null };
      var sPrice = s ? { source: s.source, value: s.value, asOf: s.asOf } : null;
      var priceCheck = compare(p, sPrice,
        priceRule(scope, x.symbol + '.price', base + ' 现价'));
      checks.push(intraday ? intradaySkew(priceCheck, 'price') : priceCheck);

      var pctCheck;
      if (timeSkew && s && num(s.changePct)) {
        pctCheck = timeSkewItem(scope, x, s);
      } else {
        var sPct = (s && num(s.changePct))
          ? { source: s.source, value: s.changePct, asOf: s.asOf } : null;
        pctCheck = compare(
          { source: '新浪财经全球行情', value: x.changePct, asOf: x.usTime || null },
          sPct, pctRule(scope, x.symbol + '.changePct', base + ' 涨跌幅'));
      }
      checks.push(intraday ? intradaySkew(pctCheck, 'pct') : pctCheck);
    });
    return checks;
  });
}

async function checkUsLinkage(out) {
  var items = (out.us.indices || []).concat(out.us.tech || [], out.us.adr || []);
  return compareUsItems(items, 'us-linkage');
}

async function checkPortfolio(out) {
  var rows = (out.positions || []).filter(function (r) { return r.quoteOk && num(r.price); });
  if (!rows.length) return [];
  var codes = rows.map(function (r) { return r.code; });
  var both = await Promise.all([tencentAMap(codes), sinaAMap(codes)]);
  var checks = [];
  rows.forEach(function (r) {
    var sec = pickSecondary(r.quoteSource, both[0][r.code], both[1][r.code]);
    var base = (r.name || r.code) + ' (' + r.code + ')';
    var ps = r.quoteSource || '主源行情';
    checks.push(compare(
      { source: ps, value: r.price, asOf: out.asOf },
      sec ? { source: sec.source, value: sec.value, asOf: sec.asOf } : null,
      priceRule('portfolio', r.code + '.price', base + ' 现价')));
    checks.push(compare(
      { source: ps, value: r.prevClose, asOf: out.asOf },
      (sec && num(sec.prevClose)) ? { source: sec.source, value: sec.prevClose, asOf: sec.asOf } : null,
      priceRule('portfolio', r.code + '.prevClose', base + ' 昨收')));
  });
  return checks;
}

async function checkEtf(out) {
  var cards = (out.cards || []).filter(function (c) { return num(c.price); });
  var checks = [];
  if (cards.length) {
    var codes = cards.map(function (c) { return c.code; });
    var sec = await sinaAMap(codes);
    cards.forEach(function (c) {
      var s = sec[c.code];
      checks.push(compare(
        { source: c.source || '腾讯行情 qt.gtimg.cn', value: c.price, asOf: c.asOf },
        s ? { source: s.source, value: s.value, asOf: s.asOf } : null,
        priceRule('etf', c.code + '.price', (c.name || c.code) + ' 现价')));
    });
  }
  /* 份额 / 净申购：东财单源，无论有没有值都不声称已核对 */
  checks.push(unverified('etf', 'etf.shares', 'ETF 份额 / 净申购',
    '东财 clist 为本面唯一来源，无独立第二源；不可用时不以 0 填充',
    { source: '东方财富 clist', value: null, asOf: out.asOf }));
  return checks;
}

async function checkIndustry(out) {
  return [unverified('industry', 'industry.changePct', '行业板块涨幅 / 强度',
    '新浪 newSinaHy.php 是行业强度的唯一来源，没有可独立核对的第二数据源；本面数字不作为已验证数据',
    { source: '新浪行业板块', value: null, asOf: out.asOf })];
}

/** 个股行情（供 /api/data-quality 预热，覆盖 SSE 个股分析页依赖的数据源） */
async function checkAQuotes(codes) {
  var primary = await Promise.all(codes.map(async function (c) {
    try { return await S.fetchQuoteA(c); } catch (e) { return { ok: false }; }
  }));
  var both = await Promise.all([tencentAMap(codes), sinaAMap(codes)]);
  var checks = [];
  codes.forEach(function (c, i) {
    var q = primary[i];
    if (!q || !q.ok || !q.quote) {
      checks.push(unverified('a-quote', c + '.price', c + ' 现价', '主源行情获取失败', null));
      return;
    }
    var sec = pickSecondary(q.source || '', both[0][c], both[1][c]);
    var base = (q.quote.name || c) + ' (' + c + ')';
    checks.push(compare(
      { source: q.source || '主源行情', value: q.quote.price, asOf: q.quote.asOf },
      sec ? { source: sec.source, value: sec.value, asOf: sec.asOf } : null,
      priceRule('a-quote', c + '.price', base + ' 现价')));
    checks.push(compare(
      { source: q.source || '主源行情', value: q.quote.prevClose, asOf: q.quote.asOf },
      (sec && num(sec.prevClose)) ? { source: sec.source, value: sec.prevClose, asOf: sec.asOf } : null,
      priceRule('a-quote', c + '.prevClose', base + ' 昨收')));
  });
  return checks;
}

/** 本地 K 线新鲜度校验（同源不同路径，明确标注 kind=freshness） */
async function checkLocalKline(codes) {
  var local, online;
  try { local = await localKlineLatest(codes); }
  catch (e) { local = {}; }
  try { online = await tencentKlineLatest(codes); }
  catch (e) { online = {}; }
  return codes.map(function (c) {
    var l = local[c], o = online[c];
    var rule = priceRule('local-kline', c + '.close', c + ' 本地最新收盘');
    rule.kind = 'freshness';
    rule.comparability = 'same-source-freshness';
    rule.noSecondReason = 'Python 引擎或线上 K 线不可用，无法做新鲜度校验';
    return compare(
      l ? { source: '本地 SQLite（腾讯前复权，' + (l.date || '日期未知') + '）', value: l.close, asOf: l.date } : null,
      o ? { source: o.source + '（' + (o.date || '日期未知') + '）', value: o.value, asOf: o.date } : null,
      rule);
  });
}

/**
 * 纯整数日期序号（"YYYY-MM-DD" → 连续日序号），只用于求两个日期相差几天。
 *
 * 刻意**不用 `new Date()`**：一是这里只做日历差、不需要时区语义；二是 T05 有一条
 * 硬规则——前端不得用 `new Date(...)` 推算数据日期，grep 断言会扫到同名的写法。
 * 算法是 Fliegel–Van Flandern 儒略日，输入非法返回 null。
 */
function dayIndex(s) {
  var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s || ''));
  if (!m) return null;
  var y = +m[1], mo = +m[2], d = +m[3];
  var a = Math.floor((14 - mo) / 12);
  var yy = y + 4800 - a;
  var mm = mo + 12 * a - 3;
  return d + Math.floor((153 * mm + 2) / 5) + 365 * yy +
    Math.floor(yy / 4) - Math.floor(yy / 100) + Math.floor(yy / 400) - 32045;
}

/**
 * 因子面板：**缓存**与**因子库**的覆盖一致性（同源不同路径，kind=freshness）。
 *
 * 为什么需要这条：`freshness.dataThrough` 读的是库里 `factor_panel` 的 MAX(date)，
 * 而页面上的 IC / 名单是拿**缓存**算的。缓存若落后于库（当日增量写进了库、
 * 但没提交到 `panel_cache`），freshness 仍会显示「数据已最新」——因为库确实新了。
 * 这个 gap 只能靠比「两个文件各自声明的末端」发现：一处是导出时的库内末端，
 * 一处是 `panel_cache/meta.json` 自己写的 `dates` 末端。
 *
 * 全部字段缺失时返回 unverified（并给出可操作原因），绝不静默当作一致。
 */
async function checkFactorPanel() {
  var api = null;
  try { api = FA.load(); } catch (e) { api = { ok: false, message: e.message }; }
  if (!api || !api.ok) {
    return [unverified('factor-panel', 'export', '因子体检导出文件',
      '因子数据尚未导出（' + ((api && api.message) || 'factor_api.json 不可用') +
      '），无法核对面板覆盖', null, 'freshness')];
  }

  var cache = null, cacheErr = null;
  try { cache = JSON.parse(fs.readFileSync(PANEL_META, 'utf8')); }
  catch (e) { cacheErr = e.message; }
  if (!cache || typeof cache !== 'object') {
    return [unverified('factor-panel', 'cache', '面板缓存清单',
      '面板缓存 meta.json 不可读（' + (cacheErr || '内容为空') +
      '）；在这之前名单是用逐因子 SQL 回落算出来的，慢但结果可用', null, 'freshness')];
  }

  var fresh = api.freshness || {};
  var panel = (api.meta || {}).panel || {};
  var dbLast = fresh.dataThrough || panel.dateMax || null;
  var dates = cache.dates || [];
  var cacheLast = dates.length ? dates[dates.length - 1] : null;
  var checks = [];

  /* 1) 末端日期：库里面板末端 vs 缓存声明的末端。
        容差 0 天 —— 缓存只要比库旧，就说明当日增量没进缓存。 */
  var ruleDate = {
    scope: 'factor-panel', field: 'dateMax', label: '面板末端日期（缓存 vs 库）',
    tolerance: 0, kind: 'freshness', comparability: 'same-source-freshness',
    noSecondReason: '面板缓存 meta.json 里没有 dates 轴'
  };
  var itemDate = compare(
    dbLast ? { source: '因子库 factor_panel（' + dbLast + '）', value: dayIndex(dbLast), asOf: dbLast } : null,
    cacheLast ? { source: '面板缓存 meta.json（' + cacheLast + '）', value: dayIndex(cacheLast), asOf: cacheLast } : null,
    ruleDate);
  if (itemDate.result === 'match') {
    itemDate.reason = '缓存与库内面板同一天（' + dbLast + '），没有待提交的增量';
  } else if (itemDate.result === 'mismatch') {
    itemDate.reason = '缓存停在 ' + cacheLast + '，库里已到 ' + dbLast + '，相差 ' +
      itemDate.delta + ' 个自然日 —— 名单可能基于旧面板';
  }
  checks.push(itemDate);

  /* 2) 因子个数：缓存里落了几个因子的矩阵 vs 导出时库里有多少个因子。
        少了就说明某个因子没写进缓存 → 那次计算会静默回落到逐因子 SQL。 */
  var cacheCount = Object.keys(cache.factors || {}).length;
  var dbCount = panel.panelFactors;
  var ruleCnt = {
    scope: 'factor-panel', field: 'factors', label: '缓存因子数（缓存 vs 库）',
    tolerance: 0, kind: 'freshness', comparability: 'same-source-freshness',
    noSecondReason: '导出文件里没有 meta.panel.panelFactors'
  };
  var itemCnt = compare(
    num(dbCount) ? { source: '因子库 factor_panel（' + dbCount + ' 个）', value: dbCount } : null,
    num(cacheCount) ? { source: '面板缓存 meta.json（' + cacheCount + ' 个）', value: cacheCount } : null,
    ruleCnt);
  if (itemCnt.result === 'match') {
    itemCnt.reason = '缓存与库的因子数一致（' + dbCount + ' 个）';
  } else if (itemCnt.result === 'mismatch') {
    itemCnt.reason = '缓存只有 ' + cacheCount + ' 个因子矩阵，库里是 ' + dbCount +
      ' 个 —— 缺的因子会走逐因子 SQL 回落（秒级变分钟级）';
  }
  checks.push(itemCnt);
  return checks;
}

/* ---------------- 汇总缓存 ---------------- */
var CACHE = { scopes: {}, updatedAt: null };

function record(scope, verification) {
  CACHE.scopes[scope] = verification;
  CACHE.updatedAt = new Date().toISOString();
}

function snapshot() {
  var t = toleranceInfo();
  var tolText = t.source === 'calibrated'
    ? '真实样本校准（' + (t.sample || 0) + ' 条跨源观测，' + (t.marketState || '时点未知') + '）'
    : '内置经验值（未校准，' + (t.reason || '无校准文件') + '）';
  return {
    ok: true,
    asOf: CACHE.updatedAt,
    scopes: CACHE.scopes,
    totals: CT.summarize(CACHE.scopes),
    catalog: CATALOG,
    tolerance: t,
    note: '未核对（unverified）不等于数据错误，只表示没有独立第二源或口径不可比；冲突项保留两源数值。' +
      'totals 是检查次数，预热与各接口可能覆盖同一批标的，不代表去重后的标的数。' +
      '对账容差来源：' + tolText
  };
}

/** 统一入口：跑一个 scope 的对账，记账并返回 checks 给 contract.attach */
async function run(scope, payload) {
  var checks = [];
  try {
    if (scope === 'us-linkage') checks = await checkUsLinkage(payload);
    else if (scope === 'portfolio') checks = await checkPortfolio(payload);
    else if (scope === 'etf') checks = await checkEtf(payload);
    else if (scope === 'industry') checks = await checkIndustry(payload);
    else if (scope === 'factor-panel') checks = await checkFactorPanel(payload);
  } catch (e) {
    checks = [unverified(scope, 'reconcile', '对账流程', '对账执行异常：' + e.message, null)];
  }
  record(scope, makeVerification(scope, checks));
  return checks;
}

function makeVerification(scope, checks) {
  var c = CT.countChecks(checks);
  return {
    scope: scope, asOf: new Date().toISOString(),
    checked: c.checked, matched: c.matched, mismatched: c.mismatched,
    unverified: c.unverified,
    timeSkew: checks.filter(function (x) { return x.comparability === 'time-skew'; }).length,
    conflicts: c.conflicts, items: checks,
    status: (c.mismatched || checks.some(function (x) { return x.comparability === 'time-skew'; }))
      ? CT.STATUS.DEGRADED : CT.STATUS.OK
  };
}

/* ---------------- 个股分析 SSE 的可信度事件 ---------------- */

var ANALYZE_MAX_CODES = 8;

/**
 * 构造 /api/analyze 的 quality 事件。
 * SSE 不能套 JSON 信封（前端按 data: 逐条消费），所以单独发一个 type=quality 事件。
 * 检查对象就是本次分析真正依赖的个股行情；超出上限的标的如实说明「仅抽样」，
 * 不把没查的标的值算进 matched。
 */
async function analyzeQuality(codes) {
  var list = (codes || []).map(function (c) { return String(c || '').trim(); })
    .filter(function (c) { return /^\d{6}$/.test(c); });
  var sampled = list.slice(0, ANALYZE_MAX_CODES);
  var checks = [];
  if (sampled.length) {
    try { checks = await checkAQuotes(sampled); }
    catch (e) {
      checks = [unverified('a-quote', 'analyze', '个股分析行情',
        '对账执行异常：' + e.message, null)];
    }
  } else {
    checks = [unverified('a-quote', 'analyze', '个股分析行情',
      '本次没有可核对的 6 位 A 股代码', null)];
  }
  var c = CT.countChecks(checks);
  var wrapped = CT.attach({ ok: true }, {
    scope: 'a-quote', checks: checks,
    /* 一个都没对上（全是取数失败）时按「必需数据缺失」处理，不假装 ok */
    requiredMissing: (c.checked > 0 && c.matched === 0 && c.unverified === c.checked) ? 1 : 0
  });
  var analysisVerification = makeVerification('analyze', checks);
  analysisVerification.status = wrapped.status; /* 与事件状态保持一致 */
  record('analyze', analysisVerification);
  return {
    type: 'quality', scope: 'analyze', status: wrapped.status,
    checked: c.checked, matched: c.matched,
    mismatched: c.mismatched, unverified: c.unverified,
    conflicts: c.conflicts, items: checks, issues: wrapped.issues,
    sampled: sampled,
    sampleNote: list.length > sampled.length
      ? ('本次共 ' + list.length + ' 只标的，仅抽样核对前 ' + sampled.length + ' 只，其余未核对')
      : null,
    tolerance: toleranceInfo(),
    note: 'SSE 流新增的可信度事件；旧事件字段与顺序未改动，未知 type 前端会忽略'
  };
}

/** /api/data-quality 首次访问时做一次真实预热，保证状态条显示的是真跑出来的数 */
async function warmup() {
  var checks = [];
  var jobs = [
    checkAQuotes(['600519', '000001', '600036']),
    S.fetchUsMarket().then(function (us) {
      return compareUsItems(us.indices.concat(us.leaders || [], us.adr || []), 'a-quote-us');
    }),
    checkLocalKline(['600519', '000001'])
  ];
  var results = await Promise.all(jobs.map(function (p) {
    return p.catch(function (e) {
      return [unverified('warmup', 'warmup', '预热对账', e.message, null)];
    });
  }));
  results.forEach(function (r) { checks = checks.concat(r || []); });
  record('warmup', makeVerification('warmup', checks));

  /* 因子面板单独成一个 scope（不进上面的 warmup 总账，避免同一个检查被数两遍）。
     它只读两个本地 JSON，不联网，所以放在预热里跑成本为零 —— 而收益很实在：
     缓存落后于库 / 缓存缺因子这两件事，用户在覆盖清单里一眼就能看到。 */
  var panelChecks;
  try {
    panelChecks = await checkFactorPanel();
  } catch (e) {
    panelChecks = [unverified('factor-panel', 'cache', '面板缓存对账',
      '对账执行异常：' + e.message, null, 'freshness')];
  }
  record('factor-panel', makeVerification('factor-panel', panelChecks));
  return makeVerification('warmup', checks);
}

module.exports = {
  TOLERANCE: TOLERANCE,
  FALLBACK: FALLBACK,
  CATALOG: CATALOG,
  compare: compare,
  priceRule: priceRule,
  pctRule: pctRule,
  detectTimeSkew: detectTimeSkew,
  usTradingNow: usTradingNow,
  intradaySkew: intradaySkew,
  unverified: unverified,
  run: run,
  warmup: warmup,
  record: record,
  snapshot: snapshot,
  loadTolerance: loadTolerance,
  refreshTolerance: refreshTolerance,
  sessionKey: sessionKey,
  toleranceInfo: toleranceInfo,
  analyzeQuality: analyzeQuality,
  ANALYZE_MAX_CODES: ANALYZE_MAX_CODES,
  checkAQuotes: checkAQuotes,
  checkLocalKline: checkLocalKline,
  checkFactorPanel: checkFactorPanel,
  dayIndex: dayIndex,
  checkUsLinkage: checkUsLinkage,
  checkPortfolio: checkPortfolio,
  checkEtf: checkEtf,
  checkIndustry: checkIndustry,
  tencentAMap: tencentAMap,
  sinaAMap: sinaAMap,
  tencentUsMap: tencentUsMap
};
