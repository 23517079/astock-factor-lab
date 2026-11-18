'use strict';
/*
 * sources.js — 五维因子数据采集（A 股 + 美股 + 宏观）
 *
 * 原则：
 *  - 每项数据都带 source / asOf，界面与报告里逐项可追溯。
 *  - 取不到就返回 null，并在 errors 里说明原因；绝不用估算值或旧值顶替。
 *  - 每个接口有主源与备源，自动降级并标注实际用了哪个。
 */

var { execFile } = require('child_process');
var { URL } = require('url');

/* ---------------- 底层请求 ---------------- */

function raw(url, opts) {
  opts = opts || {};
  var timeout = Math.ceil((opts.timeoutMs || 15000) / 1000) + 2;
  var args = ['-sS', '--compressed', '-m', String(timeout)];
  /* 本机 / 内网地址绕过系统代理 */
  if (/^https?:\/\/(127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\])(:|\/|$)/i.test(url)) args.push('--noproxy', '*');
  if (opts.referer) args.push('-H', 'Referer: ' + opts.referer);
  if (opts.ua !== false) args.push('-A', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)');
  args.push('-w', '\n__HTTP__%{http_code}');
  args.push(url);

  return new Promise(function (resolve) {
    /* 保留原始字节：GBK 源需要按 GBK 解，中文 UTF-8 源需要按 UTF-8 解 */
    execFile('curl', args, { maxBuffer: 24 * 1024 * 1024, timeout: (opts.timeoutMs || 15000) + 4000, encoding: 'buffer' },
      function (err, stdout, stderr) {
        var outBuf = Buffer.isBuffer(stdout) ? stdout : Buffer.from(String(stdout || ''), 'binary');
        var errBuf = Buffer.isBuffer(stderr) ? stderr : Buffer.from(String(stderr || ''), 'binary');
        if (err && !outBuf.length) {
          resolve({ ok: false, status: 0, text: '', error: String(errBuf.toString('utf8') || err.message).trim().slice(0, 200) });
          return;
        }
        var outStr = outBuf.toString('latin1');
        var m = outStr.match(/\n__HTTP__(\d{3})\s*$/);
        var status = m ? parseInt(m[1], 10) : 200;
        var bodyBuf = m ? outBuf.slice(0, m.index) : outBuf;
        /* 默认按 UTF-8 解；GBK 源显式声明 encoding:'gbk' */
        var text = opts.encoding === 'gbk' ? decodeGbk(bodyBuf.toString('latin1')) : bodyBuf.toString('utf8');
        resolve({ ok: status >= 200 && status < 300, status: status, text: text, error: '' });
      });
  });
}

function rawGbk(url, opts) {
  opts = Object.assign({}, opts || {}, { encoding: 'gbk' });
  return raw(url, opts);
}

function decodeGbk(buf) {
  try { return new TextDecoder('gbk').decode(Buffer.from(buf, 'binary')); }
  catch (e) { return buf; }
}

function jsonp(text, cbName) {
  var re = new RegExp('^[^(]*\\(' + (cbName || '[a-zA-Z0-9_]+') + '\\s*\\(');
  var s = String(text || '').trim();
  var m = s.match(re);
  if (m) {
    var start = s.indexOf('(', m[0].length - 1);
    var end = s.lastIndexOf(')');
    if (start >= 0 && end > start) s = s.slice(start + 1, end);
  }
  s = s.replace(/^\s*cb\s*\(/, '').replace(/\)\s*;?\s*$/, '');
  try { return { ok: true, value: JSON.parse(s) }; }
  catch (e) { return { ok: false, error: 'JSONP 解析失败', value: null }; }
}

function isNum(v) { return v !== null && v !== undefined && v !== '' && isFinite(Number(v)); }
function n(v) { var x = parseFloat(v); return isFinite(x) ? x : null; }
function r2(v, d) { var p = Math.pow(10, d === undefined ? 2 : d); return Math.round((Number(v) || 0) * p) / p; }
function nowBj() {
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'medium' }).format(new Date());
}

/* 统一的「取数结果」封装 */
function box(name, ok, value, source, asOf, extra) {
  var o = { item: name, ok: !!ok, value: ok ? value : null, source: source || null, asOf: asOf || null };
  if (extra) Object.keys(extra).forEach(function (k) { o[k] = extra[k]; });
  return o;
}

/* ---------------- A 股：行情 ---------------- */

var A_QUOTE_FIELDS = 'f2,f3,f4,f5,f6,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,f20,f21,f22,f23,f11,f62,f184';

function secidOf(code) {
  var c = String(code);
  if (/^(60|68|11|5|6)/.test(c)) return '1.' + c;
  if (/^(00|30|12|15|16|18)/.test(c)) return '0.' + c;
  if (/^(8|4|92)/.test(c)) return '0.' + c;
  return '1.' + c;
}

function parseEmQuote(x) {
  if (!x) return null;
  return {
    code: String(x.f12 || ''), name: String(x.f14 || ''),
    price: n(x.f2), changePct: r2(n(x.f3), 2), changeAmt: n(x.f4),
    volume: n(x.f5), amount: n(x.f6), turnoverPct: n(x.f8),
    pe: n(x.f9), volumeRatio: n(x.f10),
    high: n(x.f15), low: n(x.f16), open: n(x.f17), prevClose: n(x.f18),
    marketCap: n(x.f20), floatCap: n(x.f21), pb: n(x.f23),
    bid1: n(x.f11), mainInflow: n(x.f62), mainInflowPct: n(x.f184),
    secid: String(x.f13 || '')
  };
}

