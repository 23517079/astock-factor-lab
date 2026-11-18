// tests/smoke.e2e.js — astock-analyst 端到端冒烟测试
// 前置：Node(8790) 与 Python 伴侣(8791) 均已启动。
// 用法：node tests/smoke.e2e.js   （可用 ASTOCK_BASE 覆盖前端地址）
const base = process.env.ASTOCK_BASE || 'http://127.0.0.1:8790';
let pass = 0, fail = 0;
function ok(name, cond, extra) { if (cond) { pass++; console.log('PASS', name, extra || ''); } else { fail++; console.log('FAIL', name, extra || ''); } }
async function jget(p, opt) { const r = await fetch(base + p, opt || {}); let body = null; try { body = await r.json(); } catch (e) {} return { s: r.status, body }; }
async function hget(p) { const r = await fetch(base + p); const t = await r.text(); return { s: r.status, t }; }
(async () => {
  // 页面可达 + 关键标记
  for (const [p, mark] of [['/', 'MILO'], ['/strategy.html', '策略扫描'], ['/etf.html', 'ETF 实时监控'], ['/industry.html', '行业强度榜'], ['/analysis.html?code=600519', 'A 股分析终端']]) {
    const r = await hget(p); ok('page ' + p, r.s === 200 && r.t.includes(mark), 'len=' + r.t.length);
  }
  // 静态资源
  for (const p of ['/milo.css', '/nav.js', '/analysis.js']) { const r = await hget(p); ok('asset ' + p, r.s === 200, 'len=' + r.t.length); }
  // 策略引擎健康
  const h = await jget('/api/sequoia/api/health'); ok('sequoia health', h.body && h.body.ok, 'symbols=' + (h.body && h.body.symbols));
  // 行业
  const ind = await jget('/api/industry'); const b0 = (ind.body && ind.body.boards || [])[0];
  ok('industry>=40 boards', !!(ind.body && ind.body.ok && ind.body.boards.length >= 40), 'n=' + (ind.body && ind.body.boards.length) + ' top=' + (b0 && b0.name));
  ok('industry lead fields', !!(b0 && b0.leadCode && b0.leadName && typeof b0.strength === 'number'));
  // ETF
  const etf = await jget('/api/etf'); ok('etf 8 cards', !!(etf.body && etf.body.cards && etf.body.cards.length === 8), 'codes=' + (etf.body && etf.body.cards.map(c => c.code).join(',')));
  // 策略元信息
  const st = await jget('/api/sequoia/api/strategies'); ok('strategies 7', Array.isArray(st.body) && st.body.length === 7);
  // 全市场扫描
  const scan = await jget('/api/sequoia/api/scan?name=all', { method: 'POST' });
  ok('scan total>0', !!(scan.body && scan.body.total > 0), 'total=' + (scan.body && scan.body.total));
  const s0 = (scan.body && scan.body.strategies || []).find(s => s.count > 0);
  ok('scan pick shape', !!(s0 && s0.picks[0] && s0.picks[0].symbol && 'last' in s0.picks[0] && 'pct' in s0.picks[0]));
  // 回测结果
  const bt = await jget('/api/sequoia/api/backtest/results');
  ok('backtest results', ((bt.body && bt.body.strategies) || []).some(s => s.samples > 0 && s.win_rate != null));
  console.log('\n== RESULT: pass=' + pass + ' fail=' + fail + ' ==');
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('ERR', e.message); process.exit(2); });
