'use strict';
/*
 * engine.js — 因子计算与中美联动分析
 *
 * 两条硬规矩贯穿全文件：
 *  1. 每个结论都必须挂上「数值 + 时间 + 来源」，没有数据的因子直接标为 null 并计入缺失，
 *     绝不用模糊措辞糊过去。
 *  2. 不做方向性荐股。本模块只做客观因子计算与传导关系判定，
 *     操作建议由大模型基于这些因子生成，且必须给出触发/失效条件。
 */

var I = require('./indicators');

function isNum(v) { return v !== null && v !== undefined && v !== '' && isFinite(Number(v)); }
function n(v) { var x = parseFloat(v); return isFinite(x) ? x : null; }
function r2(v, d) { var p = Math.pow(10, d === undefined ? 2 : d); return Math.round((Number(v) || 0) * p) / p; }
function avg(arr) {
  var a = (arr || []).filter(function (x) { return isNum(x); });
  return a.length ? a.reduce(function (s, x) { return s + x; }, 0) / a.length : null;
}

/* 移动平均 */
function ma(closes, n) {
  if (!closes || closes.length < n) return null;
  var s = 0;
  for (var i = closes.length - n; i < closes.length; i++) s += closes[i];
  return r2(s / n, 3);
}

/* 线性回归斜率（按序号），用于趋势斜率 */
function slope(series) {
  var a = (series || []).filter(function (x) { return isNum(x); });
  if (a.length < 3) return null;
  var n = a.length, sx = 0, sy = 0, sxy = 0, sxx = 0;
  for (var i = 0; i < n; i++) { sx += i; sy += a[i]; sxy += i * a[i]; sxx += i * i; }
  var d = n * sxx - sx * sx;
  return d === 0 ? null : r2((n * sxy - sx * sy) / d, 4);
}

/* 线性回归 R²：判断趋势是否可靠 */
function rSquared(series) {
  var a = (series || []).filter(function (x) { return isNum(x); });
  if (a.length < 4) return null;
  var n = a.length, sx = 0, sy = 0, sxy = 0, sxx = 0, syy = 0;
  for (var i = 0; i < n; i++) { sx += i; sy += a[i]; sxy += i * a[i]; sxx += i * i; syy += a[i] * a[i]; }
  var den = n * sxx - sx * sx, den2 = n * syy - sy * sy;
  if (!den || !den2) return null;
  return r2(Math.pow((n * sxy - sx * sy) / Math.sqrt(den * den2), 2), 3);
}

/* ================= 技术面 ================= */

