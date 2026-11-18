'use strict';
/*
 * indicators.js — 纯 JS 技术指标实现（零依赖，口径对齐同花顺/通达信）
 *
 * 口径要点（与主流软件对齐，避免你对比时数值对不上）：
 *  - EMA 递推初值取第一根有效收盘价（通达信/同花顺一致）
 *  - MACD 柱 = 2 * (DIF - DEA)（同花顺显示口径）
 *  - BOLL 标准差用总体标准差（同花顺口径）
 *  - RSI 用 Wilder 平滑（N=6/12/24），超买>70、超卖<30
 *  - KDJ 初值 K=D=50；J=3K-2D
 *  - WR(威廉) 输出 0~100；>80 超卖区、<20 超买区（与同花顺 UI 习惯一致）
 *
 * 输入 kline 约定：{ dates:[], open:[], high:[], low:[], close:[], volume:[] }
 * 数组下标与 dates 对齐，均为数值型；长度需覆盖各指标所需最小窗口。
 */

function isNum(v) { return typeof v === 'number' && isFinite(v); }
function r2(v, n) { if (!isNum(v)) return v; var p = Math.pow(10, n === undefined ? 2 : n); return Math.round(v * p) / p; }
function last(v) { for (var i = v.length - 1; i >= 0; i--) if (isNum(v[i])) return v[i]; return null; }

function sma(src, n) {
  var out = new Array(src.length).fill(null);
  var sum = 0;
  for (var i = 0; i < src.length; i++) {
    var x = src[i];
    if (!isNum(x)) { out[i] = null; continue; }
    sum += x;
    if (i >= n) sum -= src[i - n];
    if (i >= n - 1) out[i] = r2(sum / n, 4);
  }
  return out;
}

function ema(src, n) {
  var out = new Array(src.length).fill(null);
  var k = 2 / (n + 1);
  var prev = null, started = false;
  for (var i = 0; i < src.length; i++) {
    var x = src[i];
    if (!isNum(x)) { out[i] = null; continue; }
    if (!started) { prev = x; started = true; }
    else prev = x * k + prev * (1 - k);
    out[i] = r2(prev, 4);
  }
  return out;
}

/* 极值窗口（HHV/LLV），返回与 src 等长、前 n-1 为 null 的数组 */
function hhving(src, n) {
  var out = new Array(src.length).fill(null);
  for (var i = n - 1; i < src.length; i++) {
    var m = src[i];
    for (var j = i - n + 1; j <= i; j++) if (isNum(src[j]) && (m === null || src[j] > m)) m = src[j];
    out[i] = m;
  }
  return out;
}
function llving(src, n) {
  var out = new Array(src.length).fill(null);
  for (var i = n - 1; i < src.length; i++) {
    var m = src[i];
    for (var j = i - n + 1; j <= i; j++) if (isNum(src[j]) && (m === null || src[j] < m)) m = src[j];
    out[i] = m;
  }
  return out;
}

function macd(close, fast, slow, signal) {
  fast = fast || 12; slow = slow || 26; signal = signal || 9;
  var ef = ema(close, fast), es = ema(close, slow);
  var dif = close.map(function (c, i) {
    return (isNum(ef[i]) && isNum(es[i])) ? r2(ef[i] - es[i], 4) : null;
  });
  var dea = ema(dif, signal);
  var bar = dif.map(function (d, i) {
    return (isNum(d) && isNum(dea[i])) ? r2((d - dea[i]) * 2, 4) : null;
  });
  var cross = '', golden = false, dead = false;
  for (var i = 1; i < dif.length; i++) {
    if (isNum(dif[i]) && isNum(dea[i]) && isNum(dif[i - 1]) && isNum(dea[i - 1])) {
      if (dif[i - 1] <= dea[i - 1] && dif[i] > dea[i]) { golden = true; cross = '金叉'; }
      if (dif[i - 1] >= dea[i - 1] && dif[i] < dea[i]) { dead = true; cross = '死叉'; }
    }
  }
  return { dif: dif, dea: dea, bar: bar, last: { dif: last(dif), dea: last(dea), bar: last(bar) }, cross: cross, golden: golden, dead: dead };
}

