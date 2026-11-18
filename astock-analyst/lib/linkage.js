/**
 * linkage.js — 美股联动（市场级）判定
 *
 * 为什么不直接用 engine.js 的 computeUsChinaLink：
 *   那个函数是为「单只 A 股」服务的，需要完整的个股 pack。这里要的是**市场级**结论，
 *   所以用指数与科技类 ETF 作为代理，独立成一件事，也更快（一次批量行情就够）。
 *
 * 数据口径：
 *   美股  —— 新浪全球行情（道指/纳指/标普 + 科技龙头 + 中概 ADR），真实收盘数据
 *   A 股  —— 腾讯行情批量接口（三大指数 + 科创50 + 半导体/科技/科创50/中概互联 ETF）
 * 判定逻辑是「隔夜传导」：用美股上一交易日收盘 vs A 股今日表现。
 */
var S = require('./sources.js');

/* A 股侧代理标的：指数看方向，ETF 看板块资金态度 */
var A_SYMBOLS = [
  { sym: 'sh000001', code: '000001', name: '上证指数', kind: 'index' },
  { sym: 'sz399001', code: '399001', name: '深证成指', kind: 'index' },
  { sym: 'sz399006', code: '399006', name: '创业板指', kind: 'index' },
  { sym: 'sh000688', code: '000688', name: '科创50', kind: 'index' },
  { sym: 'sh512480', code: '512480', name: '半导体ETF', kind: 'tech' },
  { sym: 'sh515000', code: '515000', name: '科技ETF', kind: 'tech' },
  { sym: 'sh588000', code: '588000', name: '科创50ETF', kind: 'tech' },
  { sym: 'sh513050', code: '513050', name: '中概互联ETF', kind: 'china_adr' }
];

const TENCENT = 'https://qt.gtimg.cn/q=';

function num(v) {
  var n = parseFloat(v);
  return isFinite(n) ? n : null;
}

function round(v, d) {
  if (v == null) return null;
  var p = Math.pow(10, d == null ? 2 : d);
  return Math.round(v * p) / p;
}

/** 批量取 A 股侧行情（一次 HTTP） */
async function fetchABatch() {
  var out = { list: [], errors: [], asOf: null, source: '腾讯行情 qt.gtimg.cn' };
  try {
    var r = await S.rawGbk ? await S.rawGbk(TENCENT + A_SYMBOLS.map(function (x) { return x.sym; }).join(','), { timeoutMs: 10000 })
      : null;
    if (!r || !r.ok) {
      /* 兜底：走 Node 原生 fetch + TextDecoder */
      var resp = await fetch(TENCENT + A_SYMBOLS.map(function (x) { return x.sym; }).join(','));
      var buf = Buffer.from(await resp.arrayBuffer());
      r = { ok: resp.ok, text: new TextDecoder('gbk').decode(buf) };
    }
    var byCode = {};
    (r.text || '').split('\n').forEach(function (line) {
      var m = line.match(/v_([a-z0-9]+)="([^"]*)"/i);
      if (!m) return;
      var b = m[2].split('~');
      if (b.length < 40) return;
      byCode[m[1]] = {
        name: b[1],
        price: num(b[3]),
        prevClose: num(b[4]),
        changePct: num(b[32]),
        amount: num(b[37]) == null ? null : num(b[37]) * 10000,
        asOf: b[30] || null
      };
    });
    A_SYMBOLS.forEach(function (x) {
      var q = byCode[x.sym];
      out.list.push({
        code: x.code, name: x.name, kind: x.kind,
        price: q ? q.price : null,
        changePct: q ? round(q.changePct, 2) : null,
        amount: q ? q.amount : null,
        ok: !!(q && q.price > 0)
      });
      if (q && q.asOf && !out.asOf) out.asOf = q.asOf;
    });
  } catch (e) {
    out.errors.push('A 股批量行情失败：' + e.message);
  }
  return out;
}

function avg(list) {
  var v = list.filter(function (x) { return x != null; });
  if (!v.length) return null;
  return v.reduce(function (a, b) { return a + b; }, 0) / v.length;
}

/** 生成一条配对对比（美股指标 vs A股指标） */
function pair(name, usVal, aVal, usName, aName) {
  var relation = null;
  if (usVal != null && aVal != null) {
    var sameDir = (usVal >= 0 && aVal >= 0) || (usVal < 0 && aVal < 0);
    var gap = Math.abs(usVal - aVal);
    relation = sameDir ? (gap >= 3 ? '联动（幅度背离）' : '联动') : '背离';
  }
  return {
    name: name, usName: usName, aName: aName,
    usPct: round(usVal, 2), aPct: round(aVal, 2), relation: relation,
    gap: (usVal != null && aVal != null) ? round(Math.abs(usVal - aVal), 2) : null
  };
}