function computeTechnical(pack) {
  var kl = pack.kline;
  var out = { available: false, missing: [], notes: [] };
  if (!kl || !kl.rows || kl.rows.length < 21) {
    out.missing.push('日K（需至少 21 个交易日）');
    return out;
  }
  var rows = kl.rows;
  var closes = rows.map(function (x) { return x.close; });
  var vols = rows.map(function (x) { return x.volume; });
  var last = rows[rows.length - 1];
  var price = last.close;

  var ma5 = ma(closes, 5), ma10 = ma(closes, 10), ma20 = ma(closes, 20), ma60 = ma(closes, 60);
  var slope20 = slope(closes.slice(-20));
  var r2_20 = rSquared(closes.slice(-20));

  /* 位置判断 */
  var above = [];
  if (isNum(ma5) && price > ma5) above.push('MA5');
  if (isNum(ma10) && price > ma10) above.push('MA10');
  if (isNum(ma20) && price > ma20) above.push('MA20');
  if (isNum(ma60) && price > ma60) above.push('MA60');

  /* 支撑与压力：近 N 日极值 + 成交量密集成交区（更贴近真实成本密集区） */
  var win = Math.min(60, rows.length);
  var seg = rows.slice(-win);
  var highs = seg.map(function (x) { return x.high; }).filter(isNum);
  var lows = seg.map(function (x) { return x.low; }).filter(isNum);
  var resistance = highs.length ? r2(Math.max.apply(null, highs), 3) : null;
  var support = lows.length ? r2(Math.min.apply(null, lows), 3) : null;

  /* 近端关键位：近 20 日高低点 */
  var seg20 = rows.slice(-20);
  var hi20 = seg20.length ? r2(Math.max.apply(null, seg20.map(function (x) { return x.high; })), 3) : null;
  var lo20 = seg20.length ? r2(Math.min.apply(null, seg20.map(function (x) { return x.low; })), 3) : null;

  /* 成交量密集成交区：按价格分桶统计成交量，取占比最高的档位作为成本密集区 */
  var dense = null;
  try {
    var span = hi20 - lo20;
    if (isNum(span) && span > 0) {
      /* 分档宽度取区间 1/10，但设上下限：不低于价格 0.3%，不高于价格 3%，避免低价股全挤一档 */
      var mid = (hi20 + lo20) / 2;
      var bucketSize = r2(Math.max(mid * 0.003, Math.min(span / 10, mid * 0.03)), 4);
      var buckets = {};
      var volTotal = 0;
      seg20.forEach(function (x) {
        if (!isNum(x.close) || !isNum(x.volume)) return;
        var b = Math.floor(x.close / bucketSize) * bucketSize;
        buckets[b] = (buckets[b] || 0) + x.volume;
        volTotal += x.volume;
      });
      var top = Object.keys(buckets).map(function (k) { return { price: r2(k, 4), vol: buckets[k] }; })
        .sort(function (a, b) { return b.vol - a.vol; });
      if (top.length && volTotal > 0) {
        /* 合并相邻档位，避免单个档位占比过高导致信息量过低 */
        var main = top[0];
        var adjacent = top.filter(function (x) {
          return Math.abs(x.price - main.price) <= bucketSize * 1.01;
        });
        var adjVol = adjacent.reduce(function (s, x) { return s + x.vol; }, 0);
        dense = {
          lower: r2(Math.min.apply(null, adjacent.map(function (x) { return x.price; })), 4),
          upper: r2(Math.max.apply(null, adjacent.map(function (x) { return x.price; })) + bucketSize, 4),
          price: main.price,
          sharePct: r2(adjVol / volTotal * 100, 1),
          bucketSize: bucketSize,
          bucketsCount: adjacent.length
        };
      }
    }
  } catch (e) { }

  /* 量价形态 */
  var volMa5 = ma(vols, 5), volMa20 = ma(vols, 20);
  var volRatio = (isNum(volMa5) && isNum(volMa20) && volMa20) ? r2(volMa5 / volMa20, 2) : null;
  var lastChange = last.changePct;
  var pattern = null, patternNote = '';
  if (isNum(lastChange) && isNum(volRatio)) {
    if (lastChange > 2 && volRatio > 1.5) { pattern = '放量上涨'; patternNote = '当日上涨 ' + lastChange + '% 且近 5 日均量是 20 日均量的 ' + volRatio + ' 倍'; }
    else if (lastChange < -2 && volRatio > 1.5) { pattern = '放量下跌'; patternNote = '当日下跌 ' + Math.abs(lastChange) + '% 且近 5 日均量是 20 日均量的 ' + volRatio + ' 倍'; }
    else if (lastChange > 0 && volRatio < 0.7) { pattern = '缩量上涨'; patternNote = '当日上涨 ' + lastChange + '% 但成交量低于近期均值（' + volRatio + ' 倍）'; }
    else if (lastChange < 0 && volRatio < 0.7) { pattern = '缩量下跌'; patternNote = '当日下跌 ' + Math.abs(lastChange) + '% 且成交量低于近期均值（' + volRatio + ' 倍）'; }
    else { pattern = '量价平稳'; patternNote = '涨跌幅 ' + lastChange + '%，量能 ' + volRatio + ' 倍'; }
  } else if (isNum(lastChange)) {
    pattern = '量能数据缺失'; patternNote = '当日涨跌 ' + lastChange + '%，但成交量序列不足以判断量价配合';
  }

  /* 技术指标库（MACD/KDJ/RSI/BOLL/ATR/WR/CCI/BIAS/OBV/ROC/DMA/VOL），口径对齐同花顺/通达信 */
  var ind = I.computeIndicators({
    dates: rows.map(function (x) { return x.date; }),
    open: rows.map(function (x) { return x.open; }),
    high: rows.map(function (x) { return x.high; }),
    low: rows.map(function (x) { return x.low; }),
    close: closes,
    volume: vols
  });
  var indicators = ind.ok
    ? { ok: true, signals: ind.signals, last: ind.last }
    : { ok: false, reason: ind.reason };
  /* 指标共振：MACD/KDJ/RSI/BOLL/DMA 五指标多空统计（偏多加分、偏空减分，仅供参考，不荐股） */
  var indicatorTrend = '指标分化', indicatorBull = 0, indicatorBear = 0;
  if (ind.ok) {
    var la = ind.last;
    if (la.macd.dif > 0) indicatorBull++; else indicatorBear++;
    if (la.kdj.k > la.kdj.d) indicatorBull++; else indicatorBear++;
    if (la.rsi12 >= 50) indicatorBull++; else indicatorBear++;
    if (isNum(la.boll.mid) && price >= la.boll.mid) indicatorBull++; else indicatorBear++;
    if (la.dma.ddd >= la.dma.ama) indicatorBull++; else indicatorBear++;
    indicatorTrend = indicatorBull > indicatorBear
      ? ('指标偏多 ' + indicatorBull + '/' + (indicatorBull + indicatorBear))
      : (indicatorBear > indicatorBull
        ? ('指标偏空 ' + indicatorBear + '/' + (indicatorBull + indicatorBear))
        : '指标分化');
  }

  /* 波动率：近 20 日日收益标准差年化 */
  var rets = [];
  for (var i = 1; i < closes.length; i++) {
    if (closes[i - 1] && isNum(closes[i])) rets.push((closes[i] - closes[i - 1]) / closes[i - 1] * 100);
  }
  var rets20 = rets.slice(-20);
  var meanRet = avg(rets20);
  var vola = null;
  if (rets20.length > 5) {
    var sd = Math.sqrt(rets20.reduce(function (s, x) { return s + (x - meanRet) * (x - meanRet); }, 0) / rets20.length);
    vola = r2(sd * Math.sqrt(244), 2);
  }

  /* 区间表现 */
  function periodReturn(k) {
    if (closes.length < k + 1) return null;
    var pc = closes[closes.length - 1 - k];
    return pc ? r2((closes[closes.length - 1] - pc) / pc * 100, 2) : null;
  }

  /* 趋势判定：斜率方向 + 均线排列 + R² 可靠性 */
  var trend = '震荡', trendBasis = [];
  if (isNum(slope20)) {
    var rel = slope20 / price * 100;
    trendBasis.push('近 20 日回归斜率 ' + slope20 + '（折合每日 ' + r2(rel, 3) + '%，R²=' + r2_20 + '）');
    if (isNum(ma5) && isNum(ma20)) trendBasis.push('MA5 ' + ma5 + ' / MA20 ' + ma20 + ' / 收盘 ' + price);
    if (slope20 > 0 && isNum(ma5) && isNum(ma20) && ma5 > ma20) { trend = '上行'; }
    else if (slope20 < 0 && isNum(ma5) && isNum(ma20) && ma5 < ma20) { trend = '下行'; }
    else if (slope20 > 0) { trend = '震荡偏强'; }
    else if (slope20 < 0) { trend = '震荡偏弱'; }
    if (isNum(r2_20) && r2_20 < 0.2) {
      trendBasis.push('R² 偏低（' + r2_20 + '），趋势可靠性不足，判定为区间震荡而非单边');
      if (trend === '上行' || trend === '下行') trend = '震荡';
    }
  }

  return {
    available: true,
    price: price, date: last.date,
    ma5: ma5, ma10: ma10, ma20: ma20, ma60: ma60,
    aboveMa: above, aboveMaText: above.length ? above.join('、') : '收盘价在所有主要均线下方',
    slope20: slope20, rSquared20: r2_20,
    trend: trend, trendBasis: trendBasis,
    pattern: pattern, patternNote: patternNote,
    volumeRatio5v20: volRatio,
    volatilityAnnualPct: vola,
    support20: lo20, resistance20: hi20,
    support60: support, resistance60: resistance,
    denseZone: dense,
    ret5: periodReturn(5), ret10: periodReturn(10), ret20: periodReturn(20), ret60: periodReturn(60),
    indicators: indicators,
    indicatorTrend: indicatorTrend, indicatorBull: indicatorBull, indicatorBear: indicatorBear,
    bars: rows.length,
    source: pack.kline.source, asOf: pack.kline.asOf
  };
}