/* 单只 A 股实时行情：东财主源 → 新浪备源 → 腾讯备源 */
/**
 * 个股查不到时，反向识别「这个代码到底是什么」。
 * 上交所 000xxx 段全部是指数（000001 上证指数 / 000300 沪深300 / 000111 380信息…），
 * 而深市 000xxx 是个股（000001 平安银行 / 000011 深物业A），所以 000xxx 天然有歧义。
 * 返回 { market, name, code, price, changePct } 或 null。
 */
async function probeNonStock(code) {
  var c = String(code);
  if (!/^\d{6}$/.test(c)) return null;
  var pref = c.charAt(0) === '6' ? 'sh' : 'sz';
  var alt = pref === 'sh' ? 'sz' : 'sh';
  var cands = [pref + c, alt + c];
  var seen = {};
  for (var i = 0; i < cands.length; i++) {
    var sym = cands[i];
    if (seen[sym]) continue;
    seen[sym] = 1;
    var r = await rawGbk('https://qt.gtimg.cn/q=' + sym, { timeoutMs: 8000 });
    if (!r.ok) continue;
    var m = r.text.match(/v_[a-z0-9]+="([^"]*)"/i);
    if (!m || !m[1]) continue;
    var b = m[1].split('~');
    if (b.length < 40 || !isNum(parseFloat(b[3]))) continue;
    var name = String(b[1] || '');
    var isSh = sym.slice(0, 2) === 'sh';
    return {
      market: isSh ? '上交所' : '深交所',
      symbol: sym, code: c, name: name,
      price: parseFloat(b[3]), changePct: r2(parseFloat(b[32]), 2),
      /* 沪市 000/880/999 段、深市 399 段、北交所 899 段均为指数 */
      isIndex: (isSh && /^(000|880|999|888)/.test(c)) || (!isSh && /^(399|899)/.test(c))
    };
  }
  return null;
}

/** 组装「这不是个股」的标准失败结果（含识别到的名称） */
async function notStockResult(code) {
  var ns = await probeNonStock(code);
  var msg;
  if (ns && ns.name) {
    var tag = ns.isIndex ? '指数' : '品种';
    msg = code + ' 不是 A 股个股。它在' + ns.market + '是' + tag + '「' + ns.name + '」' +
      '（现 ' + ns.price + '，' + (ns.changePct > 0 ? '+' : '') + ns.changePct + '%），' +
      '无法做个股五维分析。请确认是否输错了代码。';
  } else {
    msg = '未找到代码 ' + code + ' 的 A 股个股行情，请确认代码是否正确（该代码可能是指数、基金、已退市，或并非个股）。';
  }
  return { ok: false, quote: null, source: null, errors: [], message: msg, notStock: ns };
}

async function fetchQuoteA(code) {
  /* 明确属于「指数段」的代码直接拦截，避免把指数当个股分析 */
  if (/^(399|899|880|999|888)/.test(String(code))) return await notStockResult(code);
  var errors = [];
  var sawEmpty = false;
  /* 主源：东财 */
  var emUrl = 'https://push2.eastmoney.com/api/qt/stock/get?fltt=2&invt=2&secid=' +
    encodeURIComponent(secidOf(code)) + '&fields=' + A_QUOTE_FIELDS;
  var em = await raw(emUrl, { timeoutMs: 12000 });
  if (em.ok) {
    try {
      var j = JSON.parse(em.text);
      if (j && j.data) {
        var q = parseEmQuote(j.data);
        if (q && isNum(q.price)) {
          q.asOf = nowBj(); q.source = '东方财富行情';
          q.sourceUrl = 'https://quote.eastmoney.com/' + code + '.html';
          q.unitNote = '主力净流入单位元';
          return { ok: true, quote: q, source: '东方财富行情', errors: errors };
        }
      }
    } catch (e) { errors.push('东财解析失败'); }
  } else errors.push('东财行情：' + (em.error || ('HTTP ' + em.status)));

  /* 备源：新浪 */
  var sinaSym = (code[0] === '6' ? 'sh' : 'sz') + code;
  var s = await rawGbk('https://hq.sinajs.cn/list=' + sinaSym, { referer: 'https://finance.sina.com.cn', timeoutMs: 10000 });
  if (s.ok) {
    var m = s.text.match(/="([^"]*)"/);
    var a = m ? m[1].split(',') : [];
    if (a.length > 30 && isNum(parseFloat(a[3]))) {
      var prev = parseFloat(a[2]), now = parseFloat(a[3]);
      return {
        ok: true, source: '新浪行情', errors: errors,
        quote: {
          code: code, name: a[0], price: now, prevClose: prev,
          open: parseFloat(a[1]), high: parseFloat(a[4]), low: parseFloat(a[5]),
          volume: parseFloat(a[8]), amount: parseFloat(a[9]),
          changePct: prev ? r2((now - prev) / prev * 100, 2) : null,
          changeAmt: prev ? r2(now - prev, 3) : null,
          asOf: a[30] + ' ' + a[31], source: '新浪行情 hq.sinajs.cn',
          sourceUrl: 'https://finance.sina.com.cn/realstock/company/' + sinaSym + '/nc.shtml',
          unitNote: '新浪不提供 PE/量比/主力净流入，相关因子标为无数据'
        }
      };
    }
    /* 返回 200 但无有效行情（多为代码不存在） */
    sawEmpty = true;
  } else errors.push('新浪行情：' + (s.error || ('HTTP ' + s.status)));

  /* 备源：腾讯 */
  var tSym = (code[0] === '6' ? 'sh' : 'sz') + code;
  var t = await rawGbk('https://qt.gtimg.cn/q=' + tSym, { timeoutMs: 10000 });
  if (t.ok) {
    var tm = t.text.match(/="([^"]*)"/);
    var b = tm ? tm[1].split('~') : [];
    if (b.length > 40 && isNum(parseFloat(b[3]))) {
      return {
        ok: true, source: '腾讯行情', errors: errors,
        quote: {
          code: code, name: b[1], price: parseFloat(b[3]), prevClose: parseFloat(b[4]),
          open: parseFloat(b[5]), volume: parseFloat(b[6]) * 100, amount: parseFloat(b[37]) * 10000,
          changePct: r2(parseFloat(b[32]), 2), changeAmt: parseFloat(b[31]),
          high: parseFloat(b[33]), low: parseFloat(b[34]), turnoverPct: parseFloat(b[38]),
          pe: parseFloat(b[39]), pb: parseFloat(b[46]), marketCap: parseFloat(b[45]),
          asOf: b[30], source: '腾讯行情 qt.gtimg.cn',
          sourceUrl: 'https://gu.qq.com/' + code,
          unitNote: '腾讯不提供量比与主力净流入，相关因子标为无数据'
        }
      };
    }
    sawEmpty = true;
  } else errors.push('腾讯行情：' + (t.error || ('HTTP ' + t.status)));

  /* 汇总失败原因：区分「接口不可用」与「代码查无此股」，并尽量识别它到底是什么 */
  if (sawEmpty) return await notStockResult(code);
  return { ok: false, quote: null, source: null, errors: errors, message: '行情获取失败：' + (errors.join('；') || '所有数据源均不可用'), notStock: null };
}

