// factors 页面运行时冒烟测试：用最小 DOM 桩执行页面脚本，验证渲染结果非空且含关键字段。
// 不依赖浏览器，只验证「脚本能不能跑完 + 渲染出什么 + 点击后详情是否真的出现在那一行下面」。
//
// 2026-11-15 改动：详情从「表格下方独立容器」改为「展开在被点击行正下方」，
// 因此这里不再找 #detail，改为解析 tbody.innerHTML 里的 tr.fk-expand[data-for]。
// 并新增「模拟点击第二行 → 展开行跟着移动」的断言，防止再次出现「点了没反应」。
//
// 2026-11-16（T05）改动：
//   1. 载入 public/freshness.js（两页共用的新鲜度渲染），沙箱里提供 window。
//   2. 生产 factor_api.json 目前**没有** freshness 字段（要等 T03 产线 24/24 之后
//      才产出），所以这里注入一份 fixture，把「字段齐全时会渲染成什么样」也测到；
//      另外单独断言「字段缺失 → 显示未知、不崩」。
//   3. 状态行改用 innerHTML（原来读 textContent），并新增新鲜度 6 项断言：
//      4 态徽标 / 横幅位置不在 caveatList / 三段式字段逐字相同 / reason 在 fresh 下也显示 /
//      技术细节默认折叠且含 evalAsOf / 缺字段容错。
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'public', 'factors.html'), 'utf8');
const m = html.match(/<script(?![^>]*src=)[^>]*>([\s\S]*?)<\/script>/);
if (!m) { console.error('FAIL 找不到内联脚本'); process.exit(1); }
const code = m[1];

const freshCode = fs.readFileSync(path.join(__dirname, '..', 'public', 'freshness.js'), 'utf8');

const api = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'sequoia-service', 'data', 'factor_api.json'), 'utf8'));

/* ---- freshness fixture（注入，不改生产文件）----
   stale + lag=2：横幅、徽标、reason 三条路径一次性覆盖到。 */
const FRESH_FIXTURE = {
  ok: true,
  status: 'stale', statusLabel: '数据已过期',
  reason: '已过期 2 个交易日',
  dataThrough: '2026-11-11', dataThroughWeekday: '周三',
  expectedThrough: '2026-11-13', lagTradingDays: 2,
  lastCalcAt: '2026-11-15T22:49:03', lastCalcOk: true, lastCalcError: null,
  nextRunAt: '2026-11-17 16:10', factorTime: '16:10',
  partial: false, warnings: [], runId: '20261115T224203', cost: 512.3,
  cacheVerify: {
    reconcile: { state: 'ok', maxAbsDiff: 2.04e-6, extraDays: 8, extraPct: 0.021 },
    ts: { initialized: true, factors: 24, last: '2026-11-11' },
    panel: { factors: 24, dates: 1420, symbols: 5030, sig: 'min=2021-01-04|nf=24' }
  }
};
api.freshness = FRESH_FIXTURE;
api.evalAsOf = '2026-11-10';

/* ---- 极简 DOM 桩 ---- */
function el(id) {
  const node = {
    id,
    _html: '',
    textContent: '',
    className: '',
    value: '',
    dataset: {},
    classList: { add() {}, remove() {} },
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html; },
    querySelectorAll() { return []; },
    appendChild() {}
  };
  return node;
}

/* tbody 的 querySelectorAll 需要真的能找出 row-main 并把 onclick 交回来，
   否则点击逻辑根本没被测到。 */
function rowsOf(html) {
  const out = [];
  const re = /<tr class="row-main[^"]*" data-n="([^"]+)"/g;
  let mm;
  while ((mm = re.exec(html))) out.push({ n: mm[1] });
  return out;
}

const nodes = {};
['kpis', 'caveatList', 'tbody', 'fkStatus', 'filters', 'q', 'caveat', 'metricDl',
 'filterHint', 'fkFreshBanner'].forEach(k => {
  nodes[k] = el(k);
});
nodes.filters.querySelectorAll = () => [];
nodes.metricDl.querySelectorAll = () => [];

/* tbody 桩：返回带 dataset.n 和可赋值 onclick 的行对象，并记录到 CLICKERS。
   注意必须按 data-n 缓存同一个对象——否则每次 querySelectorAll 都造新对象，
   页面上刚绑好的 onclick 会丢，测试就会误报「再点一次收起」失败。 */
const CLICKERS = [];
const ROWCACHE = new Map();
nodes.tbody.querySelectorAll = function (sel) {
  if (sel !== 'tr.row-main') return [];
  CLICKERS.length = 0;
  return rowsOf(this._html).map(r => {
    let obj = ROWCACHE.get(r.n);
    if (!obj) {
      obj = { dataset: { n: r.n }, _onclick: null };
      Object.defineProperty(obj, 'onclick', {
        get() { return this._onclick; },
        set(fn) { this._onclick = fn; }
      });
      ROWCACHE.set(r.n, obj);
    }
    CLICKERS.push(obj);
    return obj;
  });
};

const document = {
  getElementById: id => nodes[id] || el(id),
  querySelectorAll: () => [],
  querySelector: () => null,          /* renderDetailIntoView 会用到，返回 null 即安全退出 */
  createElement: () => el('tmp'),
  body: { insertBefore() {}, firstChild: null }
};

const sandbox = {
  document,
  console,
  fetch: () => Promise.resolve({ json: () => Promise.resolve(api) }),
  MiloNav: { mount: () => {} },
  location: { hash: '', search: '' },
  window: { innerHeight: 800, pageYOffset: 0, scrollTo() {}, location: { search: '' } },
  setTimeout, clearTimeout,
  Math, JSON, Number, String, Object, Array, isNaN, URLSearchParams
};
vm.createContext(sandbox);
/* 先跑共用的 freshness.js，再跑页面内联脚本（顺序与真实页面一致） */
vm.runInContext(freshCode, sandbox, { filename: 'freshness.js' });
vm.runInContext(code, sandbox, { filename: 'factors.inline.js' });

let fail = 0;
function ok(cond, label, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + label + (extra ? '  ' + extra : ''));
  if (!cond) fail++;
}