/* ================= 资金面 ================= */

function computeCapital(pack) {
  var out = { available: false, missing: [], items: [] };
  var mf = pack.mainFlow, mar = pack.margin, nb = pack.northbound;

  if (mf && isNum(mf.mainNet)) {
    out.items.push({
      factor: '主力资金（当日）',
      value: r2(mf.mainNet / 10000, 2) + ' 万元',
      raw: mf.mainNet,
      detail: mf.mainNet5d !== null && mf.mainNet5d !== undefined
        ? '当日主力净流入 ' + r2(mf.mainNet / 10000, 0) + ' 万元，近 5 日累计 ' + r2(mf.mainNet5d / 10000, 0) + ' 万元'
        : '当日主力净流入 ' + r2(mf.mainNet / 10000, 0) + ' 万元',
      asOf: mf.date || mf.asOf, source: mf.source
    });
    out.mainNet5d = mf.mainNet5d;
    out.mainNet1d = mf.mainNet;
  } else out.missing.push('主力资金流向');

  if (mar && isNum(mar.marginBalance)) {
    out.items.push({
      factor: '融资余额',
      value: r2(mar.marginBalance / 100000000, 2) + ' 亿元',
      raw: mar.marginBalance,
      detail: '融资余额 ' + r2(mar.marginBalance / 100000000, 2) + ' 亿元' +
        (isNum(mar.marginChange5dPct) ? '，近 5 日变化 ' + mar.marginChange5dPct + '%' : '') +
        (isNum(mar.marginBuy5d) ? '，近 5 日融资买入 ' + r2(mar.marginBuy5d / 100000000, 2) + ' 亿元' : ''),
      asOf: mar.date, source: mar.source
    });
    out.marginChange5dPct = mar.marginChange5dPct;
    out.marginBalance = mar.marginBalance;
  } else out.missing.push('融资融券（可能非两融标的）');

  if (nb && isNum(nb.holdShares)) {
    out.items.push({
      factor: '北向持股',
      value: nb.holdPctOfFloat !== null && nb.holdPctOfFloat !== undefined
        ? '占流通盘 ' + nb.holdPctOfFloat + '%' : (nb.holdMarketCap ? r2(nb.holdMarketCap / 100000000, 2) + ' 亿元' : '有数据'),
      raw: nb.holdShares,
      detail: '持股 ' + nb.holdShares + ' 股' +
        (isNum(nb.holdPctOfFloat) ? '，占流通盘 ' + nb.holdPctOfFloat + '%' : '') +
        (isNum(nb.sharesChange1dPct) ? '，最新一期较上期变化 ' + nb.sharesChange1dPct + '%' : ''),
      asOf: nb.tradeDate, source: nb.source
    });
    out.northHoldPct = nb.holdPctOfFloat;
    out.northChangePct = nb.sharesChange1dPct;
    out.northAsOf = nb.tradeDate;
  } else out.missing.push('北向持股');

  /* 成交活跃度 */
  if (pack.quote && isNum(pack.quote.amount)) {
    out.items.push({
      factor: '成交额',
      value: r2(pack.quote.amount / 100000000, 2) + ' 亿元',
      raw: pack.quote.amount,
      detail: '当日成交额 ' + r2(pack.quote.amount / 100000000, 2) + ' 亿元' +
        (isNum(pack.quote.turnoverPct) ? '，换手率 ' + pack.quote.turnoverPct + '%' : ''),
      asOf: pack.quote.asOf, source: pack.quote.source
    });
  }

  out.available = out.items.length > 0;
  return out;
}

