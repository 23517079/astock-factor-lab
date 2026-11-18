/**
 * portfolio.js — 个人持仓分析
 *
 * 职责：
 *   1) 持仓存取（data/portfolio.json，多条记录，同一代码可多笔买入后按均价合并）
 *   2) 拉取实时行情，核算市值 / 浮动盈亏 / 当日盈亏 / 权重
 *   3) 给出集中度与风险提示（这些是判断"能不能拿"的关键，不只是算个总账）
 *
 * 设计原则：行情一律走 sources.js 的真实数据源（东财→新浪→腾讯），失败时明确标记
 * 为"行情缺失"而不是静默填 0，避免用假数字骗自己。
 */
var fs = require('fs');
var path = require('path');
var S = require('./sources.js');

var ROOT = path.join(__dirname, '..');
var DATA_DIR = process.env.ASTOCK_DATA_DIR || path.join(ROOT, 'data');
var FILE = path.join(DATA_DIR, 'portfolio.json');

function ensureDir() {
  if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
}

function load() {
  ensureDir();
  if (!fs.existsSync(FILE)) return { positions: [], updatedAt: null };
  try {
    var d = JSON.parse(fs.readFileSync(FILE, 'utf8'));
    if (!Array.isArray(d.positions)) d.positions = [];
    return d;
  } catch (e) {
    return { positions: [], updatedAt: null, error: '持仓文件解析失败：' + e.message };
  }
}

function save(positions) {
  ensureDir();
  var data = { positions: positions || [], updatedAt: new Date().toISOString() };
  fs.writeFileSync(FILE, JSON.stringify(data, null, 2), 'utf8');
  return data;
}

/** 清洗并合并同一代码的多笔记录（按股数加权算均价） */
function normalize(list) {
  var byCode = {};
  var order = [];
  (list || []).forEach(function (p) {
    var code = String(p.code || '').trim();
    if (!/^\d{6}$/.test(code)) return;
    var shares = Number(p.shares);
    var cost = Number(p.avgCost);
    if (!(shares > 0)) return;
    if (!(cost >= 0)) return;
    if (!byCode[code]) {
      byCode[code] = {
        code: code,
        name: String(p.name || '').trim(),
        shares: 0,
        costAmount: 0,
        buyDate: p.buyDate || '',
        note: String(p.note || '').slice(0, 120)
      };
      order.push(code);
    }
    var cur = byCode[code];
    cur.shares += shares;
    cur.costAmount += shares * cost;
    if (p.buyDate && (!cur.buyDate || p.buyDate < cur.buyDate)) cur.buyDate = p.buyDate;
    if (p.note) cur.note = String(p.note).slice(0, 120);
    if (p.name && !cur.name) cur.name = String(p.name).trim();
  });
  return order.map(function (c) {
    var x = byCode[c];
    return {
      code: x.code,
      name: x.name,
      shares: x.shares,
      avgCost: x.shares ? x.costAmount / x.shares : 0,
      buyDate: x.buyDate,
      note: x.note
    };
  });
}

function round(v, d) {
  var p = Math.pow(10, d == null ? 2 : d);
  return Math.round((Number(v) || 0) * p) / p;
}

/** 并发拉行情，限制同时请求数，避免把数据源打爆 */
async function fetchQuotes(codes, limit) {
  var max = limit || 5;
  var out = {};
  var queue = codes.slice();
  async function worker() {
    while (queue.length) {
      var code = queue.shift();
      try {
        var q = await S.fetchQuoteA(code);
        out[code] = q && q.ok ? q.quote : null;
      } catch (e) {
        out[code] = null;
      }
    }
  }
  var ws = [];
  for (var i = 0; i < Math.min(max, codes.length); i++) ws.push(worker());
  await Promise.all(ws);
  return out;
}

/**
 * 计算持仓看板。
 * @param {Array} rawList 原始持仓（可多笔）
 * @param {Object} opts { signalMap: {code: strategyName} } 可选，用于叠加策略信号
 */