/** 结论文案：说清方向和次日该盯什么，不给模棱两可的话 */
function buildVerdict(pairs, usTech, aTech) {
  var valid = pairs.filter(function (p) { return p.relation; });
  if (!valid.length) {
    return {
      type: '数据不足', confidence: '低',
      headline: '当前无法判定联动关系（美股或 A 股数据缺失）',
      detail: '需要美股收盘数据与 A 股当日行情同时可用。',
      nextDayHint: '补齐数据后再看，不要凭单边信息做判断。'
    };
  }
  var lian = valid.filter(function (p) { return /^联动/.test(p.relation); }).length;
  var bei = valid.filter(function (p) { return p.relation === '背离'; }).length;
  var ratio = lian / valid.length;

  var type, conf, headline, detail, hint;
  if (ratio >= 0.67) {
    type = '联动'; conf = ratio === 1 ? '高' : '中';
    headline = '中美市场同向，外部传导占主导';
    detail = valid.map(function (p) { return p.name + '：美股' + fmt(p.usPct) + ' / A股' + fmt(p.aPct); }).join('；');
    if (usTech != null && aTech != null && usTech < 0 && aTech < 0) {
      hint = '两边科技同步走弱，次日优先看防御：降低高 beta 仓位，别急着抄底科技。';
    } else if (usTech != null && aTech != null && usTech > 0 && aTech > 0) {
      hint = '两边科技同步走强，注意高开后的兑现压力，追高需等回踩确认。';
    } else {
      hint = '方向一致，按既有趋势处理，重点看成交量能否延续。';
    }
  } else if (ratio <= 0.33) {
    type = '背离'; conf = ratio === 0 ? '高' : '中';
    headline = '中美市场方向相反，本土因素主导定价';
    detail = valid.map(function (p) { return p.name + '：美股' + fmt(p.usPct) + ' / A股' + fmt(p.aPct); }).join('；');
    if (usTech != null && aTech != null && usTech > 0 && aTech < 0) {
      hint = '美股涨、A股科技跌：内因为主（政策/资金/估值），次日看能否止跌，不要简单类比美股做多。';
    } else if (usTech != null && aTech != null && usTech < 0 && aTech > 0) {
      hint = '美股跌、A股科技涨：本土资金在承接，次日关注能否放量延续，警惕补跌风险。';
    } else {
      hint = '两边脱钩，A 股按自身节奏走，减少用隔夜美股做预判。';
    }
  } else {
    type = '分化'; conf = '低';
    headline = '中美市场部分联动、部分背离，方向不统一';
    detail = valid.map(function (p) { return p.name + '：' + p.relation; }).join('；');
    hint = '市场分歧较大，适合降低仓位等方向明确，不适合重仓押注单边。';
  }
  return { type: type, confidence: conf, headline: headline, detail: detail, nextDayHint: hint };
}

function fmt(v) {
  if (v == null) return '—';
  return (v > 0 ? '+' : '') + Number(v).toFixed(2) + '%';
}