/* ================= 基本面 ================= */

function computeFundamental(pack) {
  var out = { available: false, missing: [], items: [] };
  var f = pack.fundamentals, v = pack.valuation;

  if (f) {
    out.items.push({
      factor: '最新报告期业绩',
      value: f.reportDate ? f.reportDate + ' 报告期' : '有数据',
      detail: (isNum(f.revenue) ? '营业收入 ' + r2(f.revenue / 100000000, 2) + ' 亿元' : '营收缺失') +
        (isNum(f.revenueYoy) ? '（同比 ' + (f.revenueYoy >= 0 ? '+' : '') + r2(f.revenueYoy, 2) + '%）' : '') +
        (isNum(f.netProfit) ? '；归母净利 ' + r2(f.netProfit / 100000000, 2) + ' 亿元' : '；净利缺失') +
        (isNum(f.netProfitYoy) ? '（同比 ' + (f.netProfitYoy >= 0 ? '+' : '') + r2(f.netProfitYoy, 2) + '%）' : '') +
        (isNum(f.roe) ? '；加权 ROE ' + r2(f.roe, 2) + '%' : '') +
        (isNum(f.grossMargin) ? '；毛利率 ' + r2(f.grossMargin, 2) + '%' : ''),
      asOf: f.reportDate, source: f.source
    });
    out.revenueYoy = f.revenueYoy; out.netProfitYoy = f.netProfitYoy;
    out.roe = f.roe; out.reportDate = f.reportDate;
    out.noticeDate = f.noticeDate;
    /* 业绩趋势：本期 vs 上期同比 */
    if (isNum(f.netProfitYoy) && isNum(f.prevNetProfitYoy)) {
      out.earningsTrend = f.netProfitYoy >= f.prevNetProfitYoy ? '同比增速加快' : '同比增速放缓';
      out.earningsTrendDetail = '本期净利同比 ' + f.netProfitYoy + '%，上期 ' + f.prevNetProfitYoy + '%（' + f.reportDate + ' 对比 ' + f.prevReportDate + '）';
    }
  } else out.missing.push('业绩报表');

  if (v) {
    var peTxt = isNum(v.peTtm) ? 'PE(TTM) ' + r2(v.peTtm, 2) + ' 倍' : 'PE 缺失';
    var pbTxt = isNum(v.pbMrq) ? 'PB ' + r2(v.pbMrq, 2) + ' 倍' : 'PB 缺失';
    out.items.push({
      factor: '估值',
      value: peTxt + ' / ' + pbTxt,
      detail: peTxt + '；' + pbTxt +
        (isNum(v.pePercentile) ? '；PE 近 3 年分位 ' + r2(v.pePercentile, 1) + '%' : '；PE 分位该接口未提供') +
        (isNum(v.marketCap) || isNum(v.totalMarketCap) ? '；总市值 ' + r2((v.totalMarketCap || v.marketCap) / 100000000, 0) + ' 亿元' : ''),
      asOf: v.tradeDate, source: v.source
    });
    out.peTtm = v.peTtm; out.pbMrq = v.pbMrq; out.pePercentile = v.pePercentile;
    out.boardName = v.boardName;
    out.valuationNote = isNum(v.pePercentile)
      ? 'PE 分位 ' + v.pePercentile + '%'
      : '本次接口未返回 PE 历史分位，改以 PE/PB 绝对值与所处板块对比';
  } else out.missing.push('估值分析');

  if (pack.sector) {
    out.items.push({
      factor: '所属行业',
      value: pack.sector.sectorName || pack.sector.industryName || '有数据',
      detail: '所属 ' + (pack.sector.sectorName || pack.sector.industryName || '未知') + ' 板块',
      asOf: pack.sector.asOf, source: pack.sector.source
    });
    out.industry = pack.sector.sectorName || pack.sector.industryName;
  } else out.missing.push('行业分类');

  out.available = out.items.length > 0;
  return out;
}