async function analyze(rawList, opts) {
  opts = opts || {};
  var positions = normalize(rawList);
  var quotes = await fetchQuotes(positions.map(function (p) { return p.code; }), 5);

  var rows = positions.map(function (p) {
    var q = quotes[p.code];
    var hasQuote = !!(q && q.price > 0);
    var price = hasQuote ? q.price : null;
    var cost = p.shares * p.avgCost;
    var mv = hasQuote ? p.shares * price : null;
    var pnl = hasQuote ? mv - cost : null;
    var pnlPct = hasQuote && cost > 0 ? (pnl / cost) * 100 : null;
    var dayPnl = hasQuote && q.prevClose > 0 ? (price - q.prevClose) * p.shares : null;
    return {
      code: p.code,
      name: p.name || (q && q.name) || '',
      shares: p.shares,
      avgCost: round(p.avgCost, 3),
      cost: round(cost, 2),
      price: hasQuote ? round(price, 3) : null,
      prevClose: hasQuote ? round(q.prevClose, 3) : null,
      changePct: hasQuote ? round(q.changePct, 2) : null,
      marketValue: hasQuote ? round(mv, 2) : null,
      pnl: hasQuote ? round(pnl, 2) : null,
      pnlPct: pnlPct == null ? null : round(pnlPct, 2),
      dayPnl: dayPnl == null ? null : round(dayPnl, 2),
      weight: null,
      quoteOk: hasQuote,
      quoteSource: hasQuote ? (q.source || null) : null,
      quoteError: hasQuote ? null : '行情获取失败（数据源不可用或代码无效）',
      signal: (opts.signalMap && opts.signalMap[p.code]) || null
    };
  });

  var totalCost = 0, totalValue = 0, dayPnlSum = 0, missing = 0;
  rows.forEach(function (r) {
    totalCost += r.cost;
    if (r.marketValue != null) totalValue += r.marketValue; else missing += 1;
    if (r.dayPnl != null) dayPnlSum += r.dayPnl;
  });

  rows.forEach(function (r) {
    r.weight = totalValue > 0 && r.marketValue != null ? round((r.marketValue / totalValue) * 100, 2) : null;
  });
  rows.sort(function (a, b) { return (b.marketValue || 0) - (a.marketValue || 0); });

  var totalPnl = totalValue - totalCost;
  var flags = [];
  var withQuote = rows.filter(function (r) { return r.marketValue != null; });

  if (rows.length === 0) {
    flags.push({ level: 'info', text: '还没有持仓记录。录入代码、股数和成本价后，这里会自动核算盈亏与风险。' });
  }
  if (rows.length > 0 && withQuote.length < rows.length) {
    flags.push({ level: 'warn', text: (rows.length - withQuote.length) + ' 只标的行情获取失败，其市值未计入合计（不是 0，是缺失）。' });
  }
  var top = withQuote[0];
  if (top && top.weight != null && top.weight >= 30) {
    flags.push({ level: 'bad', text: '集中度过高：' + top.name + '(' + top.code + ') 占 ' + top.weight + '%，单一标的波动会主导整个账户。' });
  } else if (top && top.weight != null && top.weight >= 20) {
    flags.push({ level: 'warn', text: '集中度偏高：' + top.name + '(' + top.code + ') 占 ' + top.weight + '%，建议留意个股风险。' });
  }
  if (withQuote.length === 1 && rows.length === 1) {
    flags.push({ level: 'warn', text: '只有 1 只持仓，账户波动完全取决于单一标的，风险敞口很大。' });
  }
  withQuote.forEach(function (r) {
    if (r.pnlPct != null && r.pnlPct <= -15) {
      flags.push({ level: 'bad', text: r.name + '(' + r.code + ') 浮亏 ' + r.pnlPct + '%，已超过 15%，请确认持仓逻辑是否还成立。' });
    } else if (r.pnlPct != null && r.pnlPct <= -8) {
      flags.push({ level: 'warn', text: r.name + '(' + r.code + ') 浮亏 ' + r.pnlPct + '%，注意止损纪律。' });
    }
    if (r.pnlPct != null && r.pnlPct >= 40 && r.weight != null && r.weight >= 20) {
      flags.push({ level: 'info', text: r.name + '(' + r.code + ') 浮盈 ' + r.pnlPct + '% 且占比 ' + r.weight + '%，可考虑分批止盈。' });
    }
  });

  return {
    ok: true,
    asOf: new Date().toISOString(),
    positions: rows,
    summary: {
      count: rows.length,
      quotedCount: withQuote.length,
      totalCost: round(totalCost, 2),
      totalValue: round(totalValue, 2),
      totalPnl: round(totalPnl, 2),
      totalPnlPct: totalCost > 0 ? round((totalPnl / totalCost) * 100, 2) : null,
      dayPnl: round(dayPnlSum, 2),
      missingQuotes: missing,
      topWeight: top && top.weight != null ? { code: top.code, name: top.name, weight: top.weight } : null
    },
    flags: flags
  };
}

module.exports = { load: load, save: save, normalize: normalize, analyze: analyze, FILE: FILE };