function kdj(high, low, close, n) {
  n = n || 9;
  var k = new Array(close.length).fill(null);
  var d = new Array(close.length).fill(null);
  var j = new Array(close.length).fill(null);
  var HH = hhving(high, n), LL = llving(low, n);
  var pk = 50, pd = 50;
  for (var i = n - 1; i < close.length; i++) {
    if (!isNum(HH[i]) || !isNum(LL[i])) continue;
    var rsv = (HH[i] === LL[i]) ? 50 : (close[i] - LL[i]) / (HH[i] - LL[i]) * 100;
    pk = 2 / 3 * pk + 1 / 3 * rsv;
    pd = 2 / 3 * pd + 1 / 3 * pk;
    k[i] = r2(pk, 2); d[i] = r2(pd, 2); j[i] = r2(3 * pk - 2 * pd, 2);
  }
  return { k: k, d: d, j: j, last: { k: last(k), d: last(d), j: last(j) } };
}

function rsi(close, n) {
  n = n || 6;
  var out = new Array(close.length).fill(null);
  var gain = 0, loss = 0;
  for (var i = 1; i < close.length; i++) {
    var ch = close[i] - close[i - 1];
    var g = ch > 0 ? ch : 0, l = ch < 0 ? -ch : 0;
    if (i === 1) { gain = g; loss = l; }
    else { gain = (gain * (n - 1) + g) / n; loss = (loss * (n - 1) + l) / n; }
    if (i >= n) {
      if (loss === 0) out[i] = 100;
      else { var rs = gain / loss; out[i] = r2(100 - 100 / (1 + rs), 2); }
    }
  }
  return out;
}

function boll(close, n, k) {
  n = n || 20; k = k || 2;
  var mid = sma(close, n);
  var upper = new Array(close.length).fill(null);
  var lower = new Array(close.length).fill(null);
  var pctB = new Array(close.length).fill(null);
  var width = new Array(close.length).fill(null);
  for (var i = n - 1; i < close.length; i++) {
    if (!isNum(mid[i])) continue;
    var sum = 0, cnt = 0;
    for (var t = i - n + 1; t <= i; t++) { if (isNum(close[t])) { sum += Math.pow(close[t] - mid[i], 2); cnt++; } }
    var sd = cnt ? Math.sqrt(sum / cnt) : 0; // 总体标准差（同花顺口径）
    upper[i] = r2(mid[i] + k * sd, 3);
    lower[i] = r2(mid[i] - k * sd, 3);
    width[i] = sd ? r2((upper[i] - lower[i]) / mid[i] * 100, 2) : null;
    if (sd && isNum(close[i])) pctB[i] = r2((close[i] - lower[i]) / (upper[i] - lower[i]) * 100, 2);
  }
  return { mid: mid, upper: upper, lower: lower, width: width, pctB: pctB, last: { mid: last(mid), upper: last(upper), lower: last(lower), width: last(width), pctB: last(pctB) } };
}

function atr(high, low, close, n) {
  n = n || 14;
  var tr = new Array(close.length).fill(null);
  for (var i = 1; i < close.length; i++) {
    if (!isNum(high[i]) || !isNum(low[i]) || !isNum(close[i]) || !isNum(close[i - 1])) continue;
    var a = high[i] - low[i];
    var b = Math.abs(high[i] - close[i - 1]);
    var c = Math.abs(low[i] - close[i - 1]);
    tr[i] = Math.max(a, b, c);
  }
  return { tr: tr, atr: sma(tr, n), last: { atr: last(sma(tr, n)) } };
}

function wr(high, low, close, n) {
  n = n || 10;
  var HH = hhving(high, n), LL = llving(low, n);
  var out = new Array(close.length).fill(null);
  for (var i = n - 1; i < close.length; i++) {
    if (!isNum(HH[i]) || !isNum(LL[i]) || HH[i] === LL[i]) continue;
    out[i] = r2((HH[i] - close[i]) / (HH[i] - LL[i]) * 100, 2);
  }
  return out;
}