/* ================= 情绪面 ================= */

/* 从新闻标题做词频情绪；这是启发式，必须标注 */
function scoreNewsSentiment(news, keywords) {
  if (!news || !news.length) return { available: false, score: null, count: 0, positive: [], negative: [], missing: ['资讯'] };
  var pos = ['增长', '突破', '创新高', '超预期', '扩产', '中标', '订单', '利好', '大涨', '上涨', '获批', '量产', '合作', '增持', '回购', '提价', '新高'];
  var neg = ['下滑', '亏损', '减持', '低于预期', '处罚', '退市', '大跌', '下跌', '风险', '警告', '问询', '暂停', '减产', '降价', '诉讼', '暴跌'];
  var p = 0, ng = 0, posHits = [], negHits = [];
  news.forEach(function (x) {
    var t = String(x.title || '');
    pos.forEach(function (w) { if (t.indexOf(w) >= 0) { p++; if (posHits.indexOf(w) < 0) posHits.push(w); } });
    neg.forEach(function (w) { if (t.indexOf(w) >= 0) { ng++; if (negHits.indexOf(w) < 0) negHits.push(w); } });
  });
  var total = p + ng;
  var score = total ? r2((p - ng) / total, 2) : 0;
  var label = score > 0.25 ? '偏正面' : (score < -0.25 ? '偏负面' : '中性');
  return {
    available: true, score: score, label: label, count: news.length,
    positiveWords: posHits, negativeWords: negHits,
    headline: news.slice(0, 5).map(function (x) {
      return { title: x.title, media: x.media, date: x.date, url: x.url };
    }),
    note: '基于标题词频的启发式打分，非事实判断；需核实原文'
  };
}

function computeSentiment(pack, news, marketCtx) {
  var out = { available: false, missing: [], items: [] };
  /* news 允许传数组（兼容旧调用）或 {items, source, keyword, error} 对象 */
  var newsArr = Array.isArray(news) ? news : ((news && news.items) || []);
  var newsMeta = Array.isArray(news) ? {} : (news || {});
  var s = scoreNewsSentiment(newsArr, null);
  if (s.available) {
    out.items.push({
      factor: '个股新闻情绪（词频）',
      value: s.label + '（' + s.score + '）',
      detail: '近 ' + s.count + ' 条资讯中正面词命中 ' + s.positiveWords.length +
        ' 类（' + (s.positiveWords.join('、') || '无') + '），负面词命中 ' + s.negativeWords.length +
        ' 类（' + (s.negativeWords.join('、') || '无') + '）',
      asOf: newsArr[0] && newsArr[0].date,
      source: newsMeta.source || '新闻标题词频（东方财富资讯搜索）'
    });
    out.newsScore = s.score; out.newsLabel = s.label; out.newsCount = s.count;
    out.headlines = s.headline;
    out.newsKeyword = newsMeta.keyword || null;
  } else {
    out.missing.push('个股资讯');
    if (newsMeta.error) out.newsError = newsMeta.error;
  }

  /* 市场情绪：用大盘与宽度近似 */
  if (marketCtx && marketCtx.shanghai) {
    var idx = marketCtx.shanghai;
    out.items.push({
      factor: '大盘情绪',
      value: idx.changePct >= 0 ? '偏暖（' + idx.changePct + '%）' : '偏冷（' + idx.changePct + '%）',
      detail: '上证指数当日 ' + idx.changePct + '%（' + r2(idx.price, 2) + ' 点），' +
        (marketCtx.chinext ? '创业板指 ' + marketCtx.chinext.changePct + '%' : ''),
      asOf: idx.asOf, source: idx.source
    });
    out.marketTone = idx.changePct;
  }

  out.available = out.items.length > 0;
  return out;
}

/* ================= 宏观与政策面 ================= */

