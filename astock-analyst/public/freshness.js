/*!
 * public/freshness.js — 因子数据「新鲜度」的渲染（因子体检页 + 智能选股页共用）
 *
 * ⚠ 命名陷阱（先读这一段，能省一次误改）
 *   `lib/reconcile.js` 里也有 freshness 这个词，但那是「本地 K 线最新收盘 vs 线上最新
 *   收盘是否一致」的**对账类型**（`kind='freshness'`，见 reconcile.js 第 190/258/544 行），
 *   对应的 `/api/data-quality` scope 叫 **`factor-panel`**，不叫 freshness。
 *   本文件管的是另一件事：因子数据算到哪一天、上次算完没有、下次什么时候算。
 *   两者只是中文都叫「新鲜度」，**不是一套东西**，不要互相引用。
 *
 * 三条硬规则（说明书 §8 + T05）
 *   1. **`reason` 非空就显示，不按 `state` 过滤** —— `fresh` 状态下也可能有
 *      「上次运行未正常结束，但数据已到位」这种提示，按状态过滤会把它吃掉。
 *   2. **前端不做任何时间推算**：不调用任何日期构造函数、不读「文件生成时刻」字段。
 *      数据日期、星期几、上次计算时间、下次更新时间**全部直接用接口字段**
 *      （`dataThrough` / `dataThroughWeekday` / `lastCalcAt` / `nextRunAt`）。
 *      （T05 的 grep 断言扫的就是这两样，所以连注释里都不写那两个写法。）
 *   3. **技术细节折叠**：用原生 `<details>`，点徽标才展开，不写 JS 开关。
 *
 * 字段缺失（T03 产线还没跑过时 `factor_api.json` 里没有 `freshness`）一律显示「未知」，
 * 绝不抛异常 —— 白屏比缺字段难查得多。
 *
 * 用法
 *   <script src="/freshness.js"></script>
 *   fkFresh.render(document.getElementById('fkStatus'), api.freshness, { evalAsOf: api.evalAsOf });
 *   document.getElementById('fkFreshBanner').innerHTML = fkFresh.bannerHtml(api.freshness);
 */