function expandOf(html) {
  const mm = html.match(/<tr class="fk-expand" data-for="([^"]+)"/);
  return mm ? mm[1] : null;
}
const strip = s => String(s || '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();

setTimeout(() => {
  const tb = nodes.tbody.innerHTML;
  const kpi = nodes.kpis.innerHTML;
  const status = nodes.fkStatus.innerHTML;
  const banner = nodes.fkFreshBanner.innerHTML;
  const fresh = sandbox.window.fkFresh;

  /* ---------- 基础渲染 ---------- */
  ok(tb.includes('corr_pv20'), '表格渲染出因子行', '长度 ' + tb.length);
  const nRows = (tb.match(/<tr class="row-main/g) || []).length;
  ok(nRows === 24, '因子行数 = 24', '实际 ' + nRows);
  /* 面板行数不写死：面板会随回填扩样本而变，写死数字只会在扩样本后假失败。
     但也不能只断言"有数字"——那连显示成 0 行都查不出来，所以要求是亿级。 */
  const rowsTxt = (kpi.match(/([\d,]+)\s*行/) || [])[1] || '';
  const rowsNum = parseInt(rowsTxt.replace(/,/g, ''), 10);
  ok(rowsNum > 1e8, 'KPI 面板规模为亿级行数', rowsTxt + ' 行');
  ok(kpi.includes('5,030') || kpi.includes('5030'), 'KPI 显示覆盖标的');
  ok(status.includes('24 个因子'), '状态栏文案', strip(status).slice(0, 80));

  /* ---------- T05：新鲜度（两页共用 freshness.js）---------- */
  ok(!!fresh, 'public/freshness.js 已加载并挂到 window.fkFresh');
  /* 1) 4 态徽标：文案必须各就各位，stale 要带交易日数 */
  const four = ['fresh', 'pending', 'stale', 'failed'].map(st =>
    fresh.badgeText({ status: st, lagTradingDays: st === 'stale' ? 2 : 0 }));
  ok(four.join('|') === '最新|今日未更新|已过期 2 个交易日|更新失败',
     '4 态徽标文案正确', four.join(' / '));
  /* 2) 页面用的徽标类名命中（stale → is-stale，橙红） */
  ok(/fk-fbadge is-stale/.test(status), '状态行渲染出 stale 徽标（is-stale）',
     (status.match(/fk-fbadge [a-z-]+/) || [''])[0]);
  /* 3) 三段式字段逐字相同：全部来自接口字段，前端不做任何推算 */
  const wantLine = '数据截止 2026-11-11（周三） · 上次计算 11-15 22:49 · 下次更新 11-17 16:10';
  ok(strip(status).includes(wantLine), '三段式与接口字段逐字相同', strip(status).slice(0, 90));
  ok(fresh.lineText({}) === '数据截止 未知 · 上次计算 未知 · 下次更新 未知',
     'freshness 缺失时三段式显示「未知」而不是崩掉');
  /* 4) 横幅：位置在表格上方独立容器，**不在** caveatList 里 */
  ok(banner.includes('fk-fbanner') && banner.includes('数据不是最新的'),
     '过期时在表格上方插入独立横幅', strip(banner).slice(0, 60));
  ok(!strip(nodes.caveatList.innerHTML).includes('数据不是最新的'),
     '横幅不混进 caveatList（那是因子语义的边界，另一件事）');
  /* 5) reason 非空就显示：fresh 状态下也要显示（不按 state 过滤） */
  const freshWithReason = fresh.html(Object.assign({}, FRESH_FIXTURE, {
    status: 'fresh', lagTradingDays: 0, dataThrough: '2026-11-13',
    reason: '上次运行未正常结束（startedAt=2026-11-16T14:00:00，已超过 90 分钟），但数据已到位'
  }));
  ok(freshWithReason.includes('未正常结束') && /fk-fbadge is-fresh/.test(freshWithReason),
     'reason 在 fresh 状态下也显示（不按 state 过滤）');
  /* 6) 技术细节：默认折叠 + evalAsOf 标注 + 字段齐全 */
  ok(/<details class="fk-fdtl">/.test(status) && !/<details class="fk-fdtl" open/.test(status),
     '技术细节默认折叠（原生 <details>，不写 JS 开关）');
  ok(status.includes('evalAsOf') && status.includes('非每日更新'),
     '技术细节含 evalAsOf 且注明「非每日更新」');
  ok(status.includes('2026-11-15T22:49:03') && status.includes('2026-11-11'),
     '技术细节里的时间原样透出（不切片、不推算）');

  /* ---------- 默认收起：点了才展开 ---------- */
  ok(expandOf(tb) === null, '默认不展开任何一行', '展开的是 ' + expandOf(tb));
  ok(!tb.includes('这个指标在量什么'), '未点击时表格里不含详情块（不会误以为详情固定在此）');
  ok(!tb.includes('undefined'), '表格无 undefined');
  ok(!/NaN/.test(tb.replace(/NaN 值/g, '')), '无 NaN 泄漏');

  /* ---------- 指标说明与筛选说明 ---------- */
  ok(nodes.metricDl.innerHTML.includes('Newey-West') || nodes.metricDl.innerHTML.includes('t 值'),
     '「怎么看这张表」指标说明已渲染', '长度 ' + nodes.metricDl.innerHTML.length);
  ok(nodes.filterHint.innerHTML.includes('当前'), '筛选档位说明已渲染（不靠悬停也能看到）',
     nodes.filterHint.innerHTML.replace(/<[^>]+>/g, '').slice(0, 50));
  ok(nodes.filterHint.innerHTML.includes('24'), '筛选说明里带当前档位的因子个数');
  ok(nodes.filterHint.innerHTML.includes('点任意一行'), '筛选说明明示「点行可展开」');

  /* ---------- 点开第一行：人话层、分组图、分板块表都必须在该行下面 ---------- */
  ok(CLICKERS.length === 24, '点击处理器绑定到全部 24 行', '实际 ' + CLICKERS.length);
  const firstName = CLICKERS[0].dataset.n;
  CLICKERS[0].onclick();
  const tbA = nodes.tbody.innerHTML;
  ok(expandOf(tbA) === firstName, '点击首行后详情展开在该行内',
     '点击 ' + firstName + ' → 展开 ' + expandOf(tbA));
  ok(tbA.includes('正在看'), '展开区有「正在看 x，再点收起」提示条');
  ok(tbA.includes('这个指标在量什么'), '详情含「这个指标在量什么」');
  ok(tbA.includes('数值高说明'), '详情含「数值高说明」');
  ok(/实测：这个指标(越大|越小)的股票/.test(tbA), '详情含按实测生成的一句话结论',
     (tbA.match(/实测：[^<]{0,40}/) || [''])[0]);
  ok(tbA.includes('fk-say'), '人话结论卡片有样式类');
  ok(tbA.includes('十分组'), '展开详情含分组图');
  ok(tbA.includes('分板块'), '展开详情含分板块表');
  ok(!tbA.includes('undefined'), '展开详情无 undefined');
  ok(!/NaN/.test(tbA.replace(/NaN 值/g, '')), '展开详情无 NaN 泄漏');
  /* 收起来，后面的点击测试从干净状态开始 */
  CLICKERS[0].onclick();
  ok(expandOf(nodes.tbody.innerHTML) === null, '再点一次恢复收起');

  /* ---------- 点击第二行，详情必须跟着移动 ---------- */
  const second = CLICKERS[1] ? CLICKERS[1].dataset.n : null;
  if (second) {
    CLICKERS[1].onclick();
    const tb2 = nodes.tbody.innerHTML;
    const now = expandOf(tb2);
    ok(now === second, '点击第二行后详情展开到该行', '点击 ' + second + ' → 展开 ' + now);
    ok((tb2.match(/<tr class="fk-expand"/g) || []).length === 1, '同时只展开一行');
    ok(tb2.includes('class="row-main active"'), '被点击行高亮');

    /* 再点同一行应收起 */
    const again = nodes.tbody.querySelectorAll('tr.row-main')[1];
    again.onclick();
    const tb3 = nodes.tbody.innerHTML;
    ok(expandOf(tb3) === null, '再点同一行则收起', '展开 ' + expandOf(tb3));
  } else {
    ok(false, '找不到第二行，点击测试未执行');
  }

  console.log('\n状态行：' + strip(status).slice(0, 160));
  console.log('横幅：' + strip(banner).slice(0, 120));
  console.log('\nRESULT: ' + (fail ? ('fail=' + fail) : 'pass'));
  process.exit(fail ? 1 : 0);
}, 300);