function computeMacro(macro) {
  var out = { available: false, missing: [], items: [] };
  if (!macro) { out.missing.push('宏观数据'); return out; }

  if (macro.usdcny && isNum(macro.usdcny.value)) {
    out.items.push({
      factor: '人民币汇率（USDCNY）',
      value: macro.usdcny.value + (isNum(macro.usdcny.changePct) ? '（' + macro.usdcny.changePct + '%）' : ''),
      detail: '在岸人民币 ' + macro.usdcny.value + '，日内变动 ' + (isNum(macro.usdcny.changePct) ? macro.usdcny.changePct + '%' : '无数据') +
        '。人民币走强通常利于外资流入与外资重仓股估值修复；走弱则相反。',
      asOf: macro.usdcny.date, source: '新浪财经 人民币汇率'
    });
    out.usdcny = macro.usdcny.value;
    out.usdcnyChangePct = macro.usdcny.changePct;
  } else out.missing.push('人民币汇率');

  if (macro.oil && isNum(macro.oil.price)) {
    out.items.push({
      factor: '纽约原油',
      value: r2(macro.oil.price, 2) + ' 美元/桶' + (isNum(macro.oil.changePct) ? '（' + macro.oil.changePct + '%）' : ''),
      detail: '原油 ' + r2(macro.oil.price, 2) + ' 美元/桶' +
        (isNum(macro.oil.changePct) ? '，当日 ' + macro.oil.changePct + '%' : '') +
        '。油价上行利好石油开采，抬高化工与运输成本。',
      asOf: macro.oil.asOf, source: '新浪财经 纽约原油'
    });
    out.oil = macro.oil.price;
  } else out.missing.push('原油');

  if (macro.gold && isNum(macro.gold.price)) {
    out.items.push({
      factor: '纽约黄金',
      value: r2(macro.gold.price, 2) + ' 美元/盎司' + (isNum(macro.gold.changePct) ? '（' + macro.gold.changePct + '%）' : ''),
      detail: '金价 ' + r2(macro.gold.price, 2) + ' 美元/盎司' +
        (isNum(macro.gold.changePct) ? '，当日 ' + macro.gold.changePct + '%' : '') +
        '。金价与美元指数同向反映避险情绪。',
      asOf: macro.gold.asOf, source: '新浪财经 纽约黄金'
    });
    out.gold = macro.gold.price;
  } else out.missing.push('黄金');

  /* 政策取向：本地无政策原文接口，用可核查的宏观变量替代，并明确说明 */
  out.items.push({
    factor: '政策取向（代理口径）',
    value: '本次未接入政策原文，仅用汇率与商品代理',
    detail: '系统未接入政策文本源，因此不对政策取向做判断。请自行核对相关部委公告与行业政策文件。',
    asOf: macro.asOf, source: '本机判断（明确标注为无数据）'
  });
  out.policyAvailable = false;

  out.available = out.items.length > 0;
  return out;
}

/* ================= 中美联动分析 ================= */

/*
 * 判断当前中美市场的联动/背离。
 * 逻辑：美股科技龙头是 A 股科技板块的先行指标；
 *   - 美股科技涨 + A 股科技跌 → 背离（可能是本土因素主导）
 *   - 美股科技跌 + A 股科技跌 → 联动（外部传导）
 *   - 方向一致 → 联动
 * 中概股是 A 股互联网/消费情绪的直接镜像。
 */
