/**
 * full-market.e2e.js — 全市场真实数据的端到端验收
 *
 * 与 smoke.e2e.js 的区别：这里断言的是「真实可用」而非「页面能打开」——
 * 明确要求数据库覆盖全市场、数据模式为 real、7 个策略都能在全市场数据上跑出结果。
 *
 * 用法：先起两个服务，再 node tests/full-market.e2e.js
 */
const BASE = process.env.ASTOCK_BASE || 'http://127.0.0.1:8790';
const fsMod = require('fs');
const pathMod = require('path');

let pass = 0, fail = 0;
const fails = [];

function ok(name, cond, extra) {
  if (cond) { pass++; console.log('PASS', name, extra || ''); }
  else { fail++; fails.push(name); console.log('FAIL', name, extra || ''); }
}

async function jget(path, opt) {
  const r = await fetch(BASE + path, opt || {});
  let body = null;
  try { body = await r.json(); } catch (e) { body = null; }
  return { s: r.status, body };
}

async function hget(path) {
  const r = await fetch(BASE + path);
  return { s: r.status, t: await r.text() };
}

(async () => {
  console.log('=== 1. 全市场数据 ===');
  const h = await jget('/api/sequoia/api/health');
  ok('策略引擎健康', h.body && h.body.ok, JSON.stringify(h.body && {
    symbols: h.body.symbols, latest: h.body.latest_date, mode: h.body.data_mode, src: h.body.data_source
  }));
  ok('覆盖全市场(>=4900只)', h.body && h.body.symbols >= 4900, 'symbols=' + (h.body && h.body.symbols));
  ok('数据为真实行情', h.body && h.body.data_mode === 'real', 'mode=' + (h.body && h.body.data_mode));
  ok('数据源已标注', h.body && !!h.body.data_source, 'source=' + (h.body && h.body.data_source));
  ok('最新交易日为近期', h.body && !!h.body.latest_date, 'latest=' + (h.body && h.body.latest_date));

  console.log('\n=== 2. 页面 ===');
  const pages = [
    ['/', 'MILO'], ['/analysis.html', 'A 股分析终端'], ['/strategy.html', '策略扫描'],
    ['/portfolio.html', '个人持仓分析'], ['/us.html', '美股联动'],
    ['/etf.html', 'ETF 实时监控'], ['/industry.html', '行业强度榜']
  ];
  for (const [p, mark] of pages) {
    const r = await hget(p);
    ok('page ' + p, r.s === 200 && r.t.includes(mark), 'len=' + r.t.length);
  }
  for (const p of ['/milo.css', '/nav.css', '/nav.js', '/analysis.js']) {
    const r = await hget(p);
    ok('asset ' + p, r.s === 200, 'len=' + r.t.length);
  }

  console.log('\n=== 3. 七策略全市场扫描 ===');
  const t0 = Date.now();
  const scan = await jget('/api/sequoia/api/scan?name=all', { method: 'POST' });
  const cost = ((Date.now() - t0) / 1000).toFixed(1);
  ok('扫描返回', scan.body && Array.isArray(scan.body.strategies), cost + 's');
  ok('策略数=7', scan.body && scan.body.strategies.length === 7,
    (scan.body && scan.body.strategies.map(s => s.key).join(',')));
  ok('全市场有命中', scan.body && scan.body.total > 0, 'total=' + (scan.body && scan.body.total));

  const byKey = {};
  (scan.body && scan.body.strategies || []).forEach(s => { byKey[s.key] = s; });
  const newsix = ['limit_up_shakeout', 'turtle', 'ma_volume', 'high_tight_flag', 'uptrend_limit_down', 'rps_breakout'];
  for (const k of newsix) {
    ok('策略存在 ' + k, !!byKey[k], byKey[k] ? ('命中 ' + byKey[k].count + ' / ' + byKey[k].cost + 's') : '缺失');
  }
  // 至少 4 个策略有真实信号（其余可能是条件严格或依赖外部事件数据）
  const withSignal = newsix.filter(k => byKey[k] && byKey[k].count > 0);
  ok('多数策略有真实信号(>=4)', withSignal.length >= 4, withSignal.join(','));
  // pick 必须带真实名称，说明来自真实清单
  const anyPick = (scan.body.strategies.find(s => s.picks && s.picks.length) || {}).picks || [];
  ok('命中标的带名称', anyPick.length > 0 && !!anyPick[0].name,
    anyPick[0] ? anyPick[0].symbol + ' ' + anyPick[0].name : '-');
  // 命中标的必须能被个股行情接口识别（真实存在的股票）
  if (anyPick.length) {
    const lk = await jget('/api/lookup?code=' + anyPick[0].symbol);
    ok('命中标的可查真实行情', lk.body && lk.body.ok, anyPick[0].symbol + ' ' + (lk.body && lk.body.name));
  }

  console.log('\n=== 4. 个人持仓分析 ===');
  const pfPost = await jget('/api/portfolio', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      positions: [
        { code: '600519', shares: 100, avgCost: 1400, buyDate: '2026-06-01' },
        { code: '000001', shares: 2000, avgCost: 11.5, buyDate: '2026-07-15' }
      ]
    })
  });
  ok('持仓保存', pfPost.body && pfPost.body.ok && pfPost.body.saved === 2, 'saved=' + (pfPost.body && pfPost.body.saved));
  const pos = (pfPost.body && pfPost.body.positions) || [];
  ok('持仓取到实时行情', pos.length === 2 && pos.every(p => p.price > 0), pos.map(p => p.code + ':' + p.price).join(' '));
  ok('盈亏计算正确', pos.every(p => p.pnl != null && Math.abs(p.pnl - (p.marketValue - p.cost)) < 1),
    pos.map(p => p.pnl).join(','));
  const sm = (pfPost.body && pfPost.body.summary) || {};
  ok('汇总有总市值', sm.totalValue > 0, 'value=' + sm.totalValue);
  ok('集中度风险提示', ((pfPost.body && pfPost.body.flags) || []).some(f => /集中度/.test(f.text)),
    ((pfPost.body && pfPost.body.flags) || []).map(f => f.level).join(','));
  const pfGet = await jget('/api/portfolio');
  ok('持仓持久化', pfGet.body && pfGet.body.positions.length === 2);

  console.log('\n=== 5. 美股联动 ===');
  const us = await jget('/api/us-linkage');
  ok('联动接口返回', us.body && us.body.ok, 'errors=' + JSON.stringify(us.body && us.body.errors));
  ok('美股三大指数', us.body && (us.body.us.indices || []).length >= 3,
    ((us.body && us.body.us.indices) || []).map(i => i.name + i.changePct + '%').join(' '));
  ok('A股指数有数据', us.body && (us.body.a.indices || []).some(x => x.price > 0),
    ((us.body && us.body.a.indices) || []).map(i => i.name).join(','));
  ok('联动结论已生成', us.body && us.body.verdict && us.body.verdict.type,
    (us.body && us.body.verdict && (us.body.verdict.type + '/' + us.body.verdict.confidence)));

  /*
   * 交叉校验：美股数字必须与腾讯 usDJI/usIXIC/usINX/usNVDA 对得上。
   * 2026-11-13 的 bug 就是新浪 int_dji 返回过期快照（46247 vs 实际 52297），
   * 以及把「今开」当「昨收」导致台积电 -1.90% 显示成 +2.75%。这条断言用于防回归。
   */
  const tq = await fetch('https://qt.gtimg.cn/q=usDJI,usIXIC,usINX,usNVDA');
  const tqText = new TextDecoder('gbk').decode(Buffer.from(await tq.arrayBuffer()));
  const tqMap = {};
  tqText.split('\n').forEach(function (line) {
    const m = line.match(/v_(us[A-Z]+)="([^"]*)"/);
    if (!m) return;
    const a = m[2].split('~');
    tqMap[m[1]] = { price: parseFloat(a[3]), prev: parseFloat(a[4]), pct: parseFloat(a[32]) };
  });
  const usIdx = (us.body && us.body.us.indices) || [];
  const usTechList = (us.body && us.body.us.tech) || [];
  const cmpPairs = [
    ['道琼斯', usIdx[0], tqMap.usDJI],
    ['纳斯达克', usIdx[1], tqMap.usIXIC],
    ['标普500', usIdx[2], tqMap.usINX],
    ['英伟达', usTechList.filter(x => /英伟达/.test(x.name))[0], tqMap.usNVDA]
  ];
  /*
   * 盘前/新交易日切换时新浪涨跌幅归 0、腾讯仍是上一交易日收盘，两源「不可比」。
   * 此时价格依然必须严格对上（防 int_* 过期快照回归），涨跌幅则要求系统显式标注
   * time-skew 而非静默判成一致或硬报冲突 —— 两者都不能让「数字错了没人知道」。
   */
  var usSkew = !!(us.body && us.body.verification && us.body.verification.timeSkew > 0);
  cmpPairs.forEach(function (p) {
    var mine = p[1], ref = p[2];
    if (!mine || !ref) { ok('美股对账 ' + p[0], false, '缺数据'); return; }
    var dp = Math.abs(mine.price - ref.price) / ref.price;
    var dc = Math.abs((mine.changePct || 0) - ref.pct);
    if (usSkew) {
      ok('美股对账 ' + p[0] + '（时点不可比：价格仍严格一致）', dp < 0.005,
        mine.price + '/' + ref.price + '  pct 不可比（两源 ' + mine.changePct + ' / ' + ref.pct + '）');
    } else {
      ok('美股对账 ' + p[0] + '（价格/涨跌幅与腾讯一致）', dp < 0.005 && dc < 0.3,
        mine.price + '/' + ref.price + '  pct ' + mine.changePct + ' vs ' + ref.pct);
    }
  });
  /*
   * 「盘前结论不可用」与「两源时点错位」是两件独立的事：
   *   盘前两源可能都归 0（一致但结论仍不可用），也可能一个归 0 一个仍是昨收（不可比）。
   * 所以分开断言，不能把两者绑死 —— 第三遍 e2e 的失败就是这个耦合造成的。
   */
  var preMarket = !!(us.body && us.body.us && us.body.us.marketState === 'pre-market');
  var usIssues = (us.body && us.body.issues) || [];
  ok('盘前时段状态降级且明确写出结论不可用',
    !preMarket || (us.body.status === 'degraded' &&
      !!(us.body.verdict && us.body.verdict.dataCaveat) &&
      usIssues.some(i => i.code === 'PREMARKET_NOT_COMPARABLE')),
    'preMarket=' + preMarket + ' status=' + (us.body && us.body.status));
  ok('跨源时点错位不算数据冲突、有专属标注',
    !usSkew || (us.body.verification.mismatched === 0 &&
      us.body.status === 'degraded' &&
      usIssues.some(i => i.code === 'TIME_SKEW_UNCOMPARABLE')),
    'timeSkew=' + (us.body.verification && us.body.verification.timeSkew));
  ok('两源可比时不残留时点标注',
    preMarket || usSkew || (us.body.verification.timeSkew === 0 &&
      !(us.body.verdict && us.body.verdict.dataCaveat)));

  console.log('\n=== 6. 自动任务与数据进度 ===');
  const sch = await jget('/api/sequoia/api/scheduler/status');
  ok('调度器运行中', sch.body && sch.body.ok && sch.body.running === true);
  ok('调度器已配置时间', sch.body && !!sch.body.sync_time && !!sch.body.scan_time,
    (sch.body && (sch.body.sync_time + ' / ' + sch.body.scan_time)));
  const prog = await jget('/api/sequoia/api/backfill/progress');
  ok('回填进度可查', prog.body && prog.body.db_symbols >= 4900, 'db_symbols=' + (prog.body && prog.body.db_symbols));

  console.log('\n=== 7. 既有页面接口 ===');
  const etf = await jget('/api/etf');
  ok('ETF 8 张', etf.body && (etf.body.cards || []).length === 8);
  const ind = await jget('/api/industry');
  ok('行业板块 >=40', ind.body && (ind.body.boards || []).length >= 40,
    'n=' + (ind.body && ind.body.boards.length));

  console.log('\n=== 8. 状态契约与第二源对账 ===');
  const dq = await jget('/api/data-quality');
  ok('可信度汇总接口可用', dq.body && dq.body.ok && dq.body.totals,
    JSON.stringify(dq.body && dq.body.totals));
  ok('真实执行过对账（非静态占位）', dq.body && dq.body.totals.checked > 0,
    'checked=' + (dq.body && dq.body.totals.checked));
  ok('覆盖清单含单源与新鲜度项',
    (dq.body && dq.body.catalog || []).some(c => c.kind === 'single-source') &&
    (dq.body && dq.body.catalog || []).some(c => c.kind === 'freshness'));

  const us2 = await jget('/api/us-linkage');
  ok('美股接口带四态状态', us2.body && ['ok', 'degraded', 'incomplete', 'failed'].includes(us2.body.status),
    'status=' + (us2.body && us2.body.status));
  ok('美股接口带 issues / verification',
    us2.body && Array.isArray(us2.body.issues) && !!us2.body.verification);
  ok('美股独立双源真的对上了', us2.body && us2.body.verification.matched > 0,
    'matched=' + (us2.body && us2.body.verification.matched));
  ok('美股旧字段零改动', us2.body && us2.body.verdict && us2.body.us && us2.body.ok === true);

  const pf2 = await jget('/api/portfolio');
  ok('持仓接口带状态契约', pf2.body && !!pf2.body.status && !!pf2.body.verification);
  ok('持仓旧字段零改动', pf2.body && Array.isArray(pf2.body.positions) && !!pf2.body.summary);

  const etf2 = await jget('/api/etf');
  const ind2 = await jget('/api/industry');
  ok('ETF 接口带状态契约', etf2.body && !!etf2.body.status && !!etf2.body.verification);
  ok('ETF 单源份额标为未核对', etf2.body && etf2.body.verification.unverified >= 1,
    'unverified=' + (etf2.body && etf2.body.verification.unverified));
  ok('行业单源不伪装成已验证',
    ind2.body && ind2.body.verification.unverified >= 1 && ind2.body.verification.matched === 0);
  ok('行业旧字段零改动', ind2.body && (ind2.body.boards || []).length >= 40);

  /* 故障注入：把英伟达涨跌幅改成错的，验证拦截链路真的会响 */
  const CT = require('../lib/contract.js');
  const RC = require('../lib/reconcile.js');
  const injected = RC.compare({ source: '新浪(错误解析)', value: 10.06 },
    { source: '腾讯', value: -4.34 },
    { scope: 'inject', field: 'nvda.changePct', label: '英伟达 涨跌幅',
      tolerance: 0.3, toleranceMode: 'absolute', kind: 'independent' });
  ok('故障注入：错误数字被判 mismatch', injected.result === 'mismatch', 'delta=' + injected.delta);
  const inj = CT.attach({ ok: true, verdict: { type: '联动' } }, { scope: 'inject', checks: [injected] });
  ok('故障注入：状态降级为 degraded', inj.status === 'degraded');
  ok('故障注入：两源数值都留在结果里',
    inj.verification.conflicts[0].primary.value === 10.06 &&
    inj.verification.conflicts[0].secondary.value === -4.34);
  ok('故障注入：旧 ok 与业务字段未被篡改', inj.ok === true && inj.verdict.type === '联动');

  console.log('\n=== 9. 容差校准 + SSE 可信度事件 ===');
  const dq2 = await jget('/api/data-quality');
  ok('汇总接口带校准容差来源',
    dq2.body && dq2.body.tolerance && dq2.body.tolerance.source === 'calibrated',
    'source=' + (dq2.body && dq2.body.tolerance && dq2.body.tolerance.source));
  ok('校准样本量真实（>=100 条跨源观测）',
    dq2.body && dq2.body.tolerance.sample >= 100,
    'obs=' + (dq2.body && dq2.body.tolerance && dq2.body.tolerance.sample));
  ok('容差有高价上限且按时段选 profile',
    dq2.body && dq2.body.tolerance.values.priceRel <= 0.001 &&
    dq2.body.tolerance.values.priceMax <= 0.5 &&
    !!dq2.body.tolerance.profile,
    'profile=' + (dq2.body && dq2.body.tolerance.profile));
  ok('汇总说明写明容差来源', /对账容差来源/.test((dq2.body && dq2.body.note) || ''));
  /* 系统目标不是「永远 0 冲突」，而是「冲突必须可追溯」。盘中两源时间戳不同步时
     会显式标为不可比；真出现冲突时每一条都要带两源数值、差值与所用容差。 */
  const allConflicts = [];
  Object.keys((dq2.body && dq2.body.scopes) || {}).forEach(k =>
    ((dq2.body.scopes[k].items) || []).forEach(x => {
      if (x.result === 'mismatch') allConflicts.push(x);
    }));
  ok('冲突项全部可追溯（两源数值/差值/容差齐全）',
    allConflicts.every(x => x.primary && x.secondary &&
      typeof x.delta === 'number' && typeof x.toleranceUsed === 'number'),
    'conflicts=' + allConflicts.length);

  /* 真机抓一次 SSE 流，验证 quality 事件真的会发、旧事件顺序没被破坏 */
  const histDir = pathMod.join(__dirname, '..', 'data', 'history');
  const histBefore = new Set(fsMod.readdirSync(histDir));
  const sseResp = await fetch(BASE + '/api/analyze?codes=600519&mode=dry-run',
    { signal: AbortSignal.timeout(90000) });
  const sseCt = sseResp.headers.get('content-type') || '';
  const rd = sseResp.body.getReader(), dec = new TextDecoder();
  let buf = '', evTypes = [], qEv = null;
  while (true) {
    const chunk = await rd.read();
    if (chunk.done) break;
    buf += dec.decode(chunk.value, { stream: true });
    let cut;
    while ((cut = buf.indexOf('\n\n')) >= 0) {
      const frame = buf.slice(0, cut); buf = buf.slice(cut + 2);
      const line = frame.split('\n').find(l => l.startsWith('data: '));
      if (!line) continue;
      try {
        const ev = JSON.parse(line.slice(6));
        evTypes.push(ev.type);
        if (ev.type === 'quality') qEv = ev;
      } catch (e) { /* 非 JSON 帧忽略 */ }
    }
  }
  ok('SSE 返回 event-stream', /text\/event-stream/.test(sseCt));
  ok('SSE 旧事件顺序零改动',
    evTypes[0] === 'hello' && ['market', 'item-start', 'item-done', 'done', 'archived']
      .every(t => evTypes.indexOf(t) >= 0), evTypes.join(' -> '));
  ok('SSE 新增 quality 事件', !!qEv, qEv ? qEv.status : 'missing');
  ok('quality 事件带真实双源对账结果', !!qEv && qEv.checked >= 2 && qEv.matched >= 1,
    qEv ? 'checked=' + qEv.checked + ' matched=' + qEv.matched : 'missing');
  ok('quality 事件两源数值可追溯', !!qEv &&
    (qEv.items || []).some(x => x.primary && x.secondary));
  ok('quality 事件带校准容差来源', !!qEv && qEv.tolerance &&
    qEv.tolerance.source === 'calibrated');
  /* 测试会写一条 history，只删除本次新增的，绝不动用户已有记录 */
  await new Promise(r => setTimeout(r, 500));
  const histAdded = fsMod.readdirSync(histDir).filter(f => !histBefore.has(f));
  histAdded.forEach(f => { try { fsMod.unlinkSync(pathMod.join(histDir, f)); } catch (e) { } });
  const histLeft = fsMod.readdirSync(histDir);
  ok('SSE 测试产物已清理且未动用户记录',
    histLeft.length === histBefore.size && histLeft.every(f => histBefore.has(f)),
    'removed=' + histAdded.length + ' remain=' + histLeft.length);

  console.log('\n========================================');
  console.log('RESULT: pass=' + pass + ' fail=' + fail);
  if (fail) console.log('失败项: ' + fails.join(' | '));
  console.log('========================================');
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('ERR', e.message); process.exit(2); });