function cci(high, low, close, n) {
  n = n || 14;
  var tp = close.map(function (c, i) {
    return (isNum(high[i]) && isNum(low[i]) && isNum(c)) ? (high[i] + low[i] + c) / 3 : null;
  });
  var ma = sma(tp, n);
  var out = new Array(close.length).fill(null);
  for (var i = n - 1; i < close.length; i++) {
    if (!isNum(ma[i])) continue;
    var sum = 0, cnt = 0;
    for (var t = i - n + 1; t <= i; t++) { if (isNum(tp[t])) { sum += Math.abs(tp[t] - ma[i]); cnt++; } }
    var md = cnt ? sum / cnt : 0;
    out[i] = (md === 0) ? 0 : r2((tp[i] - ma[i]) / (0.015 * md), 2);
  }
  return out;
}

function bias(close, n) {
  n = n || 6;
  var ma = sma(close, n);
  var out = new Array(close.length).fill(null);
  for (var i = n - 1; i < close.length; i++) {
    if (isNum(ma[i]) && ma[i] !== 0 && isNum(close[i])) out[i] = r2((close[i] - ma[i]) / ma[i] * 100, 2);
  }
  return out;
}

function obv(close, vol) {
  var out = new Array(close.length).fill(null);
  var cum = 0;
  for (var i = 0; i < close.length; i++) {
    if (!isNum(close[i]) || !isNum(vol[i])) { out[i] = (i === 0 ? 0 : out[i - 1]); continue; }
    if (i > 0 && isNum(close[i - 1])) {
      if (close[i] > close[i - 1]) cum += vol[i];
      else if (close[i] < close[i - 1]) cum -= vol[i];
    }
    out[i] = cum;
  }
  return out;
}

function roc(close, n) {
  n = n || 12;
  var out = new Array(close.length).fill(null);
  for (var i = n; i < close.length; i++) {
    if (isNum(close[i]) && isNum(close[i - n]) && close[i - n] !== 0)
      out[i] = r2((close[i] - close[i - n]) / close[i - n] * 100, 2);
  }
  return out;
}

function vr(close, vol, n) {
  n = n || 26;
  var out = new Array(close.length).fill(null);
  var avs = 0, bvs = 0, cv = 0;
  for (var i = 1; i < close.length; i++) {
    if (!isNum(close[i]) || !isNum(vol[i]) || !isNum(close[i - 1])) continue;
    if (close[i] > close[i - 1]) avs += vol[i];
    else if (close[i] < close[i - 1]) bvs += vol[i];
    else cv += vol[i];
    if (i >= n) {
      var denom = bvs + cv / 2;
      out[i] = denom === 0 ? r2((avs + cv / 2) > 0 ? 100 : 0, 2) : r2((avs + cv / 2) / denom * 100, 2);
    }
  }
  return out;
}

function dma(close, short, long, m) {
  short = short || 10; long = long || 50; m = m || 10;
  var maS = sma(close, short), maL = sma(close, long);
  var ddd = close.map(function (c, i) {
    return (isNum(maS[i]) && isNum(maL[i])) ? r2(maS[i] - maL[i], 4) : null;
  });
  var ama = sma(ddd, m);
  return { ddd: ddd, ama: ama, last: { ddd: last(ddd), ama: last(ama) } };
}

function trix(close, n, m) {
  n = n || 12; m = m || 9;
  var e1 = ema(close, n), e2 = ema(e1, n), e3 = ema(e2, n);
  var tr = new Array(close.length).fill(null);
  for (var i = 1; i < close.length; i++) {
    if (isNum(e3[i]) && isNum(e3[i - 1]) && e3[i - 1] !== 0)
      tr[i] = r2((e3[i] - e3[i - 1]) / e3[i - 1] * 100, 3);
  }
  return { trix: tr, matrix: sma(tr, m), last: { trix: last(tr) } };
}