(function (global) {
  'use strict';

  /* 4 态 → 徽标文案（与后端 freshness.STATUS_LABELS 同义，但徽标更短，
     状态标签原文由后端 statusLabel 提供，这里只负责「短徽标」） */
  var STATE_LABEL = { fresh: '最新', pending: '今日未更新', stale: '已过期', failed: '更新失败' };
  var STATE_CLASS = { fresh: 'is-fresh', pending: 'is-pending', stale: 'is-stale', failed: 'is-failed' };
  var STATES = ['fresh', 'pending', 'stale', 'failed'];

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/[&<>"']/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
      });
  }

  function txt(v) {
    if (v === null || v === undefined) return null;
    var s = String(v).trim();
    return s ? s : null;
  }

  function isNum(v) { return typeof v === 'number' && isFinite(v); }

  /** 后端字段名是 `status`（freshness.compute），说明书里写的是 `state`。两个都认。 */
  function stateOf(f) {
    if (!f || typeof f !== 'object') return 'unknown';
    var s = txt(f.status) || txt(f.state);
    return (s && STATE_LABEL[s]) ? s : 'unknown';
  }

  /** 纯切片，不做任何日期运算（规则 2） */
  function shortTime(iso) {
    var s = txt(iso);
    if (!s) return null;
    return s.length >= 16 ? (s.slice(5, 10) + ' ' + s.slice(11, 16)) : s;
  }

  function planTime(s) {
    var v = txt(s);
    if (!v) return null;
    return v.length >= 16 ? (v.slice(5, 10) + ' ' + v.slice(11, 16)) : v;
  }

  function fmtSci(v) {
    if (!isNum(v)) return '—';
    if (v === 0) return '0';
    return Number(v).toExponential(1).replace('e', 'e');
  }

  function fmtPct(v, d) {
    return isNum(v) ? (v * 100).toFixed(d === undefined ? 1 : d) + '%' : '—';
  }

  /* ── 徽标 ─────────────────────────────────────────────── */
  function badgeText(f) {
    var s = stateOf(f);
    if (s === 'unknown') return '未知';
    if (s === 'stale') {
      var lag = (f && f.lagTradingDays);
      return (isNum(lag) && lag > 0) ? ('已过期 ' + lag + ' 个交易日') : '已过期';
    }
    return STATE_LABEL[s];
  }

  function badgeHtml(f) {
    var s = stateOf(f);
    var cls = STATE_CLASS[s] || 'is-unknown';
    /* title 用后端的 statusLabel，保证「短徽标」和「长标签」都来自同一判定 */
    var title = (f && txt(f.statusLabel)) || STATE_LABEL[s] || '未知';
    return '<span class="fk-fbadge ' + cls + '" title="' + esc(title) + '">' +
      esc(badgeText(f)) + '</span>';
  }

  /* ── 三段式 ───────────────────────────────────────────── */
  /* 为什么写「下次更新 11-17 16:10」而不是「今天 16:10」：
     说「今天」必须先知道今天是几号，而导出文件是**固化的快照**——
     文件放两天再打开，「今天」就变成了假话。要显示「正在重算」的实时信息，
     页面应该去读 /api/scheduler/status 的 busy.factor，那里才是活的。
     这也是规则 2「前端不做时间推算」的具体后果：宁可用绝对日期。 */
  function lineText(f) {
    if (!f || typeof f !== 'object') {
      return '数据截止 未知 · 上次计算 未知 · 下次更新 未知';
    }
    var through = txt(f.dataThrough);
    var wk = txt(f.dataThroughWeekday);
    var head = '数据截止 ' + (through ? (through + (wk ? '（' + wk + '）' : '')) : '未知');
    var mid = '上次计算 ' + (shortTime(f.lastCalcAt) || '未知');
    var tail = '下次更新 ' + (planTime(f.nextRunAt) || '未知');
    return head + ' · ' + mid + ' · ' + tail;
  }

  /* ── reason（规则 1：非空就显示，不看 state）───────────── */
  function reasonHtml(f) {
    var why = txt(f && f.reason);
    if (!why) return '';
    var bad = stateOf(f) === 'failed' ? ' is-bad' : '';
    return '<span class="fk-freason' + bad + '">' + esc(why) + '</span>';
  }

  /* ── 技术细节（折叠）──────────────────────────────────── */
  function reconcileLines(f) {
    var out = [];
    if (!f || typeof f !== 'object') return out;
    var rec = (f.cacheVerify || {}).reconcile || {};
    var suspects = rec.suspectFactors || [];
    if (suspects.length || txt(rec.state) === 'suspect') {
      var head = suspects.slice(0, 5).join('、');
      var more = suspects.length > 5 ? (' 等 ' + suspects.length + ' 个') : '';
      out.push({
        cls: 'fk-fdtl-bad',
        html: suspects.length + ' 个因子 IC 未经复核，滚动验证已跳过：' + (head || '（未列出）') + more
      });
      return out;
    }
    /* 两类偏差性质不同（数据问题 vs 参数问题），**都显示、不合并成一句** */
    var dataWarn = (rec.dataWarn === true) || (isNum(rec.maxAbsDiff) && rec.maxAbsDiff > 1e-5);
    var caliberWarn = (rec.caliberWarn === true) || (isNum(rec.extraPct) && rec.extraPct > 0.05);
    if (dataWarn) {
      out.push({
        cls: 'fk-fdtl-warn',
        html: '对账偏差：重叠日最大差 ' + fmtSci(rec.maxAbsDiff) +
          (txt(rec.firstWarnAt) ? ('（自 ' + esc(rec.firstWarnAt) + ' 起）') : '')
      });
    }
    if (caliberWarn) {
      out.push({
        cls: 'fk-fdtl-warn',
        html: '口径差异：' + (isNum(rec.extraDays) ? rec.extraDays : '—') + ' 天（' +
          fmtPct(rec.extraPct) + '）因样本不足 ' + (isNum(f.minStocks) ? f.minStocks : 100) +
          ' 只被排除'
      });
    }
    return out;
  }

  function kv(rows) {
    return '<table class="fk-fdtl-table"><tbody>' + rows.filter(function (r) {
      return r && r[1] !== null && r[1] !== undefined && r[1] !== '';
    }).map(function (r) {
      return '<tr><td>' + esc(r[0]) + '</td><td>' + r[1] + '</td></tr>';
    }).join('') + '</tbody></table>';
  }

  /**
   * 技术细节块。
   * @param {object} f   freshness 对象
   * @param {object} [opts] `{evalAsOf}` —— `evalAsOf` 是顶层字段，不在 freshness 里
   */
  function detailHtml(f, opts) {
    opts = opts || {};
    var body = [];
    if (!f || typeof f !== 'object') {
      body.push('<div class="fk-fdtl-row">接口里没有 freshness 字段（T03 产线尚未产出），' +
        '所以「数据截止」这类信息暂时不可用。</div>');
    } else {
      var cv = f.cacheVerify || {};
      var rows = [
        ['dataThrough', esc(txt(f.dataThrough) || '—')],
        ['expectedThrough', esc(txt(f.expectedThrough) || '—')],
        ['lagTradingDays', isNum(f.lagTradingDays) ? String(f.lagTradingDays) : '—'],
        ['lastCalcAt', esc(txt(f.lastCalcAt) || '—')],
        ['lastCalcOk', f.lastCalcOk === true ? 'true' : (f.lastCalcOk === false ? 'false' : '—')],
        ['nextRunAt', esc(txt(f.nextRunAt) || '—')],
        ['factorTime', esc(txt(f.factorTime) || '—')],
        ['runId', esc(txt(f.runId) || '—')],
        ['partial', f.partial === true ? 'true' : 'false']
      ];
      if (txt(f.lastCalcError)) rows.push(['lastCalcError', esc(f.lastCalcError)]);
      if (isNum(cv.checked)) {
        rows.push(['cacheVerify', esc('checked=' + cv.checked +
          (isNum(cv.maxRelErr) ? (' maxRelErr=' + fmtSci(cv.maxRelErr)) : '') +
          (cv.ok === true ? ' ok' : (cv.ok === false ? ' 未通过' : '')))]);
      }
      var ts = cv.ts || {};
      if (ts.factors) {
        rows.push(['时间序列', esc(ts.factors + ' 个因子 · 最新 ' + (ts.last || '—') +
          (ts.initialized === false ? '（未初始化）' : ''))]);
      }
      var panel = cv.panel || {};
      if (panel.dates || panel.symbols) {
        rows.push(['面板缓存', esc((panel.factors || '—') + ' 因子 / ' +
          (panel.dates || '—') + ' 天 / ' + (panel.symbols || '—') + ' 只')]);
      }
      body.push(kv(rows));
    }

    /* evalAsOf：单因子 IC 是**离线**统计的，不随每日 pipeline 重算。
       不标出来，用户会把 evalAsOf 当成今天。 */
    var evalAsOf = txt(opts.evalAsOf);
    if (evalAsOf) {
      body.push('<div class="fk-fdtl-row fk-fdtl-note">evalAsOf ' + esc(evalAsOf) +
        ' —— 单因子 IC 统计基于此日期评估，非每日更新</div>');
    }

    reconcileLines(f).forEach(function (line) {
      body.push('<div class="fk-fdtl-row ' + line.cls + '">' + line.html + '</div>');
    });

    var warnings = (f && f.warnings) || [];
    if (warnings.length) {
      body.push('<div class="fk-fdtl-row fk-fdtl-warn">本次告警：' +
        warnings.map(function (w) { return esc(w); }).join('；') + '</div>');
    }
    /* T09：后端已把证据门槛失败写进 warnings，所以这不是必需字段。
       若将来直接透传 evidenceGate，就多一行独立说明；缺失时整行不出现，
       旧版 JSON 的渲染一字不变。它是「这次运行的结论」不是运行状态，
       放在技术细节里不进徽标，避免和「数据新不新鲜」混成一个维度。 */
    var eg = (f && f.evidenceGate) || null;
    if (eg && typeof eg === 'object') {
      var failed = (eg.failed || []).map(function (x) { return String(x); });
      body.push('<div class="fk-fdtl-row' +
        (eg.passed === false ? ' fk-fdtl-warn' : '') + '">证据门槛：' +
        esc(txt(eg.level) || '未计算') +
        (failed.length ? '（' + esc(failed.join('、')) + '）' : '') +
        (txt(eg.note) ? ' —— ' + esc(eg.note) : '') + '</div>');
    }
    return '<div class="fk-fdtl-body">' + body.join('') + '</div>';
  }

  /* ── 横幅（只给 stale / failed：数据不能用，就必须在表格上方说清）── */
  function reasonOr(f, fallback) {
    return txt(f && f.reason) || fallback;
  }

  function bannerHtml(f) {
    var s = stateOf(f);
    if (s === 'failed') {
      return '<div class="fk-fbanner is-bad">✕ <b>上次计算失败</b>：' +
        esc(reasonOr(f, '原因未记录')) + '。下面显示的是 ' +
        esc(shortTime(f.lastCalcAt) || '上一次') + ' 那次算出来的结果。</div>';
    }
    if (s === 'stale') {
      var through = txt(f.dataThrough);
      var expect = txt(f.expectedThrough);
      return '<div class="fk-fbanner is-warn">⚠ <b>数据不是最新的</b>：' +
        esc(reasonOr(f, '数据已过期')) +
        '。当前结果基于 ' + esc(through || '未知') + ' 的数据' +
        (expect ? ('，行情已走到 ' + esc(expect)) : '') + '。</div>';
    }
    return '';
  }

  /* ── 渲染 ─────────────────────────────────────────────── */
  function html(f, opts) {
    return '<span class="fk-fline">' + esc(lineText(f)) + '</span>' +
      '<details class="fk-fdtl"><summary title="点徽标看技术细节">' + badgeHtml(f) + '</summary>' +
      detailHtml(f, opts) + '</details>' +
      reasonHtml(f);
  }

  /**
   * 写进一个容器元素（状态行位置）。
   * @returns {string} 生成的 HTML（测试直接断言返回值，不必解析 DOM）
   */
  function render(el, f, opts) {
    var out = html(f, opts);
    if (el) {
      el.innerHTML = out;
      var cls = (el.className || '') + '';
      if (cls.indexOf('fk-fresh') < 0) el.className = (cls + ' fk-fresh').trim();
    }
    return out;
  }

  global.fkFresh = {
    STATES: STATES,
    stateOf: stateOf,
    badgeText: badgeText,
    badgeHtml: badgeHtml,
    lineText: lineText,
    detailHtml: detailHtml,
    reasonHtml: reasonHtml,
    bannerHtml: bannerHtml,
    html: html,
    render: render,
    _esc: esc
  };
})(typeof window !== 'undefined' ? window : this);