/** 主入口 */
async function build() {
  var out = { ok: false, asOf: new Date().toISOString(), errors: [] };

  /* 1) 美股（新浪，失败退腾讯） */
  var us = null;
  try {
    us = await S.fetchUsMarket();
    if (!us || ((!us.indices || !us.indices.length) && typeof S.fetchUsMarketTencent === 'function')) {
      var t = await S.fetchUsMarketTencent();
      if (t && t.indices && t.indices.length) us = t;
    }
  } catch (e) {
    out.errors.push('美股数据失败：' + e.message);
    try { us = await S.fetchUsMarketTencent(); } catch (e2) { }
  }
  us = us || { indices: [], leaders: [], adr: [], errors: ['美股数据源不可用'] };
  out.us = {
    source: us.source || '未知',
    indices: (us.indices || []).map(function (x) {
      return { name: x.name, symbol: x.symbol, price: x.price, prevClose: x.prevClose == null ? null : round(x.prevClose, 2), changePct: round(x.changePct, 2), usTime: x.usTime || null };
    }),
    tech: (us.leaders || []).map(function (x) {
      return { name: x.name, symbol: x.symbol, price: x.price, prevClose: x.prevClose == null ? null : round(x.prevClose, 2), changePct: round(x.changePct, 2), usTime: x.usTime || null };
    }),
    adr: (us.adr || []).map(function (x) {
      return { name: x.name, symbol: x.symbol, price: x.price, prevClose: x.prevClose == null ? null : round(x.prevClose, 2), changePct: round(x.changePct, 2), usTime: x.usTime || null };
    }),
    errors: us.errors || []
  };

  /* 数据自检：涨跌幅算不出来就明说，别用 0 顶上 */
  var badUs = out.us.indices.concat(out.us.tech, out.us.adr).filter(function (x) { return x.changePct == null; });
  if (badUs.length) out.errors.push('美股涨跌幅缺失：' + badUs.map(function (x) { return x.name; }).join('、'));
  if (out.us.indices.length < 3) out.errors.push('美股指数仅返回 ' + out.us.indices.length + ' 个（应为 3 个）');

  /* 2) A 股（腾讯批量） */
  var a = await fetchABatch();
  out.a = {
    source: a.source,
    asOf: a.asOf,
    indices: a.list.filter(function (x) { return x.kind === 'index'; }),
    tech: a.list.filter(function (x) { return x.kind === 'tech'; }),
    adr: a.list.filter(function (x) { return x.kind === 'china_adr'; }),
    errors: a.errors
  };

  /* 3) 计算配对与均值 */
  var usIdx = out.us.indices;
  var usTech = avg(out.us.tech.map(function (x) { return x.changePct; }));
  var aTech = avg(out.a.tech.map(function (x) { return x.changePct; }));
  var usAdr = avg(out.us.adr.map(function (x) { return x.changePct; }));

  function pick(list, re) {
    var hit = list.filter(function (x) { return re.test(x.name); })[0];
    return hit ? hit.changePct : null;
  }
  var usNas = pick(usIdx, /纳斯达克|纳指/);
  var usSpx = pick(usIdx, /标普/);
  var aSh = pick(out.a.indices, /上证/);
  var aCy = pick(out.a.indices, /创业板/);
  var aKc = pick(out.a.indices, /科创/);
  var aCx = pick(out.a.tech, /科创50ETF/);
  var aCxetf = pick(out.a.adr, /中概/) || pick(a.list, /中概/);

  out.metrics = {
    usTechAvg: round(usTech, 2),
    aTechAvg: round(aTech, 2),
    usAdrAvg: round(usAdr, 2),
    aAdrProxy: aCxetf
  };
  out.pairs = [
    pair('科技股', usTech, aTech, '美股科技龙头均值', 'A股科技ETF均值'),
    pair('大盘', usSpx, aSh, '标普500', '上证指数'),
    pair('成长风格', usNas, (aCy != null ? aCy : aKc), '纳斯达克', '创业板指/科创50'),
    pair('中概情绪', usAdr, aCxetf, '中概股ADR均值', '中概互联ETF')
  ];
  out.verdict = buildVerdict(out.pairs, usTech, aTech);

  /* 新交易日盘前：主源把现价置为昨收、涨跌幅归 0；第二源可能仍是上一交易日收盘，
     也可能已同步归 0（随切换进度变化）。无论哪种，用「美股 0%」算联动都会失真。
     不伪造涨跌幅、不改算法，只显式标注结论不可用；跨源时点差异由对账层单独标注。 */
  var usAll = out.us.indices.concat(out.us.tech, out.us.adr);
  /* 只用「涨跌幅归零」判定，不要求现价=昨收：盘前部分标的已有报价，
     硬加这个条件会让判据贴着阈值、被单个标的的价格波动翻掉。 */
  var flatUs = usAll.filter(function (x) { return x.changePct === 0; }).length;
  if (usAll.length >= 5 && flatUs / usAll.length >= 0.8) {
    out.us.marketState = 'pre-market';
    out.verdict.dataCaveat = '美股处于新交易日盘前（尚未开盘或刚切换交易日）：' +
      '涨跌幅为 0，与 A 股当日表现不可比，本次联动结论与配对方向暂不可用；' +
      '若两家数据源报价时点不同，可信度状态条会另行标注';
  } else {
    out.us.marketState = 'regular';
  }

  out.ok = (out.us.indices.length > 0) && (out.a.indices.length > 0);
  if (!out.ok) out.errors = out.errors.concat(out.us.errors || []);
  out.us.errors = undefined;
  return out;
}

module.exports = { build: build, A_SYMBOLS: A_SYMBOLS };