function mavol(vol, n) {
  return sma(vol, n);
}

/* 统一入口：输入 kline，输出所有指标的最新值 + 技术信号 */
function computeIndicators(k) {
  var close = (k && k.close) || [];
  var high = (k && k.high) || close;
  var low = (k && k.low) || close;
  var vol = (k && k.volume) || [];
  if (close.length < 26) {
    return { ok: false, reason: '日K不足 26 根，无法计算核心指标（当前 ' + close.length + ' 根）', raw: {} };
  }

  var m = macd(close);
  var kd = kdj(high, low, close);
  var rs6 = rsi(close, 6), rs12 = rsi(close, 12), rs24 = rsi(close, 24);
  var bl = boll(close);
  var at = atr(high, low, close);
  var w = wr(high, low, close);
  var cc = cci(high, low, close);
  var b6 = bias(close, 6), b12 = bias(close, 12), b24 = bias(close, 24);
  var o = obv(close, vol);
  var rc = roc(close, 12);
  var v = vr(close, vol);
  var dm = dma(close);
  var tx = trix(close);
  var mv5 = mavol(vol, 5), mv10 = mavol(vol, 10);

  var sig = [];
  function push(name, value, detail, suggestion) {
    sig.push({ factor: name, value: value, detail: detail, suggestion: suggestion });
  }

  /* MACD 信号：基于最近一次交叉判定，而非「是否出现过」（避免金叉死叉都出现时误判） */
  var macdState;
  if (m.last.dif < 0) macdState = '空头区间';
  else if (m.cross === '金叉') macdState = '金叉（偏多）';
  else if (m.cross === '死叉') macdState = '死叉（偏空）';
  else macdState = '多头区间';
  push('MACD', macdState,
    'DIF ' + m.last.dif + ' / DEA ' + m.last.dea + ' / 柱 ' + m.last.bar +
    '（最近交叉：' + (m.cross || '无') + '）',
    m.golden ? '短期动能转强' : (m.dead ? '短期动能转弱' : (m.last.dif > 0 ? '多方占优' : '空方占优')));

  /* KDJ 信号 */
  var kLast = kd.last.k, dLast = kd.last.d, jLast = kd.last.j;
  var kdjState = '中性';
  if (kLast >= 80) kdjState = '超买（K≥80）';
  else if (kLast <= 20) kdjState = '超卖（K≤20）';
  else if (kLast > dLast) kdjState = '金叉区（K>D）';
  else kdjState = '死叉区（K<D）';
  push('KDJ', kdjState, 'K ' + kLast + ' / D ' + dLast + ' / J ' + jLast,
    kLast >= 80 ? '警惕回调' : (kLast <= 20 ? '关注反弹' : '跟随趋势'));

  /* RSI 信号 */
  var rsiState = '中性';
  if (rs12[rs12.length - 1] >= 70) rsiState = '超买（RSI12≥70）';
  else if (rs12[rs12.length - 1] <= 30) rsiState = '超卖（RSI12≤30）';
  push('RSI', rsiState, 'RSI6 ' + last(rs6) + ' / RSI12 ' + last(rs12) + ' / RSI24 ' + last(rs24),
    rsiState.indexOf('超买') >= 0 ? '短线过热' : (rsiState.indexOf('超卖') >= 0 ? '短线超跌' : '力度适中'));

  /* BOLL 信号 */
  var cl = close[close.length - 1];
  var bollState = '中轨附近';
  if (isNum(bl.last.upper) && cl >= bl.last.upper) bollState = '触及上轨（偏强/超买）';
  else if (isNum(bl.last.lower) && cl <= bl.last.lower) bollState = '触及下轨（偏弱/超卖）';
  push('BOLL', bollState, '上 ' + bl.last.upper + ' / 中 ' + bl.last.mid + ' / 下 ' + bl.last.lower +
    ' / 带宽 ' + bl.last.width + '% / %B ' + bl.last.pctB,
    bollState.indexOf('上轨') >= 0 ? '强势但注意回落' : (bollState.indexOf('下轨') >= 0 ? '弱势但关注支撑' : '通道内运行'));

  /* ATR（波动率） */
  push('ATR', '波动率 ' + at.last.atr, '近 14 日平均真实波幅 ' + at.last.atr + '（用于设定止损距离）',
    '可用 1~2 倍 ATR 设浮动止损');

  /* WR */
  var wLast = last(w);
  push('WR', wLast, '威廉指标(10日) ' + wLast + '（>80 超卖、<20 超买）',
    wLast >= 80 ? '接近超卖' : (wLast <= 20 ? '接近超买' : '中性'));

  /* CCI */
  var cLast = last(cc);
  push('CCI', cLast, 'CCI(14) ' + cLast + '（>100 超买、<-100 超卖）',
    cLast >= 100 ? '强势超买区' : (cLast <= -100 ? '弱势超卖区' : '常态'));

  /* BIAS */
  push('BIAS', '6日 ' + last(b6) + '%', '乖离率 6日 ' + last(b6) + ' / 12日 ' + last(b12) + ' / 24日 ' + last(b24),
    Math.abs(last(b6)) > 5 ? '短线偏离较大' : '偏离正常');

  /* OBV 趋势（与收盘价方向） */
  var obvNow = last(o), obvPrev = (function () { for (var i = close.length - 2; i >= 0; i--) if (isNum(o[i])) return o[i]; return null; })();
  push('OBV', obvNow >= obvPrev ? '量能配合上行' : '量能走弱',
    'OBV 最新 ' + obvNow + '（能量潮跟随' + (obvNow >= obvPrev ? '价格上行' : '价格回落') + '）',
    obvNow >= obvPrev ? '量价配合' : '量价背离预警');

  /* ROC */
  push('ROC', last(rc) + '%', '12 日变动率 ' + last(rc) + '%', Math.abs(last(rc)) > 10 ? '短线动量强' : '动量温和');

  /* DMA */
  push('DMA', dm.last.ddd >= dm.last.ama ? '多头排列' : '空头排列',
    'DDD ' + dm.last.ddd + ' / AMA ' + dm.last.ama, dm.last.ddd >= dm.last.ama ? '中期偏多' : '中期偏空');

  /* 量能 */
  var vNow = last(vol), mv5l = last(mv5), mv10l = last(mv10);
  push('VOL', vNow > mv5l ? '放量' : '缩量',
    '今量 ' + vNow + ' / 5日均量 ' + mv5l + ' / 10日均量 ' + mv10l + '（VR26 ' + last(v) + '）',
    vNow > mv5l * 1.5 ? '明显放量' : (vNow < mv5l * 0.6 ? '明显缩量' : '量能平稳'));

  return {
    ok: true,
    raw: {
      macd: m, kdj: kd, rsi: { rsi6: rs6, rsi12: rs12, rsi24: rs24 }, boll: bl,
      atr: at, wr: w, cci: cc, bias: { b6: b6, b12: b12, b24: b24 }, obv: o,
      roc: rc, vr: v, dma: dm, trix: tx, mavol: { mv5: mv5, mv10: mv10 }
    },
    signals: sig,
    last: {
      macd: m.last, kdj: kd.last, rsi12: last(rs12), boll: bl.last, atr: at.last.atr,
      wr: wLast, cci: cLast, bias6: last(b6), obv: obvNow, roc: last(rc), dma: dm.last
    }
  };
}

module.exports = {
  isNum: isNum, r2: r2, sma: sma, ema: ema, hhving: hhving, llving: llving,
  macd: macd, kdj: kdj, rsi: rsi, boll: boll, atr: atr, wr: wr, cci: cci, bias: bias,
  obv: obv, roc: roc, vr: vr, dma: dma, trix: trix, mavol: mavol,
  computeIndicators: computeIndicators
};