/* ---------------- A 股：日 K ---------------- */

/* 东财 push2his 经常限流，备源顺序：新浪日K → 东财 → 腾讯 */
async function fetchKlineA(code, limit) {
  var lim = limit || 120;
  var errors = [];

  /* 备源 1：新浪日K（稳定） */
  var sym = (code[0] === '6' ? 'sh' : 'sz') + code;
  var s = await raw('https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol=' +
    sym + '&scale=240&ma=no&datalen=' + lim, { timeoutMs: 15000 });
  if (s.ok) {
    try {
      var arr = JSON.parse(s.text);
      if (Array.isArray(arr) && arr.length) {
        var rows = arr.map(function (p) {
          var close = parseFloat(p.close), prevClose = null;
          return { date: p.day, open: parseFloat(p.open), close: close,
            high: parseFloat(p.high), low: parseFloat(p.low), volume: parseFloat(p.volume),
            amount: null, changePct: null, changeAmt: null, turnover: null, _prev: prevClose };
        });
        /* 由 close 自行推算涨跌幅与成交额 */
        for (var i = 0; i < rows.length; i++) {
          if (i > 0) {
            var pc = rows[i - 1].close;
            rows[i].changePct = pc ? r2((rows[i].close - pc) / pc * 100, 2) : null;
            rows[i].changeAmt = pc ? r2(rows[i].close - pc, 3) : null;
            rows[i].amount = rows[i].close * rows[i].volume;
          }
        }
        return { ok: true, rows: rows, source: '新浪日K（不复权）', asOf: rows[rows.length - 1].date };
      }
    } catch (e) { errors.push('新浪日K解析失败'); }
  } else errors.push('新浪日K HTTP ' + s.status);

  /* 主源：东财 */
  var url = 'https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=' + encodeURIComponent(secidOf(code)) +
    '&klt=101&fqt=1&lmt=' + lim + '&end=20500101&fields1=f1,f2,f3' +
    '&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61';
  var r = await raw(url, { timeoutMs: 15000 });
  if (r.ok) {
    try {
      var j = JSON.parse(r.text);
      if (j && j.data && j.data.klines && j.data.klines.length) {
        var rows2 = j.data.klines.map(function (line) {
          var p = line.split(',');
          return { date: p[0], open: parseFloat(p[1]), close: parseFloat(p[2]), high: parseFloat(p[3]),
            low: parseFloat(p[4]), volume: parseFloat(p[5]), amount: parseFloat(p[6]),
            amplitude: parseFloat(p[7]), changePct: parseFloat(p[8]), changeAmt: parseFloat(p[9]), turnover: parseFloat(p[10]) };
        });
        return { ok: true, rows: rows2, source: '东方财富日K（前复权）', asOf: rows2[rows2.length - 1].date };
      }
    } catch (e) { errors.push('东财日K解析失败'); }
  } else errors.push('东财日K HTTP ' + r.status);

  /* 备源 2：腾讯 */
  var t = await rawGbk('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=' + sym + ',day,,,' + lim + ',qfq', { timeoutMs: 15000 });
  if (t.ok) {
    try {
      var j2 = JSON.parse(t.text);
      var d = j2.data && j2.data[sym];
      var a2 = d && (d.qfqday || d.day);
      if (a2 && a2.length) {
        var rows3 = a2.map(function (p) {
          return { date: p[0], open: parseFloat(p[1]), close: parseFloat(p[2]), high: parseFloat(p[3]),
            low: parseFloat(p[4]), volume: parseFloat(p[5]), amount: null, changePct: null, changeAmt: null, turnover: null };
        });
        return { ok: true, rows: rows3, source: '腾讯日K（前复权）', asOf: rows3[rows3.length - 1].date };
      }
    } catch (e) { errors.push('腾讯日K解析失败'); }
  } else errors.push('腾讯日K HTTP ' + t.status);

  return { ok: false, rows: [], source: null, asOf: null, errors: errors };
}

/* ---------------- A 股：估值分位 ---------------- */