function computeUsChinaLink(usMarket, usTencent, pack, macro) {
  var out = { available: false, missing: [], items: [], relations: [] };

  var idx = (usMarket && usMarket.indices) || [];
  var adr = (usMarket && usMarket.adr) || [];
  /* 科技龙头：优先用与指数同源的（新浪），没有再用腾讯备源 */
  var leaders = (usMarket && usMarket.leaders && usMarket.leaders.length)
    ? usMarket.leaders
    : ((usTencent && usTencent.leaders) || []);
  var leaderSource = (usMarket && usMarket.leaders && usMarket.leaders.length)
    ? usMarket.source : (usTencent ? usTencent.source : '未知来源');

  /* 美股指数 */
  var nasdaq = idx.filter(function (x) { return /纳斯达克|纳指/.test(x.name); })[0] ||
    idx.filter(function (x) { return /纳斯达克/.test(x.name); })[0] || null;
  var spx = idx.filter(function (x) { return /标普/.test(x.name); })[0] || null;
  var dji = idx.filter(function (x) { return /道琼斯/.test(x.name); })[0] || null;

  /* 科技龙头：英伟达、台积电、AMD、博通 */
  var semiNames = /英伟达|NVIDIA|台积电|TSMC|超威|AMD|博通|Broadcom|美光/i;
  var semiLeaders = leaders.filter(function (x) { return semiNames.test(x.name + ' ' + x.symbol); });

  /* 中概 */
  var adrAvg = r2(avg(adr.map(function (x) { return x.changePct; })), 2);

  if (nasdaq) {
    out.items.push({
      factor: '纳斯达克指数',
      value: (nasdaq.changePct >= 0 ? '+' : '') + nasdaq.changePct + '%',
      detail: '纳斯达克当日 ' + nasdaq.changePct + '%（' + r2(nasdaq.price, 2) + ' 点）' +
        (spx ? '；标普500 ' + spx.changePct + '%' : '') + (dji ? '；道琼斯 ' + dji.changePct + '%' : ''),
      asOf: nasdaq.asOf, source: usMarket.source
    });
    out.nasdaqChangePct = nasdaq.changePct;
    out.spxChangePct = spx ? spx.changePct : null;
  } else out.missing.push('纳斯达克指数');

  if (semiLeaders.length) {
    /* 均值先收敛到 2 位小数，避免 -0.5174999999999998 这类浮点尾巴进入文本 */
    var semiAvg = r2(avg(semiLeaders.map(function (x) { return x.changePct; })), 2);
    out.items.push({
      factor: '美股科技龙头',
      value: semiLeaders.map(function (x) { return x.name + ' ' + (x.changePct >= 0 ? '+' : '') + r2(x.changePct, 2) + '%'; }).join('、'),
      detail: '台积电/英伟达/AMD/博通等 ' + semiLeaders.length + ' 只龙头平均 ' + (semiAvg >= 0 ? '+' : '') + r2(semiAvg, 2) +
        '%，这是 A 股半导体与 AI 板块最直接的隔夜先行指标。',
      asOf: semiLeaders[0].asOf, source: leaderSource
    });
    out.semiLeaderAvgPct = semiAvg;
    out.semiLeaders = semiLeaders.map(function (x) { return { name: x.name, changePct: x.changePct, price: x.price }; });
  } else out.missing.push('美股科技龙头');

  if (adr.length) {
    out.items.push({
      factor: '中概股隔夜',
      value: adr.map(function (x) { return x.name + ' ' + (x.changePct >= 0 ? '+' : '') + r2(x.changePct, 2) + '%'; }).join('、'),
      detail: '中概股隔夜平均 ' + (adrAvg >= 0 ? '+' : '') + r2(adrAvg, 2) + '%，' +
        '这是 A 股互联网、消费与新能源中概映射标的的隔夜情绪参考。',
      asOf: adr[0].asOf, source: usMarket.source
    });
    out.adrAvgPct = adrAvg;
    out.adr = adr.map(function (x) { return { name: x.name, changePct: x.changePct, price: x.price }; });
  } else out.missing.push('中概股');

  /* A 股当日表现（用于判定联动还是背离） */
  var aChange = pack.quote ? r2(pack.quote.changePct, 2) : null;
  var aChange5 = null;
  if (pack.kline && pack.kline.rows && pack.kline.rows.length > 5) {
    var rows = pack.kline.rows;
    var pc = rows[rows.length - 6].close;
    aChange5 = pc ? r2((rows[rows.length - 1].close - pc) / pc * 100, 2) : null;
  }

  /* 联动判定：美股科技基准 = 可得的指数（优先纳指）+ 科技龙头均值，两者至少要有一个 */
  var usTechParts = [];
  if (isNum(out.nasdaqChangePct)) usTechParts.push({ label: '纳指', v: out.nasdaqChangePct });
  if (isNum(out.semiLeaderAvgPct)) usTechParts.push({ label: '科技龙头均值', v: out.semiLeaderAvgPct });
  if (!out.nasdaqChangePct && isNum(out.spxChangePct)) usTechParts.push({ label: '标普500', v: out.spxChangePct });

  if (usTechParts.length) {
    var usTech = r2(usTechParts.reduce(function (s, x) { return s + x.v; }, 0) / usTechParts.length, 2);
    var usTechText = usTechParts.map(function (x) { return x.label + ' ' + r2(x.v, 2) + '%'; }).join('、');
    var isTechStock = pack.quote && /半导体|芯片|科技|AI|电子|算力/i.test(
      (pack.sector && (pack.sector.sectorName || pack.sector.industryName)) || '');

    if (isNum(aChange)) {
      var sameDir = (usTech >= 0 && aChange >= 0) || (usTech < 0 && aChange < 0);
      var magnitudeGap = Math.abs(aChange - usTech);

      if (sameDir && magnitudeGap < 3) {
        out.relations.push({
          type: '联动', confidence: usTechParts.length >= 2 ? '高' : '中',
          text: '判定为联动：美股科技（' + usTechText + '）与该股当日 ' + aChange + '% 同向，幅度差 ' +
            r2(magnitudeGap, 2) + ' 个百分点，属于典型的隔夜传导，外部因素是主要驱动。' +
            (usTechParts.length < 2 ? '（本次缺少纳指或科技龙头数据，置信度相应下调）' : '')
        });
      } else if (sameDir && magnitudeGap >= 3) {
        out.relations.push({
          type: '联动（幅度背离）', confidence: '中',
          text: '判定为联动但幅度背离：方向一致但幅度差 ' + r2(magnitudeGap, 2) + ' 个百分点（美股科技 ' + r2(usTech, 2) +
            '% vs 该股 ' + aChange + '%），说明外部传导之外还有本土资金或基本面因素在起作用。'
        });
      } else {
        out.relations.push({
          type: '背离', confidence: '中',
          text: '判定为背离：美股科技当日 ' + r2(usTech, 2) + '% 而该股 ' + aChange + '%，方向相反，属于背离。' +
            '该股短期走势更可能由本土资金、业绩或政策因素主导，不宜简单按美股方向外推。'
        });
      }
    }

    /* 中概 → A 股映射 */
    if (isNum(adrAvg)) {
      out.relations.push({
        type: '中概映射',
        confidence: '中',
        text: '中概映射：中概股隔夜平均 ' + r2(adrAvg, 2) + '%，方向' + (adrAvg >= 0 ? '偏正面' : '偏负面') + '。' +
          (isTechStock
            ? '该股属科技链，与中概的关联度主要在风险偏好而非业务直接相关，中概大跌通常意味着离岸资金整体谨慎，间接压制 A 股同类板块。'
            : '若该股与中概存在业务或资金关联，中概隔夜走势可作为其次日情绪的先行参考；若无直接关联，此项参考价值有限。')
      });
    }

    /* 传导路径说明 */
    out.channels = [
      { name: '估值传导', text: '美股科技股估值变化通过可比公司 PE 中枢影响 A 股同类标的的「合理估值区间」，属中期慢变量，单日涨跌传导有限。' },
      { name: '情绪传导', text: '隔夜纳指与中概的方向最容易在次日开盘半小时内反映为情绪冲击，是短期最有效的先行指标。' },
      { name: '外资偏好', text: '北向资金（当前接口为季度披露，更新有滞后）反映外资持仓而非当日流向；当日外资行为需看 RMB 汇率与成交额变化。' },
      { name: '产业链映射', text: '台积电/英伟达等龙头直接对应 A 股半导体制造与算力链的订单预期，属基本面传导，时滞以周计。' }
    ];
  } else {
    out.missing.push('联动判定所需的美股数据');
  }

  out.available = out.items.length > 0;
  return out;
}

