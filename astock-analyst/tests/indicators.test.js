'use strict';
/* 指标算法正确性：用已知算例验证公式（同花顺/通达信口径基础） */
var assert = require('assert');
var I = require('../lib/indicators');

var passed = 0, failed = 0;
function ok(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + ' — ' + e.message); }
}
function approx(a, b, tol) { return Math.abs(a - b) <= (tol || 1e-6); }

console.log('指标算法测试：');

ok('SMA 基础窗口', function () {
  var s = I.sma([1, 2, 3, 4, 5], 3);
  assert.strictEqual(s[0], null);
  assert.ok(approx(s[2], 2));        // (1+2+3)/3
  assert.ok(approx(s[4], 4));        // (3+4+5)/3
});

ok('EMA 递推初值取首值（N=3）', function () {
  var e = I.ema([1, 2, 3, 4, 5], 3);
  assert.ok(approx(e[0], 1));
  // e1=1; e2=2*0.5+1*0.5=1.5; e3=3*0.5+1.5*0.5=2.25; e4=4*.5+2.25*.5=3.125; e5=5*.5+3.125*.5=4.0625
  assert.ok(approx(e[1], 1.5, 1e-6));
  assert.ok(approx(e[2], 2.25, 1e-6));
  assert.ok(approx(e[4], 4.0625, 1e-6));
});

ok('EMA 跳过 null 不污染', function () {
  var e = I.ema([null, 10, 12, 14], 3);
  assert.strictEqual(e[0], null);
  assert.ok(approx(e[1], 10));
  assert.ok(approx(e[2], 12 * 0.5 + 10 * 0.5));
});

ok('RSI 单调上升序列 → 100', function () {
  var up = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20];
  var r = I.rsi(up, 6);
  assert.strictEqual(r[5], null);
  assert.strictEqual(r[r.length - 1], 100);
});

ok('RSI 单调下降序列 → 0', function () {
  var dn = [20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10];
  var r = I.rsi(dn, 6);
  assert.strictEqual(r[r.length - 1], 0);
});

ok('MACD 单调上升 → DIF>0 且柱为正', function () {
  var c = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30,
           31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50];
  var m = I.macd(c);
  assert.ok(m.last.dif > 0);
  assert.ok(m.last.bar > 0);
});

ok('BOLL 总体标准差口径', function () {
  // 序列 [1..5]，均值 3，总体方差 ((4+1+0+1+4)/5)=2，std=√2≈1.4142
  var b = I.boll([1, 2, 3, 4, 5], 5, 2);
  assert.ok(approx(b.mid[4], 3));
  assert.ok(approx(b.upper[4], 3 + 2 * Math.sqrt(2), 1e-3));
  assert.ok(approx(b.lower[4], 3 - 2 * Math.sqrt(2), 1e-3));
});

ok('KDJ 常数序列 → K=D=50（初值稳定）', function () {
  var flat = [10, 10, 10, 10, 10, 10, 10, 10, 10];
  var kd = I.kdj(flat, flat, flat, 9);
  // high=low=close 全相等 → RSV=50 → K=D 收敛到 50
  assert.ok(approx(kd.k[8], 50, 1e-6));
  assert.ok(approx(kd.d[8], 50, 1e-6));
});

ok('ATR 平坦序列 → 真实波幅为 0', function () {
  var h = [10, 10, 10], l = [10, 10, 10], c = [10, 10, 10];
  var a = I.atr(h, l, c, 3);
  assert.strictEqual(a.last.atr, 0);
});

ok('WR 范围 0~100', function () {
  var h = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20];
  var l = [5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5];
  var c = [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17];
  var w = I.wr(h, l, c, 10);
  assert.ok(w[9] >= 0 && w[9] <= 100);
});

ok('computeIndicators 短序列拒绝', function () {
  var r = I.computeIndicators({ close: [1, 2, 3], high: [1, 2, 3], low: [1, 2, 3], volume: [1, 2, 3] });
  assert.strictEqual(r.ok, false);
  assert.ok(r.reason);
});

ok('computeIndicators 返回 12 条信号且无 NaN', function () {
  var c = []; for (var i = 0; i < 120; i++) c.push(100 + Math.sin(i / 5) * 5 + i * 0.1);
  var k = { dates: c.map(function (_, i) { return 'd' + i; }), open: c, high: c.map(function (x) { return x + 1; }),
    low: c.map(function (x) { return x - 1; }), close: c, volume: c.map(function (x) { return 1000 + x; }) };
  var r = I.computeIndicators(k);
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.signals.length, 12);
  r.signals.forEach(function (s) {
    Object.keys(s).forEach(function (key) {
      var v = s[key];
      if (typeof v === 'number' && !isFinite(v)) throw new Error('NaN in ' + s.factor + '.' + key);
    });
  });
});

console.log('\n结果：' + passed + ' 通过 / ' + failed + ' 失败');
process.exit(failed ? 1 : 0);