/* 东财估值分析：PE/PB 及近 3 年分位 */
async function fetchValuation(code) {
  var url = 'https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_VALUEANALYSIS_DET&columns=ALL' +
    '&filter=' + encodeURIComponent('(SECURITY_CODE="' + code + '")') +
    '&pageNumber=1&pageSize=1&source=WEB&client=WEB';
  var r = await raw(url, { referer: 'https://data.eastmoney.com/', timeoutMs: 15000 });
  if (!r.ok) return { ok: false, value: null, source: null, errors: ['估值分位接口 HTTP ' + r.status] };
  try {
    var j = JSON.parse(r.text);
    var d = j && j.result && j.result.data && j.result.data[0];
    if (!d) return { ok: false, value: null, source: null, errors: ['估值数据为空'] };
    return {
      ok: true, source: '东方财富估值分析',
      value: {
        closePrice: n(d.CLOSE_PRICE), changeRate: n(d.CHANGE_RATE),
        peTtm: n(d.PE_TTM), peLar: n(d.PE_LAR), pbMrq: n(d.PB_MRQ), psTtm: n(d.PS_TTM),
        pcfTtm: n(d.PCF_OCF_TTM), totalMarketCap: n(d.TOTAL_MARKET_CAP),
        pePercentile: n(d.PE_TTM_PERCENTILE), pbPercentile: n(d.PB_MRQ_PERCENTILE),
        peAvg: n(d.PE_TTM_AVG), pbAvg: n(d.PB_MRQ_AVG),
        boardName: d.BOARD_NAME || null, tradeDate: d.TRADE_DATE || null
      }
    };
  } catch (e) {
    return { ok: false, value: null, source: null, errors: ['估值解析失败'] };
  }
}

/* ---------------- A 股：业绩（最近报告期） ---------------- */

async function fetchFundamentals(code) {
  var url = 'https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_LICO_FN_CPD&columns=ALL' +
    '&filter=' + encodeURIComponent('(SECURITY_CODE="' + code + '")') +
    '&pageNumber=1&pageSize=2&sortColumns=REPORTDATE&sortTypes=-1&source=WEB&client=WEB';
  var r = await raw(url, { referer: 'https://data.eastmoney.com/', timeoutMs: 15000 });
  if (!r.ok) return { ok: false, value: null, source: null, errors: ['业绩接口 HTTP ' + r.status] };
  try {
    var j = JSON.parse(r.text);
    var arr = (j && j.result && j.result.data) || [];
    if (!arr.length) return { ok: false, value: null, source: null, errors: ['业绩数据为空'] };
    var d = arr[0];
    var prev = arr[1] || null;
    return {
      ok: true, source: '东方财富业绩报表',
      value: {
        reportDate: d.REPORTDATE ? String(d.REPORTDATE).slice(0, 10) : null,
        noticeDate: d.NOTICE_DATE ? String(d.NOTICE_DATE).slice(0, 10) : null,
        revenue: n(d.TOTAL_OPERATE_INCOME), revenueYoy: n(d.YSTZ),
        netProfit: n(d.PARENT_NETPROFIT), netProfitYoy: n(d.SJLTZ),
        eps: n(d.BASIC_EPS), bps: n(d.BPS), roe: n(d.WEIGHTAVG_ROE),
        grossMargin: n(d.XSMLL), cashFlowPerShare: n(d.MGJYXJJE),
        prevReportDate: prev ? String(prev.REPORTDATE || '').slice(0, 10) : null,
        prevRevenueYoy: prev ? n(prev.YSTZ) : null,
        prevNetProfitYoy: prev ? n(prev.SJLTZ) : null
      }
    };
  } catch (e) {
    return { ok: false, value: null, source: null, errors: ['业绩解析失败'] };
  }
}

/* ---------------- A 股：融资融券 ---------------- */

async function fetchMargin(code) {
  var url = 'https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_WEB_RZRQ_GGMX&columns=ALL' +
    '&filter=' + encodeURIComponent('(SCODE="' + code + '")') +
    '&pageNumber=1&pageSize=6&sortColumns=DATE&sortTypes=-1&source=WEB&client=WEB';
  var r = await raw(url, { referer: 'https://data.eastmoney.com/', timeoutMs: 15000 });
  if (!r.ok) return { ok: false, value: null, source: null, errors: ['两融接口 HTTP ' + r.status] };
  try {
    var j = JSON.parse(r.text);
    var arr = (j && j.result && j.result.data) || [];
    if (!arr.length) return { ok: false, value: null, source: null, errors: ['两融数据为空（该股可能非两融标的）'] };
    var d = arr[0];
    var d5 = arr[5] || arr[1] || null;
    var rz = n(d.RZYE), rz5 = d5 ? n(d5.RZYE) : null;
    return {
      ok: true, source: '东方财富融资融券',
      value: {
        date: d.DATE ? String(d.DATE).slice(0, 10) : null,
        marginBalance: rz, marginBuy: n(d.RZMRE), marginBuy3d: n(d.RZMRE3D), marginBuy5d: n(d.RZMRE5D),
        shortBalance: n(d.RQYE), shortVolume: n(d.RQYL),
        rzrqBalance: n(d.RZRQYE), rzrqChange: n(d.RZRQYECZ),
        marginBalance5dAgo: rz5,
        marginChange5dPct: (rz && rz5) ? r2((rz - rz5) / rz5 * 100, 2) : null,
        priceChange5dPct: n(d.RCHANGE5DCP)
      }
    };
  } catch (e) {
    return { ok: false, value: null, source: null, errors: ['两融解析失败'] };
  }
}

/* ---------------- A 股：北向持股 ---------------- */