/* ================= 汇总：单只股票的完整分析包 ================= */

function buildAnalysis(pack, marketCtx, news) {
  var tech = computeTechnical(pack);
  var cap = computeCapital(pack);
  var fund = computeFundamental(pack);
  var sent = computeSentiment(pack, news, marketCtx);
  var macro = computeMacro(marketCtx.macro);
  var link = computeUsChinaLink(marketCtx.us, marketCtx.usTencent, pack, marketCtx.macro);

  var dims = [
    { key: 'fundamental', label: '基本面', data: fund },
    { key: 'capital', label: '资金面', data: cap },
    { key: 'technical', label: '技术面', data: tech },
    { key: 'sentiment', label: '情绪面', data: sent },
    { key: 'macro', label: '宏观与政策', data: macro }
  ];

  /* 数据完整性 */
  var covered = dims.filter(function (d) { return d.data && d.data.available; }).length;
  var missing = [];
  /* 基金类标的的「不适用」项单独列出，不与真正缺失混为一谈；
     出现在 notApplicableMap 中的因子不算缺失，直接从缺失清单剔除 */
  var naMap = pack.notApplicableMap || {};
  dims.forEach(function (d) {
    (d.data && d.data.missing || []).forEach(function (m) {
      if (naMap[m]) return;
      missing.push(d.label + '：' + m);
    });
  });
  var notApplicable = pack.notApplicable || [];

  /* 维度级「不适用」：整维度都不适用于该标的（如 ETF 的基本面维度） */
  var notApplicableDims = dims.filter(function (d) {
    return !d.data.available && (d.data.missing || []).length &&
      (d.data.missing || []).every(function (m) { return !!naMap[m]; });
  }).map(function (d) { return d.label; });

  return {
    code: pack.code, name: pack.name,
    quote: pack.quote,
    quoteSource: pack.quoteSource,
    dims: dims,
    coverage: {
      covered: covered, total: dims.length,
      missing: missing, notApplicable: notApplicable,
      notApplicableDims: notApplicableDims
    },
    usChinaLink: link,
    collectedAt: pack.collectedAt,
    elapsedMs: pack.elapsedMs,
    dataErrors: pack.errors || []
  };
}

module.exports = {
  isNum: isNum, n: n, r2: r2, avg: avg, ma: ma, slope: slope, rSquared: rSquared,
  computeTechnical: computeTechnical,
  computeCapital: computeCapital,
  computeFundamental: computeFundamental,
  computeSentiment: computeSentiment,
  computeMacro: computeMacro,
  scoreNewsSentiment: scoreNewsSentiment,
  computeUsChinaLink: computeUsChinaLink,
  buildAnalysis: buildAnalysis
};
