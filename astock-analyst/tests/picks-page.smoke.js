// picks 页面运行时冒烟测试：DOM 桩执行页面脚本，验证渲染 + 点击展开 + 结论横幅。
// 重点验证两件事：
//   1. 证据不充分时，横幅必须显示警示（不能默默把「偏弱」当通过）
//   2. 点某只股票，详情必须展开（避免重演 factors 页「点了没反应」）
//
// 2026-11-16（T05）改动：
//   1. 载入 public/freshness.js（与 factors 页共用），沙箱提供 window。
//   2. 生产 factor_score.json 目前还没有 freshness / rolling（要等 T03 产线 24/24），
//      所以这里注入 fixture —— 「字段齐全时渲染成什么样」必须被测到。
//   3. 新增 6 项：4 态徽标 / 横幅位置不在 caveatList / 状态行字段逐字 /
//      ?v=recent 回落且告知 / 逐期表默认 5 行可展开 / skippedFactors 带 since。
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'public', 'picks.html'), 'utf8');
const m = html.match(/<script(?![^>]*src=)[^>]*>([\s\S]*?)<\/script>/);
if (!m) { console.error('FAIL 找不到内联脚本'); process.exit(1); }
const code = m[1];
const freshCode = fs.readFileSync(path.join(__dirname, '..', 'public', 'freshness.js'), 'utf8');

const api = JSON.parse(fs.readFileSync(
  path.join(__dirname, '..', '..', 'sequoia-service', 'data', 'factor_score.json'), 'utf8'));
api.ok = true;

/* ---- fixture：freshness（stale）+ rolling（8 期，1 个 suspect 因子）---- */
const FRESH_FIXTURE = {
  ok: true, status: 'stale', statusLabel: '数据已过期',
  reason: '已过期 2 个交易日',
  dataThrough: '2026-11-11', dataThroughWeekday: '周三',
  expectedThrough: '2026-11-13', lagTradingDays: 2,
  lastCalcAt: '2026-11-16T16:22:11', lastCalcOk: true, lastCalcError: null,
  nextRunAt: '2026-11-17 16:10', factorTime: '16:10',
  partial: false, warnings: [], runId: '20261116T161012', cost: 719.4,
  cacheVerify: { reconcile: { state: 'ok' } }
};
const PERIOD_DAYS = ['2026-09-16', '2026-09-30', '2026-10-14', '2026-10-21',
  '2026-10-28', '2026-11-04', '2026-11-07', '2026-11-11'];
/* 原始 IC（未乘 direction）与有效 IC（icMean × direction）**故意设成不同符号**：
   页面必须用 Adj 那一列。原始值恒定 -0.0481 便于断言「页面没读它」。 */
const PERIOD_ICS = [0.020, 0.035, -0.010, 0.045, 0.012, 0.038, -0.004, 0.041];
const PERIOD_ICS_ADJ = [0.021, 0.036, -0.008, 0.046, 0.013, 0.039, -0.002, 0.042];
const PERIODS = PERIOD_DAYS.map((d, i) => ({
  asOf: d, icCutoff: '2026-09-09', n_factors: 23, n_considered: 24, n_evaluable: 23,
  n_selected: 5 + (i % 3),
  selected: ['mom60', 'turn_cv20', 'corr_pv20'], avgIc: -0.030,
  avgIcSelected: -0.0481, avgIcSelectedAdj: PERIOD_ICS_ADJ[i],
  factors: []
}));
const ROLL_FIXTURE = {
  generatedAt: '2026-11-16T16:11:00',
  params: { horizon: 5, minStocks: 100, nGroups: 10, nwLag: 4, rollStep: 20, rollSelectWindow: 120 },
  latest: PERIODS[PERIODS.length - 1],
  summary: {
    nPeriods: 8, nUsablePeriods: 8,
    avgSelectedIc: -0.0481, avgSelectedIcAdj: 0.0312,
    avgSelectedIcRecent5: -0.0481, avgSelectedIcAdjRecent5: 0.028,
    avgSelectedNum: 6.1, nSuspectFactors: 1, horizon: 5, rollStep: 20, rollSelectWindow: 120
  },
  skippedFactors: [{ factor: 'mom60', reason: 'suspect', since: '2026-11-16' }],
  periods: PERIODS
};
api.freshness = FRESH_FIXTURE;
api.rolling = ROLL_FIXTURE;
api.evalAsOf = '2026-11-10';