async function fetchNorthbound(code) {
  var url = 'https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_MUTUAL_HOLDSTOCKNORTH_STA&columns=ALL' +
    '&filter=' + encodeURIComponent('(SECURITY_CODE="' + code + '")') +
    '&pageNumber=1&pageSize=3&sortColumns=TRADE_DATE&sortTypes=-1&source=WEB&client=WEB';
  var r = await raw(url, { referer: 'https://data.eastmoney.com/', timeoutMs: 15000 });
  if (!r.ok) return { ok: false, value: null, source: null, errors: ['北向接口 HTTP ' + r.status] };
  try {
    var j = JSON.parse(r.text);
    var arr = (j && j.result && j.result.data) || [];
    if (!arr.length) return { ok: false, value: null, source: null, errors: ['北向持股数据为空'] };
    var d = arr[0], p = arr[1] || null;
    var shares = n(d.HOLD_SHARES), cap = n(d.HOLD_MARKET_CAP);
    var pShares = p ? n(p.HOLD_SHARES) : null;
    return {
      ok: true, source: '东方财富北向持股',
      value: {
        tradeDate: d.TRADE_DATE ? String(d.TRADE_DATE).slice(0, 10) : null,
        holdShares: shares, holdMarketCap: cap,
        holdPctOfA: n(d.A_SHARES_RATIO), holdPctOfFloat: n(d.FREE_SHARES_RATIO),
        changeRate: n(d.CHANGE_RATE),
        sharesChange1d: pShares ? (shares - pShares) : null,
        sharesChange1dPct: (shares && pShares) ? r2((shares - pShares) / pShares * 100, 2) : null
      }
    };
  } catch (e) {
    return { ok: false, value: null, source: null, errors: ['北向解析失败'] };
  }
}

/* ---------------- A 股：主力资金流向（个股） ---------------- */

async function fetchMainFlow(code) {
  var url = 'https://push2.eastmoney.com/api/qt/stock/fflow/kline/get?secid=' + encodeURIComponent(secidOf(code)) +
    '&klt=101&lmt=10&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65';
  var r = await raw(url, { timeoutMs: 12000 });
  if (r.ok) {
    try {
      var j = JSON.parse(r.text);
      var kl = j && j.data && j.data.klines;
      if (kl && kl.length) {
        var rows = kl.map(function (line) {
          var p = line.split(',');
          return { date: p[0], mainNet: n(p[1]), smallNet: n(p[2]), mediumNet: n(p[3]),
            largeNet: n(p[4]), xlargeNet: n(p[5]), mainPct: n(p[6]) };
        });
        var last = rows[rows.length - 1];
        var sum5 = rows.slice(-5).reduce(function (a, x) { return a + (x.mainNet || 0); }, 0);
        return {
          ok: true, source: '东方财富资金流向', asOf: last.date,
          value: { date: last.date, mainNet: last.mainNet, mainPct: last.mainPct,
            largeNet: last.largeNet, xlargeNet: last.xlargeNet,
            mainNet5d: r2(sum5, 2), days: rows.length, rows: rows }
        };
      }
    } catch (e) { }
  }
  /* 备源：从实时行情的 f62 拿当日主力净流入（单日） */
  var q = await fetchQuoteA(code);
  if (q.ok && isNum(q.quote.mainInflow)) {
    return { ok: true, source: '东方财富行情（f62 当日主力净流入）', asOf: q.quote.asOf,
      value: { date: null, mainNet: q.quote.mainInflow, mainPct: q.quote.mainInflowPct, mainNet5d: null, days: 1 } };
  }
  return { ok: false, value: null, source: null, errors: ['资金流向接口不可用'] };
}

/* ---------------- 行业景气（板块） ---------------- */

/* 东财个股接口常限流，备源顺序：东财 → 新浪行业分类 → 估值接口的 boardName */
async function fetchSector(code) {
  var errors = [];

  /* 主源：东财（UTF-8，不能走 GBK 解码） */
  var url = 'https://push2.eastmoney.com/api/qt/stock/get?fltt=2&invt=2&secid=' +
    encodeURIComponent(secidOf(code)) + '&fields=f12,f14,f127,f128,f140,f141,f100,f102';
  var r = await raw(url, { timeoutMs: 10000 });
  if (r.ok) {
    try {
      var j = JSON.parse(r.text);
      var d = j && j.data;
      if (d && d.f128) {
        return { ok: true, source: '东方财富个股行业', asOf: nowBj(),
          value: { sectorCode: d.f127 || null, sectorName: d.f128 || null,
            industryName: d.f140 || null, industryCode: d.f141 || null, listName: d.f100 || null } };
      }
    } catch (e) { errors.push('东财行业解析失败'); }
  } else errors.push('东财行业 HTTP ' + r.status);

  /* 备源：新浪行业分类表（按代码前缀匹配行业名） */
  try {
    var h = await rawGbk('https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php',
      { referer: 'https://finance.sina.com.cn', timeoutMs: 12000 });
    if (h.ok) {
      var m = h.text.match(/=\s*(\{[\s\S]*\})/);
      if (m) {
        var map = JSON.parse(m[1]);
        var key = null;
        Object.keys(map).forEach(function (k) {
          if (map[k] && String(map[k]).indexOf(code) >= 0) key = k;
        });
        if (key) {
          var label = String(map[key]).split(',')[0];
          return { ok: true, source: '新浪财经行业分类', asOf: nowBj(),
            value: { sectorCode: null, sectorName: label, industryName: label,
              industryCode: key, listName: label } };
        }
      }
    }
  } catch (e) { errors.push('新浪行业解析失败'); }

  /* 兜底：估值接口已返回所属板块名（东财为 UTF-8，直接用 latin1→utf8 还原） */
  try {
    var v = await fetchValuation(code);
    if (v.ok && v.value.boardName) {
      return { ok: true, source: '东方财富估值分析（所属板块）', asOf: nowBj(),
        value: { sectorCode: null, sectorName: v.value.boardName, industryName: v.value.boardName,
          industryCode: null, listName: v.value.boardName } };
    }
  } catch (e) { }

  return { ok: false, value: null, source: null, errors: errors.concat(['行业分类不可用']) };
}

/* ---------------- 美股：指数与中概股 ---------------- */

/*
 * 新浪美股字段口径（2026-11-13 用腾讯 usNVDA 逐字段对账后确定，勿凭印象改）：
 *
 *   0 名称 | 1 现价 | 2 涨跌幅% | 3 行情时间(北京) | 4 涨跌额
 *   5 今开 | 6 最高 | 7 最低 | 8 52周高 | 9 52周低 | 10 成交量
 *   24/25 美东时间 | 其后一格 = 昨收
 *
 * 踩过并已修掉的两个坑：
 *   a. 旧代码把 a[5] 当昨收 → 实际是「今开」。后果：英伟达真实 -4.34% 被算成 +10.06%，
 *      台积电真实 -1.90% 被算成 +2.75%（连涨跌方向都反了）。昨收位置随品种漂移
 *      （个股 ES 时间在 25、指数在 25 但 24 为空），因此改为「定位最后一个美东时间字段，取其后一格」。
 *   b. 指数旧用 int_dji / int_nasdaq / int_sp500 —— 这组符号返回的是**过期快照**
 *      （道指 46247.29，实际 52297.09，差 11%）。改用 gb_$dji / gb_$ixic / gb_$inx，
 *      与腾讯 usDJI/usIXIC/usINX 完全一致。
 */
var US_EST_RE = /^[A-Z][a-z]{2} \d{1,2} \d{1,2}:\d{2}(AM|PM) EST/;

/* 美东时间字段之后那一格 = 昨收；定位失败返回 null */
function usPrevClose(arr) {
  var hit = -1;
  for (var i = 0; i < arr.length; i++) {
    if (US_EST_RE.test(arr[i])) hit = i;
  }
  if (hit < 0) return null;
  var v = parseFloat(arr[hit + 1]);
  return isNum(v) && v > 0 ? v : null;
}

async function fetchUsMarket(symbols) {
  var syms = symbols || {
    indices: [['gb_$dji', '道琼斯'], ['gb_$ixic', '纳斯达克'], ['gb_$inx', '标普500']],
    adr: [['gb_baba', '阿里巴巴'], ['gb_pdd', '拼多多'], ['gb_jd', '京东'], ['gb_nio', '蔚来'],
      ['gb_bidu', '百度'], ['gb_xpev', '小鹏汽车'], ['gb_li', '理想汽车'], ['gb_tme', '腾讯音乐']],
    leaders: [['gb_nvda', '英伟达'], ['gb_amd', '超威半导体'], ['gb_tsm', '台积电'], ['gb_avgo', '博通'],
      ['gb_aapl', '苹果'], ['gb_msft', '微软'], ['gb_tsla', '特斯拉']]
  };
  var all = syms.indices.concat(syms.adr).concat(syms.leaders || []);
  var list = all.map(function (x) { return x[0]; }).join(',');
  var r = await rawGbk('https://hq.sinajs.cn/list=' + list, { referer: 'https://finance.sina.com.cn', timeoutMs: 12000 });
  var out = { indices: [], adr: [], leaders: [], errors: [], asOf: nowBj(), source: '新浪财经全球行情' };
  if (!r.ok) { out.errors.push('新浪美股 HTTP ' + r.status); return out; }

  var map = {};
  all.forEach(function (x) { map[x[0]] = x[1]; });
  r.text.split(/\n/).forEach(function (line) {
    /* 指数符号含 $（gb_$dji），字符类必须放行 */
    var m = line.match(/hq_str_([a-z0-9_$]+)="([^"]*)"/i);
    if (!m) return;
    var key = m[1], a = m[2].split(',');
    var label = map[key] || key;

    /* 旧式 4 字段指数（int_*）：保留解析但不再使用，仅作兜底 */
    if (a.length === 4 && isNum(parseFloat(a[1]))) {
      out.indices.push({ symbol: key, name: label, price: parseFloat(a[1]),
        change: parseFloat(a[2]), changePct: r2(parseFloat(a[3]), 2), asOf: out.asOf,
        isIndex: true, staleIntel: true });
      return;
    }
    if (a.length > 10 && isNum(parseFloat(a[1]))) {
      var now = parseFloat(a[1]);
      var prev = usPrevClose(a);
      var sinaPct = parseFloat(a[2]);
      var pct = (now && prev) ? r2((now - prev) / prev * 100, 2)
        : (isNum(sinaPct) ? r2(sinaPct, 2) : null);
      var item = {
        symbol: key, name: label, price: now,
        change: (now && prev) ? r2(now - prev, 3) : (isNum(parseFloat(a[4])) ? r2(parseFloat(a[4]), 3) : null),
        changePct: pct,
        prevClose: prev,
        open: isNum(parseFloat(a[5])) ? parseFloat(a[5]) : null,
        high: isNum(parseFloat(a[6])) ? parseFloat(a[6]) : null,
        low: isNum(parseFloat(a[7])) ? parseFloat(a[7]) : null,
        volume: isNum(parseFloat(a[10])) ? parseFloat(a[10]) : null,
        asOf: a[3] || out.asOf,
        usTime: (function () {
          for (var i = a.length - 1; i >= 0; i--) if (US_EST_RE.test(a[i])) return a[i];
          return null;
        })(),
        isIndex: /^gb_\$|^int_/.test(key)
      };
      if (item.isIndex) out.indices.push(item);
      else if (syms.leaders && syms.leaders.some(function (x) { return x[0] === key; })) out.leaders.push(item);
      else out.adr.push(item);
    }
  });
  /* 指数字段缺失时明确报错，不拿过期快照充数 */
  if (out.indices.some(function (x) { return x.staleIntel; })) {
    out.errors.push('指数退化为过期快照（int_*），结论请谨慎');
  }
  if (!out.indices.length && !out.adr.length) out.errors.push('新浪美股返回无有效数据');
  return out;
}