function el(id) {
  return {
    id, _html: '', textContent: '', value: '', className: '', dataset: {},
    style: {},
    classList: { add() {}, remove() {} },
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html; },
    querySelectorAll() { return []; },
    appendChild() {}
  };
}

const nodes = {};
['pkStatus', 'pkCaveat', 'pkVerdict', 'caveatList', 'pkKpis', 'picksBody',
 'pickDetail', 'methodBox', 'pkSwitch', 'pkSwitchNotice', 'rollBox', 'rollCard',
 'pkFreshBanner', 'recentBox'].forEach(k => { nodes[k] = el(k); });

/* tbody 桩：按 data-sym 缓存对象，保证 onclick 不会在重渲染后丢失 */
const CLICKERS = [];
const ROWCACHE = new Map();
function rowsOf(html) {
  const out = [];
  const re = /<tr class="row-main[^"]*" data-sym="([^"]+)"/g;
  let mm;
  while ((mm = re.exec(html))) out.push(mm[1]);
  return out;
}
nodes.picksBody.querySelectorAll = function (sel) {
  if (sel !== 'tr.row-main') return [];
  CLICKERS.length = 0;
  return rowsOf(this._html).map(sym => {
    let obj = ROWCACHE.get(sym);
    if (!obj) {
      obj = { dataset: { sym }, _onclick: null };
      Object.defineProperty(obj, 'onclick', {
        get() { return this._onclick; },
        set(fn) { this._onclick = fn; }
      });
      ROWCACHE.set(sym, obj);
    }
    CLICKERS.push(obj);
    return obj;
  });
};

const document = {
  getElementById: id => nodes[id] || el(id),
  querySelectorAll: () => [],
  querySelector: () => null,
  createElement: () => el('tmp'),
  body: { insertBefore() {}, firstChild: null }
};

const sandbox = {
  document, console,
  fetch: () => Promise.resolve({ json: () => Promise.resolve(api) }),
  MiloNav: { mount: () => {} },
  /* 用真实的老书签参数打开：?v=recent 必须回落主版并明确告知 */
  location: { hash: '', search: '?v=recent' },
  window: { innerHeight: 800, pageYOffset: 0, scrollTo() {}, location: { search: '?v=recent' } },
  setTimeout, clearTimeout,
  Math, JSON, Number, String, Object, Array, isNaN, URLSearchParams
};
vm.createContext(sandbox);
vm.runInContext(freshCode, sandbox, { filename: 'freshness.js' });
vm.runInContext(code, sandbox, { filename: 'picks.inline.js' });

let fail = 0;
function ok(cond, label, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + label + (extra ? '  ' + extra : ''));
  if (!cond) fail++;
}
const strip = s => String(s || '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();

setTimeout(() => {
  const tb = nodes.picksBody.innerHTML;
  const kpi = nodes.pkKpis.innerHTML;
  const method = nodes.methodBox.innerHTML;
  const verdict = nodes.pkVerdict.textContent;
  const caveat = nodes.caveatList.innerHTML;
  const status = nodes.pkStatus.innerHTML;
  const banner = nodes.pkFreshBanner.innerHTML;
  const sw = nodes.pkSwitch.innerHTML;
  const notice = nodes.pkSwitchNotice.innerHTML;
  const roll = nodes.rollBox.innerHTML;
  const fresh = sandbox.window.fkFresh;

  /* 名单条数由 --top 决定、名字每天会变，写死只会在换参数/换数据后假失败。
     真正要守住的是「渲染出了带中文名和 6 位代码的行」，以及行数与数据一致。 */
  const n = (tb.match(/<tr class="row-main/g) || []).length;
  ok(n > 0, '推荐表渲染出股票', '行数 ' + n);
  ok(/<div class="nm">[^<]+<\/div>/.test(tb), '每行含股票中文名');
  ok(/<div class="lb">\d{6}<\/div>/.test(tb), '每行含 6 位股票代码');
  ok(tb.includes('卖出条件'), '每行含卖出条件');
  ok(!tb.includes('undefined'), '表格无 undefined');
  ok(kpi.includes('综合分 IC'), 'KPI 有 IC 卡片');
  ok(kpi.includes('偏弱') || kpi.includes('通过') || kpi.includes('反号'), 'KPI 显示前后段检验结果');

  /* 结论横幅：weak 必须给警示，不能沉默 */
  ok(verdict.length > 0, '结论横幅有文案', verdict.slice(0, 40));
  if (api.oos && api.oos.verdict !== 'pass') {
    ok(/⚠/.test(verdict), '证据不足时横幅带警示符号', verdict.slice(0, 50));
    ok(/别照着买|不可用/.test(verdict), '证据不足时明确劝阻', verdict.slice(0, 50));
  } else {
    ok(true, '（本次数据判定为通过，跳过警示断言）');
    ok(true, '（同上）');
  }
  ok(caveat.includes('退市股'), '警示清单含幸存者偏差');
  ok(caveat.includes('交易成本') || caveat.includes('滑点'), '警示清单含成本未扣');
  /* T05：「全部因子」也有轻度前视，必须写在警示里（原来只写给了已停用那一版） */
  ok(caveat.includes('全样本 t 值') && caveat.includes('前视'),
     '「全部因子」组合也标注了轻度前视', strip(caveat).slice(0, 70));

  /* 方法表：必须用中文名，不能只给代号 */
  ok(method.includes('成交额变异系数') || method.includes('量价相关'), '方法表显示因子中文名');
  ok(method.includes('turn_cv20'), '方法表同时保留代号便于对照');
  ok(method.includes('权重'), '方法表含权重列');

  /* ---------- T05-1：新鲜度（两页共用 freshness.js）---------- */
  ok(!!fresh, 'public/freshness.js 已加载并挂到 window.fkFresh');
  const four = ['fresh', 'pending', 'stale', 'failed'].map(st =>
    fresh.badgeText({ status: st, lagTradingDays: st === 'stale' ? 2 : 0 }));
  ok(four.join('|') === '最新|今日未更新|已过期 2 个交易日|更新失败',
     '4 态徽标文案正确', four.join(' / '));
  ok(/fk-fbadge is-stale/.test(status), '状态行渲染出 stale 徽标');
  ok(strip(status).includes('数据截止 2026-11-11（周三） · 上次计算 11-16 16:22 · 下次更新 11-17 16:10'),
     '三段式与接口字段逐字相同', strip(status).slice(0, 90));
  ok(banner.includes('fk-fbanner') && banner.includes('数据不是最新的'),
     '过期时在表格上方插入独立横幅', strip(banner).slice(0, 60));
  ok(!strip(caveat).includes('数据不是最新的'),
     '横幅不混进 caveatList（那是因子语义的边界，另一件事）');

  /* ---------- T05-2：?v=recent 已停用 → 告知 + 回落主版 ---------- */
  ok(notice.includes('已停用') && notice.includes('前视'),
     '?v=recent 打开时明确告知「该版本已停用」');
  ok(!sw.includes('仅近期有效'), '切换条不再出现「仅近期有效」这一档', strip(sw).slice(0, 60));
  ok(/class="ps active" data-v="main"/.test(sw), '回落主版：main 标签为 active');
  ok(n > 0 && tb.includes('卖出条件'), '回落之后名单照常渲染（不是空白页）');

  /* ---------- T05-2b：滚动验证入口不得假装是 tab ----------
     它不切换名单，拿到 active 就是在声称一个没发生的状态变化：
     用户看到高亮、名单没变，只会以为卡住了。 */
  ok(!/ps active[^>]*data-v="roll"/.test(sw), '滚动验证入口**永不**进入 active');
  ok(/href="#rollCard"/.test(sw) && /role="link"/.test(sw) && sw.includes('↓'),
     '滚动验证入口是锚点链接（href=#rollCard + role=link + ↓）');
  ok(/title="跳到下方「滚动验证」卡片（名单不切换）"/.test(sw) && /aria-label=/.test(sw),
     '入口带 title 与 aria-label，写明「名单不切换」');
  (function () {
    let threw = null;
    try {
      nodes.pkSwitch.onclick({ target: { dataset: { v: 'roll' }, parentNode: nodes.pkSwitch } });
    } catch (e) { threw = e.message; }
    ok(!threw && !/ps active[^>]*data-v="roll"/.test(nodes.pkSwitch.innerHTML),
       '点入口不抛异常、不把自己变成 active', threw || '');
  })();

  /* ---------- T05-3：滚动验证卡片 ---------- */
  ok(strip(sw).includes('滚动验证') && strip(sw).includes('8 期')
     && strip(sw).includes('平均有效 IC'),
     '切换条第二标签换成「滚动验证 · 逐期重选（N 期 · 平均有效 IC）」', strip(sw).slice(0, 90));
  const preview = roll.split('<details')[0];
  const nPreview = (preview.match(/<tr><td>/g) || []).length;
  ok(nPreview === 5, '逐期表默认只显示 5 期', '实际 ' + nPreview + ' 行');
  ok(/<details class="fk-rtable"/.test(roll) && !/<details class="fk-rtable"[^>]*open/.test(roll),
     '逐期表默认折叠（<details> 无 open）');
  ok(roll.includes('展开全部 8 期'), '折叠条的文案写明共有多少期');
  ok(strip(roll).includes('8 期') && strip(roll).includes('N-W t')
     && strip(roll).includes('75%'),
     '卡片含 期数 / N-W t / IC>0 占比', strip(roll).slice(0, 120));
  /* 平均 IC 必须是「按方向归正」的那一列：raw=-0.0481 与 adj=+0.0312 符号相反，
     页面若读了 raw，就会显示「-0.0481」并让用户以为这套组合在反向预测。 */
  ok(strip(roll).includes('平均有效 IC') && strip(roll).includes('+0.0312'),
     '平均 IC 用的是 avgSelectedIcAdj（有效 IC）', strip(roll).slice(0, 80));
  ok(!strip(roll).includes('-0.0481'),
     '页面**不读**原始 IC（raw=-0.0481 一个字符都不出现）');
  ok(strip(roll).includes('已按因子方向归正'), '卡片注明「已按因子方向归正」');
  ok(strip(roll).includes('23/24'),
     '逐期表带「可评估/考虑」因子数（早期期两者不等是正常的）');
  ok(roll.includes('不是另一份推荐股名单'),
     '卡片明说滚动验证不是另一份名单（避免用户以为换了版本）');
  ok(roll.includes('上方名单不变'), '卡片底部的说明补了「上方名单不变」');
  /* skippedFactors：必须带 since，否则「以前那些期跳过了吗」无法解释 */
  ok(roll.includes('自 2026-11-16 起') && roll.includes('mom60'),
     'suspect 因子带 since（逐期入选个数变化才可解释）', strip(roll).slice(-120));

  /* 点击展开 */
  ok(CLICKERS.length === n, '点击处理器绑定全部推荐行', CLICKERS.length + ' / ' + n);
  const second = CLICKERS[1] ? CLICKERS[1].dataset.sym : null;
  if (second) {
    CLICKERS[1].onclick();
    const det = nodes.pickDetail.innerHTML;
    ok(det.length > 200, '点击后详情有内容', '长度 ' + det.length);
    ok(det.includes('为什么选它'), '详情含「为什么选它」');
    ok(det.includes('fk-contrib'), '详情含因子贡献条');
    ok(!det.includes('undefined'), '详情无 undefined');
    ok(det.includes('买入条件'), '详情含买入条件');
    ok(det.includes('卖出条件'), '详情含卖出条件');
    /* 再点收起 */
    nodes.picksBody.querySelectorAll('tr.row-main')[1].onclick();
    ok(nodes.pickDetail.innerHTML === '', '再点同一行则收起');
  } else {
    ok(false, '找不到第二行');
  }

  /* ---------- T05-4：rolling 缺失时的两种说法必须分开 ----------
     生产文件现在就是「has rolling: False」（新流程还没跑过），
     用户看到「尚未运行」会想「是不是我这坏了」，所以要给原因；
     而「跑过但本次没产出」是另一类问题，不能混成同一句话。 */
  sandbox.renderRolling({ freshness: { lastCalcAt: null } });
  const noRollNever = nodes.rollBox.innerHTML;
  ok(noRollNever.includes('新流程尚未运行过') && noRollNever.includes('每日 16:10'),
     'rolling 缺失 + 从未跑过 → 给出原因与何时生成', strip(noRollNever).slice(0, 70));
  sandbox.renderRolling({ freshness: { lastCalcAt: '2026-11-16T16:22:11' } });
  const noRollRan = nodes.rollBox.innerHTML;
  ok(noRollRan.includes('本次没有产出滚动验证结果')
     && !noRollRan.includes('新流程尚未运行过'),
     '跑过但本次没产出 → 另一种说法（不混成「从未运行」）', strip(noRollRan).slice(0, 70));
  sandbox.renderRolling({ rolling: ROLL_FIXTURE, freshness: FRESH_FIXTURE });   /* 复原 */
  ok(nodes.rollBox.innerHTML.includes('逐期明细') || nodes.rollBox.innerHTML.includes('展开全部'),
     '复原后卡片正常（上面两次临时渲染没留下残留）');

  /* ---------- T09：扣费呈现与「证据不足不许亮绿灯」 ----------
     注意：这三个用例**必须显式构造「缺扣费字段」的后端响应**，不能靠
     「生产 factor_score.json 恰好没有 cost 字段」来达成。
     原因：T08 真跑完 pipeline 后，生产文件已经带上 cost / validation.costed /
     costSensitivity / robustness（2026-11-16 20:51 首次产出），靠生产文件的
     偶然状态来测「字段缺失降级」会在产物更新后失效 —— 本轮真机验证就实测到
     3 条假红。降级渲染是**必须长期守住**的行为，得由测试自己造场景。
     做法：以真实生产数据为基底，克隆一份并删掉扣费四段，模拟旧版后端。 */
  ok(kpi.includes('扣费后 Top10 年化') && kpi.includes('费用×2 后'),
     'KPI 增加扣费后与费用翻倍两张卡');
  {
    /* 旧版后端：没有 cost / costed / costSensitivity / robustness 任何一段 */
    const legacy = JSON.parse(JSON.stringify(api));
    delete legacy.cost;
    delete legacy.settlement;
    delete legacy.costSensitivity;
    delete legacy.robustness;
    delete legacy.evidenceGate;
    if (legacy.validation) delete legacy.validation.costed;
    if (legacy.validation) delete legacy.validation.icCostNote;
    sandbox.renderKpis(legacy);
    const kpiLegacy = nodes.pkKpis.innerHTML;
    ok(kpiLegacy.includes('未计算（后端尚未产出扣费字段）') &&
       kpiLegacy.includes('未计算（后端尚未产出敏感性字段）') && !kpiLegacy.includes('undefined'),
       '新字段缺失时降级为「未计算」，不伪造 0 / undefined');

    sandbox.renderVerdict(legacy);
    ok(nodes.pkVerdict.textContent.includes('尚未计算') &&
       !nodes.pkVerdict.textContent.includes('通过了基础检验'),
       '旧版后端无扣费字段时横幅不许亮绿灯');
    ok(nodes.pkVerdict.textContent.includes('扣费后验证尚未计算'),
       '旧版后端明说扣费后验证尚未计算', nodes.pkVerdict.textContent.slice(0, 40));

    sandbox.renderRolling({ ...legacy, freshness: FRESH_FIXTURE });
    ok(strip(nodes.rollBox.innerHTML).includes('扣费后口径尚未计算'),
       '滚动卡片在扣费字段缺失时明说未计算（上方 IC 仍是毛口径）',
       strip(nodes.rollBox.innerHTML).slice(0, 70));
    /* 复原成生产数据，后面的用例继续在真实产物上跑 */
    sandbox.renderKpis(api);
    sandbox.renderRolling({ rolling: ROLL_FIXTURE, freshness: FRESH_FIXTURE });
  }
  ok(caveat.includes('IC 仍为毛口径') && caveat.includes('滑点为假设值'),
     '警示清单写清 IC 毛口径 / 成本档位 / 滑点假设三层口径');

  (function () {
    /* 全绿只在四道门都过时才允许出现 */
    const passAll = {
      validation: { t_nw: 6.74, oos: null,
        costed: { verdict: 'pass', netTop10Annual: 0.123,
                  gate: { passed: true, actual: 520, requiredTrades: 50 } } },
      oos: { verdict: 'pass' },
      costSensitivity: { verdict: 'pass', doubled: { netTop10Annual: 0.045 } },
      robustness: { verdict: 'pass' }
    };
    sandbox.renderVerdict(passAll);
    ok(nodes.pkVerdict.textContent.includes('通过了基础检验与扣费后检验'),
       '四道门全过才亮绿灯', nodes.pkVerdict.textContent.slice(0, 40));

    /* 样本不足：哪怕 IC 显著 + 前后段通过，也只能降级 */
    sandbox.renderVerdict({ validation: { t_nw: 9.0, costed: {
      verdict: 'insufficient', n_trades: 12,
      gate: { passed: false, actual: 12, requiredTrades: 50 } } }, oos: { verdict: 'pass' } });
    const ins = nodes.pkVerdict.textContent;
    ok(ins.includes('样本不足 12 笔') && ins.includes('50 笔') &&
       ins.includes('不能判定有效') && !ins.includes('通过'),
       '样本 <50 笔时单独降级，不亮绿灯', ins.slice(0, 60));

    /* 费用翻倍不成立 / 参数簇失败 */
    sandbox.renderVerdict({ validation: { t_nw: 9.0, costed: {
      verdict: 'pass', gate: { passed: true } } }, oos: { verdict: 'pass' },
      costSensitivity: { verdict: 'fail' } });
    ok(nodes.pkVerdict.textContent.includes('不可用'),
       '费用×2 判定 fail 时横幅明说不可用');
    sandbox.renderVerdict({ validation: { t_nw: 9.0, costed: {
      verdict: 'pass', gate: { passed: true } } }, oos: { verdict: 'pass' },
      robustness: { verdict: 'fail' } });
    ok(nodes.pkVerdict.textContent.includes('不可用'),
       '参数簇稳健性 fail 时横幅明说不可用');

    /* KPI 有值时按百分比显示，不能出现 NaN */
    sandbox.renderKpis({ validation: { horizon: 5, costed: {
      netTop10Annual: 0.123, n_trades: 520, verdict: 'pass' } },
      costSensitivity: { verdict: 'weak', doubled: { netTop10Annual: 0.045 } },
      oos: { verdict: 'pass' }, factors: [] });
    const k2 = nodes.pkKpis.innerHTML;
    ok(k2.includes('+12.30%') && k2.includes('+4.50%') && k2.includes('偏弱') &&
       !k2.includes('NaN'), '新字段齐全时显示净年化与费用翻倍判定', strip(k2).slice(-120));

    /* 滚动卡：净口径小结必须存在，且绝不把原始 IC 端上来 */
    sandbox.renderRolling({ rolling: ROLL_FIXTURE, freshness: FRESH_FIXTURE,
      validation: { costed: { netTopSpreadAnnual: 0.18, n_trades: 520 } },
      costSensitivity: { verdict: 'pass', doubled: { netTop10Annual: 0.09 } } });
    const r2 = strip(nodes.rollBox.innerHTML);
    ok(r2.includes('扣费后十分组价差年化 +18.00%') && r2.includes('费用×2 后 +9.00%'),
       '滚动卡净口径小结显示价差 / 样本 / 费用翻倍', r2.slice(100, 230));
    ok(!nodes.rollBox.innerHTML.includes('-0.0481') && r2.includes('平均有效 IC'),
       '加净口径后仍不读原始 IC、平均 IC 仍为有效 IC');
  })();

  /* freshness.js：证据门槛是可选字段；不传时技术细节一字不变 */
  (function () {
    const plain = sandbox.window.fkFresh.detailHtml({ warnings: ['已有告警'] }, {});
    const withGate = sandbox.window.fkFresh.detailHtml({ warnings: ['已有告警'],
      evidenceGate: { passed: false, level: 'insufficient', failed: ['样本不足'],
                      note: '本轮不能判定有效' } }, {});
    ok(!plain.includes('证据门槛') && plain.includes('已有告警'),
       'freshness 技术细节无 evidenceGate 时保持旧渲染');
    ok(withGate.includes('证据门槛') && withGate.includes('样本不足') &&
       withGate.includes('已有告警'),
       'evidenceGate 可选行追加在 warnings 之后，两者并存');
  })();

  console.log('\n横幅：' + verdict);
  console.log('状态行：' + strip(status).slice(0, 140));
  console.log('滚动卡片：' + strip(roll).slice(0, 200));
  console.log('\nRESULT: ' + (fail ? ('fail=' + fail) : 'pass'));
  process.exit(fail ? 1 : 0);
}, 300);