/* 腾讯美股：usDJI / usIXIC / usINX / usNVDA 等（备源） */
async function fetchUsMarketTencent(symbols) {
  var list = symbols || ['usDJI', 'usIXIC', 'usINX', 'usNVDA', 'usAMD', 'usTSM', 'usAVGO', 'usTSLA', 'usAAPL', 'usMSFT'];
  var r = await rawGbk('https://qt.gtimg.cn/q=' + list.join(','), { timeoutMs: 12000 });
  var out = { indices: [], leaders: [], errors: [], asOf: nowBj(), source: '腾讯行情（美股）' };
  if (!r.ok) { out.errors.push('腾讯美股 HTTP ' + r.status); return out; }
  r.text.split(/\n/).forEach(function (line) {
    var m = line.match(/v_([a-zA-Z0-9_]+)="([^"]*)"/);
    if (!m) return;
    var a = m[2].split('~');
    if (a.length < 35 || !isNum(parseFloat(a[3]))) return;
    var item = { symbol: m[1], name: a[1], price: parseFloat(a[3]), prevClose: parseFloat(a[4]),
      change: parseFloat(a[31]), changePct: r2(parseFloat(a[32]), 2), high: parseFloat(a[33]),
      low: parseFloat(a[34]), asOf: a[30] };
    if (/^us(DJI|IXIC|INX|SPX)$/.test(m[1])) out.indices.push(item);
    else out.leaders.push(item);
  });
  if (!out.indices.length) out.errors.push('腾讯美股返回无有效指数');
  return out;
}

/* ---------------- 宏观 ---------------- */

async function fetchMacro() {
  var out = { errors: [], asOf: nowBj(), sources: [] };
  /* 汇率 + 大宗：新浪 */
  var r = await rawGbk('https://hq.sinajs.cn/list=fx_susdcny,hf_CL,hf_GC',
    { referer: 'https://finance.sina.com.cn', timeoutMs: 12000 });
  if (r.ok) {
    var m = r.text.match(/hq_str_fx_susdcny="([^"]*)"/);
    if (m) {
      var a = m[1].split(',');
      out.usdcny = { value: parseFloat(a[1]), name: a[13], changePct: r2(parseFloat(a[10]), 2), date: a[17], asOf: out.asOf };
      out.sources.push('新浪财经 人民币汇率');
    }
    var cl = r.text.match(/hq_str_hf_CL="([^"]*)"/);
    if (cl) {
      /* hf_ 字段：0最新 1涨跌额(常为空) 2买价 3卖价 4最高 5最低 6时间 7昨收 13名称 */
      var b = cl[1].split(',');
      var cp = parseFloat(b[0]), pc = parseFloat(b[7]);
      out.oil = { name: b[13], price: cp, prevClose: pc,
        changePct: (cp && pc) ? r2((cp - pc) / pc * 100, 2) : null,
        change: isNum(parseFloat(b[1])) ? parseFloat(b[1]) : (cp && pc ? r2(cp - pc, 2) : null),
        high: parseFloat(b[4]), low: parseFloat(b[5]), asOf: b[6] };
      out.sources.push('新浪财经 纽约原油');
    }
    var gc = r.text.match(/hq_str_hf_GC="([^"]*)"/);
    if (gc) {
      var g = gc[1].split(',');
      var gp = parseFloat(g[0]), gpc = parseFloat(g[7]);
      out.gold = { name: g[13], price: gp, prevClose: gpc,
        changePct: (gp && gpc) ? r2((gp - gpc) / gpc * 100, 2) : null,
        change: isNum(parseFloat(g[1])) ? parseFloat(g[1]) : (gp && gpc ? r2(gp - gpc, 2) : null),
        high: parseFloat(g[4]), low: parseFloat(g[5]), asOf: g[6] };
      out.sources.push('新浪财经 纽约黄金');
    }
  } else out.errors.push('新浪宏观 HTTP ' + r.status);

  /* A 股基准指数：用新浪精简接口 s_ 前缀，字段最稳定 */
  var si = await rawGbk('https://hq.sinajs.cn/list=s_sh000001,s_sz399001,s_sz399006',
    { referer: 'https://finance.sina.com.cn', timeoutMs: 12000 });
  if (si.ok) {
    var re = /hq_str_s_(sh000001|sz399001|sz399006)="([^"]*)"/g;
    var mm, names = { sh000001: '上证指数', sz399001: '深证成指', sz399006: '创业板指' };
    while ((mm = re.exec(si.text))) {
      var a = mm[2].split(',');
      var item = { name: names[mm[1]] || mm[1], price: parseFloat(a[1]),
        change: parseFloat(a[2]), changePct: r2(parseFloat(a[3]), 2), asOf: out.asOf, source: '新浪行情 s_ 精简接口' };
      if (mm[1] === 'sh000001') out.shanghai = item;
      if (mm[1] === 'sz399001') out.shenzhen = item;
      if (mm[1] === 'sz399006') out.chinext = item;
    }
    if (out.shanghai) out.sources.push('上证指数 / 深证成指 / 创业板指 新浪行情');
    else out.errors.push('指数精简接口无数据');
  } else out.errors.push('新浪指数 HTTP ' + si.status);

  /* 补充：上证指数成交额（精简接口缺成交额，用全量接口补） */
  try {
    var fi = await rawGbk('https://hq.sinajs.cn/list=sh000001', { referer: 'https://finance.sina.com.cn', timeoutMs: 10000 });
    if (fi.ok) {
      var fm = fi.text.match(/hq_str_sh000001="([^"]*)"/);
      if (fm && out.shanghai) {
        var fa = fm[1].split(',');
        out.shanghai.amount = parseFloat(fa[9]);
        out.shanghai.open = parseFloat(fa[1]);
        out.shanghai.high = parseFloat(fa[4]);
        out.shanghai.low = parseFloat(fa[5]);
        out.shanghai.quoteTime = fa[30] + ' ' + (fa[31] || '');
      }
    }
  } catch (e) { }
  return out;
}

/* ---------------- 批量：单只股票的完整五维数据包 ---------------- */

async function collectStock(code, opts) {
  opts = opts || {};
  var t0 = Date.now();
  var q = await fetchQuoteA(code);
  var res = {
    code: code,
    name: q.ok ? q.quote.name : code,
    quote: q.ok ? q.quote : null,
    quoteSource: q.ok ? q.source : null,
    quoteErrors: q.errors || [],
    kline: null, valuation: null, fundamentals: null, margin: null, northbound: null,
    mainFlow: null, sector: null, news: null,
    collectedAt: nowBj(),
    errors: []
  };
  if (!q.ok) res.errors.push('实时行情获取失败：' + (q.errors || []).join('；'));

  /* 并行取其余数据 */
  var names = q.ok ? q.quote.name : null;
  var [kl, val, fund, mar, nb, flow, sec] = await Promise.all([
    fetchKlineA(code, opts.klineLimit || 120),
    fetchValuation(code),
    fetchFundamentals(code),
    fetchMargin(code),
    fetchNorthbound(code),
    fetchMainFlow(code),
    fetchSector(code)
  ]);
  res.kline = kl.ok ? { rows: kl.rows, source: kl.source, asOf: kl.asOf } : null;
  res.valuation = val.ok ? Object.assign({ source: val.source }, val.value) : null;
  res.fundamentals = fund.ok ? Object.assign({ source: fund.source }, fund.value) : null;
  res.margin = mar.ok ? Object.assign({ source: mar.source }, mar.value) : null;
  res.northbound = nb.ok ? Object.assign({ source: nb.source }, nb.value) : null;
  res.mainFlow = flow.ok ? Object.assign({ source: flow.source, asOf: flow.asOf }, flow.value) : null;
  res.sector = sec.ok ? Object.assign({ source: sec.source }, sec.value) : null;

  [['日K', res.kline, kl], ['估值分位', res.valuation, val], ['业绩', res.fundamentals, fund],
   ['融资融券', res.margin, mar], ['北向持股', res.northbound, nb], ['资金流向', res.mainFlow, flow],
   ['所属行业', res.sector, sec]].forEach(function (x) {
    if (!x[1]) res.errors.push(x[0] + '：' + ((x[2] && x[2].errors && x[2].errors[0]) || '不可用'));
  });

  /* ETF / 基金类标的本身没有业绩报表与北向持股，标注为「不适用」而非「缺失」 */
  var isFund = /^15|^16|^50|^51|^56|^58/.test(code);
  res.errors = res.errors.filter(function (e) {
    if (isFund && (e.indexOf('业绩') >= 0 || e.indexOf('估值') >= 0 || e.indexOf('行业分类') >= 0 || e.indexOf('北向') >= 0)) return false;
    return true;
  });
  if (isFund) {
    res.notApplicable = ['业绩报表（ETF 无财务报表）', '估值 PE/PB（ETF 不适用）', '行业分类（按基金跟踪指数判断）', '北向持股（ETF 不适用）'];
    /* key 为因子名，reason 为说明；引擎据此把这些项从「缺失」清单中剔除 */
    res.notApplicableMap = {
      '业绩报表': '业绩报表（ETF 无财务报表，不属于数据缺失）',
      '估值分析': '估值 PE/PB（ETF 不适用，不属于数据缺失）',
      '行业分类': '行业分类（按基金跟踪指数判断，不属于数据缺失）',
      '北向持股': '北向持股（ETF 不适用，不属于数据缺失）'
    };
  }

  res.elapsedMs = Date.now() - t0;
  res.coverage = {
    total: 9, ok: ['行情', '日K', '估值', '业绩', '两融', '北向', '资金', '行业'].filter(function (k) {
      return ({ '行情': res.quote, '日K': res.kline, '估值': res.valuation, '业绩': res.fundamentals,
        '两融': res.margin, '北向': res.northbound, '资金': res.mainFlow, '行业': res.sector })[k] ? 1 : 0;
    }).length
  };
  return res;
}

module.exports = {
  raw: raw, rawGbk: rawGbk, jsonp: jsonp, decodeGbk: decodeGbk,
  nowBj: nowBj, isNum: isNum, n: n, r2: r2, secidOf: secidOf,
  fetchQuoteA: fetchQuoteA, fetchKlineA: fetchKlineA, fetchValuation: fetchValuation,
  fetchFundamentals: fetchFundamentals, fetchMargin: fetchMargin, fetchNorthbound: fetchNorthbound,
  fetchMainFlow: fetchMainFlow, fetchSector: fetchSector,
  fetchUsMarket: fetchUsMarket, fetchUsMarketTencent: fetchUsMarketTencent,
  fetchMacro: fetchMacro, collectStock: collectStock
};
