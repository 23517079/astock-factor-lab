'use strict';
/*
 * app.js — 界面逻辑
 *
 * 三条原则：
 *   1. 任何数字都标出来源与时间，不裸奔
 *   2. 每个专业名词旁边有「?」，点开就是人话解释
 *   3. 出错时说清楚哪一步错了、下一步怎么做
 */

var $ = function (id) { return document.getElementById(id); };
var CFG = null;
var PICKED = [];
var ES = null;
var HAD_ERROR = false;

/* ---------------- 基础工具 ---------------- */

function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function toast(msg, kind) {
  var el = $('toast');
  el.textContent = msg;
  el.className = 'show' + (kind ? ' ' + kind : '');
  clearTimeout(el._t);
  el._t = setTimeout(function () { el.className = ''; }, 2800);
}

function api(url, opts) {
  return fetch(url, opts).then(function (r) {
    return r.json().then(function (j) {
      if (!r.ok && j && j.message) throw new Error(j.message);
      return j;
    });
  });
}

function go(view) {
  document.querySelectorAll('.tab').forEach(function (t) {
    t.classList.toggle('active', t.dataset.view === view);
  });
  document.querySelectorAll('.view').forEach(function (v) {
    v.classList.toggle('active', v.id === 'view-' + view);
  });
  /* 记到地址栏，刷新后停在同一页 */
  if (location.hash !== '#' + view) {
    try { history.replaceState(null, '', '#' + view); } catch (e) { location.hash = view; }
  }
  window.scrollTo(0, 0);
  if (view === 'history') loadHistory();
  if (view === 'settings' && !CFG) loadConfig();
}
document.querySelectorAll('.tab').forEach(function (t) {
  t.onclick = function () { go(t.dataset.view); };
});

/* ---------------- 顶栏状态 ---------------- */

function renderKeyPill() {
  var el = $('keyPill');
  if (!CFG) { el.className = 'pill'; el.innerHTML = '<span class="dot"></span>检查中'; return; }
  if (CFG.key.ready) {
    el.className = 'pill ok';
    el.innerHTML = '<span class="dot"></span>模型已就绪';
  } else {
    el.className = 'pill warn';
    el.innerHTML = '<span class="dot"></span>未配密钥';
  }
}

function renderKeyBanner() {
  var box = $('keyBanner');
  if (!CFG) { box.innerHTML = ''; return; }
  if (CFG.key.ready) {
    box.innerHTML =
      '<div class="banner ok"><span>模型已就绪 · ' + esc(CFG.llm.model) + '</span>' +
      '<span style="margin-left:auto;font-size:11.5px;opacity:.75">密钥来源：' + esc(CFG.key.source) + '</span></div>';
  } else {
    box.innerHTML =
      '<div class="banner warn"><div><strong>还没配密钥。</strong>' +
      '可以先点「不调模型，只跑数据」看客观因子，或去' +
      '<a href="#" onclick="go(\'settings\');return false" style="color:#fcd34d">设置</a>里配一个。</div></div>';
  }
}

/* ---------------- 配置 ---------------- */

function loadConfig() {
  return api('/api/config').then(function (c) {
    CFG = c;
    renderKeyPill();
    renderKeyBanner();
    fillSettings();
    renderPickChips();
    renderWatchList();
    renderKeywordEditor();
    $('cfgPath').textContent = c.paths.config;
    $('dataPath').textContent = c.paths.dataDir;
    $('envAliasList').textContent = c.key.envAliases.join(' → ');
    $('runHint').textContent = c.analysis.codes.length
      ? '默认清单 ' + c.analysis.codes.length + ' 只' : '清单为空，先去「标的」页添加';
    return c;
  }).catch(function (e) {
    toast('读取配置失败：' + e.message, 'bad');
  });
}

function fillSettings() {
  if (!CFG) return;
  $('cfgModel').value = CFG.llm.model || '';
  $('cfgBaseUrl').value = CFG.llm.baseUrlRaw || '';
  $('cfgBaseUrl').placeholder = CFG.llm.inferredBaseUrl
    ? '留空自动推断（当前生效：' + CFG.llm.baseUrl + '）'
    : 'https://api.example.com/v1';
  $('cfgConcurrency').value = CFG.analysis.concurrency;
  $('cfgTimeout').value = CFG.llm.timeoutMs;
  $('cfgRetries').value = CFG.llm.maxRetries;
  $('cfgExtra').value = CFG.analysis.extraContext || '';

  var h = $('keyStateHint');
  if (CFG.key.ready) {
    h.innerHTML = '当前密钥：<code>' + esc(CFG.key.masked) + '</code>　来源：' + esc(CFG.key.source);
  } else {
    h.innerHTML = esc(CFG.key.hint);
  }
}

function saveSettings() {
  var patch = {
    llm: {
      model: $('cfgModel').value.trim(),
      baseUrl: $('cfgBaseUrl').value.trim(),
      apiKey: $('cfgApiKey').value
    }
  };
  api('/api/config', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch)
  }).then(function (r) {
    CFG = r.config;
    $('cfgApiKey').value = '';
    fillSettings(); renderKeyPill(); renderKeyBanner();
    toast('已保存', 'ok');
  }).catch(function (e) { toast('保存失败：' + e.message, 'bad'); });
}

function saveRuntime() {
  api('/api/config', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      llm: { timeoutMs: Number($('cfgTimeout').value), maxRetries: Number($('cfgRetries').value) },
      analysis: { concurrency: Number($('cfgConcurrency').value), extraContext: $('cfgExtra').value }
    })
  }).then(function (r) {
    CFG = r.config; fillSettings(); toast('参数已保存', 'ok');
  }).catch(function (e) { toast('保存失败：' + e.message, 'bad'); });
}

function testModel() {
  var el = $('testResult');
  el.innerHTML = '<span class="spin"></span> 正在发请求…';
  api('/api/config/test', { method: 'POST' }).then(function (r) {
    if (r.ok) {
      el.innerHTML = '<span style="color:#86efac">连接成功</span> · ' + esc(r.model) +
        ' · 往返 ' + r.elapsedMs + 'ms';
      toast('模型可用', 'ok');
    } else if (r.stage === 'key') {
      el.innerHTML = '<span style="color:#fca5a5">未配置密钥</span>';
      toast('先填密钥再测', 'bad');
    } else {
      el.innerHTML = '<span style="color:#fca5a5">' + esc(r.message) + '</span>';
      toast('连接失败，看下方提示', 'bad');
    }
  }).catch(function (e) {
    el.innerHTML = '<span style="color:#fca5a5">' + esc(e.message) + '</span>';
  });
}

/* ---------------- 标的 ---------------- */

function renderPickChips() {
  var box = $('pickChips');
  if (!CFG || !CFG.analysis.codes.length) {
    box.innerHTML = '<span class="muted" style="font-size:12.5px">清单为空。去「标的」页加几个代码。</span>';
    return;
  }
  if (!PICKED.length) PICKED = CFG.analysis.codes.slice();
  /* 只保留仍在清单里的 */
  PICKED = PICKED.filter(function (c) { return CFG.analysis.codes.indexOf(c) >= 0; });

  box.innerHTML = CFG.analysis.codes.map(function (c) {
    var on = PICKED.indexOf(c) >= 0;
    return '<div class="chip' + (on ? ' on' : '') + '" data-code="' + esc(c) + '">' +
      '<span>' + esc(c) + '</span></div>';
  }).join('');

  box.querySelectorAll('.chip').forEach(function (el) {
    el.onclick = function () {
      var c = el.dataset.code;
      var i = PICKED.indexOf(c);
      if (i >= 0) PICKED.splice(i, 1); else PICKED.push(c);
      el.classList.toggle('on');
      $('runHint').textContent = PICKED.length ? '本次将分析 ' + PICKED.length + ' 只' : '至少选一只';
    };
  });
  $('runHint').textContent = '本次将分析 ' + PICKED.length + ' 只';
}

function renderWatchList() {
  var box = $('watchList');
  if (!CFG || !CFG.analysis.codes.length) {
    box.innerHTML = '<div class="muted" style="font-size:12.5px;padding:8px 0">还没有标的</div>';
    return;
  }
  box.innerHTML = '<div class="chips">' + CFG.analysis.codes.map(function (c) {
    return '<div class="chip"><span class="mono">' + esc(c) + '</span>' +
      '<span class="x" data-del="' + esc(c) + '" title="移除">×</span></div>';
  }).join('') + '</div>';

  box.querySelectorAll('[data-del]').forEach(function (el) {
    el.onclick = function () {
      var c = el.dataset.del;
      CFG.analysis.codes = CFG.analysis.codes.filter(function (x) { return x !== c; });
      renderWatchList();
      toast('已从清单移除，记得点保存');
    };
  });
}

function lookupCode() {
  var code = $('newCode').value.trim();
  var el = $('lookupResult');
  if (!/^\d{6}$/.test(code)) { el.textContent = '请输入 6 位数字代码'; return; }
  el.innerHTML = '<span class="spin"></span> 查询中…';
  api('/api/lookup?code=' + encodeURIComponent(code)).then(function (r) {
    if (!r.ok) { el.innerHTML = '<span style="color:#fca5a5">' + esc(r.message) + '</span>'; return; }
    var cls = r.changePct >= 0 ? 'var(--up)' : 'var(--down)';
    el.innerHTML = '<span style="color:var(--tx)">' + esc(r.name) + '</span> ' +
      '<span class="mono">' + r.price + '</span> ' +
      '<span class="mono" style="color:' + cls + '">' + (r.changePct >= 0 ? '+' : '') + r.changePct + '%</span>';
    var btn = document.createElement('button');
    btn.className = 'sm'; btn.textContent = '加入清单';
    btn.onclick = function () {
      if (CFG.analysis.codes.indexOf(code) >= 0) { toast('已经在清单里了'); return; }
      CFG.analysis.codes.push(code);
      renderWatchList();
      $('newCode').value = '';
      el.textContent = '已加入，记得点保存';
    };
    el.appendChild(document.createTextNode(' '));
    el.appendChild(btn);
  }).catch(function (e) {
    el.innerHTML = '<span style="color:#fca5a5">' + esc(e.message) + '</span>';
  });
}

function saveWatchlist() {
  api('/api/config', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ analysis: { codes: CFG.analysis.codes } })
  }).then(function (r) {
    CFG = r.config;
    PICKED = CFG.analysis.codes.slice();
    renderPickChips();
    toast('清单已保存', 'ok');
  }).catch(function (e) { toast('保存失败：' + e.message, 'bad'); });
}

function renderKeywordEditor() {
  if (!CFG) return;
  var kw = CFG.news.keywords || {};
  var lines = Object.keys(kw).map(function (k) {
    var v = kw[k];
    return k + ' ' + (Array.isArray(v) ? v.join(',') : v);
  });
  $('kwEditor').value = lines.join('\n');
}

function saveKeywords() {
  var lines = $('kwEditor').value.split(/\r?\n/);
  var kw = {};
  var bad = [];
  lines.forEach(function (ln) {
    ln = ln.trim();
    if (!ln) return;
    var m = ln.match(/^(\S+)\s+(.+)$/);
    if (!m) { bad.push(ln); return; }
    kw[m[1]] = m[2].split(/[,，]/).map(function (x) { return x.trim(); }).filter(Boolean);
  });
  if (bad.length) { toast('格式不对（要「代码 词1,词2」）：' + bad[0], 'bad'); return; }
  api('/api/config', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ news: { keywords: kw } })
  }).then(function (r) { CFG = r.config; toast('检索词已保存', 'ok'); })
    .catch(function (e) { toast('保存失败：' + e.message, 'bad'); });
}

/* ---------------- 分析 ---------------- */

function setProgressVisible(on) {
  $('progressCard').style.display = on ? 'block' : 'none';
}

function logLine(text, cls) {
  var box = $('progLog');
  var t = new Date();
  var ts = ('0' + t.getHours()).slice(-2) + ':' + ('0' + t.getMinutes()).slice(-2) + ':' + ('0' + t.getSeconds()).slice(-2);
  var d = document.createElement('div');
  d.className = 'l';
  d.innerHTML = '<span class="t">' + ts + '</span><span class="' + (cls || '') + '">' + esc(text) + '</span>';
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
}

function startAnalyze(noLlm) {
  if (ES) { toast('已经在跑了'); return; }
  if (!PICKED.length) { toast('先勾选至少一只标的', 'bad'); return; }

  $('btnRun').disabled = true;
  $('btnRunNoLlm').disabled = true;
  $('progLog').innerHTML = '';
  $('progBar').style.width = '0';
  $('progSpin').style.display = 'inline-block';
  $('progText').textContent = '正在取美股与宏观数据…';
  HAD_ERROR = false;
  setProgressVisible(true);
  $('resultArea').innerHTML = '<div class="empty"><div class="big">分析进行中…</div><div>结果会在完成后自动出现</div></div>';

  var total = PICKED.length;
  var done = 0;

  var qs = '/api/analyze?codes=' + encodeURIComponent(PICKED.join(',')) + '&mode=' + (noLlm ? 'no-llm' : 'llm');
  ES = new EventSource(qs);

  ES.onmessage = function (e) {
    var ev;
    try { ev = JSON.parse(e.data); } catch (err) { return; }

    if (ev.type === 'hello') {
      logLine('开始分析 ' + ev.codes.length + ' 只 · ' + (ev.mode === 'no-llm' ? '不调模型' : ev.model), 'info');
    } else if (ev.type === 'market') {
      var us = (ev.usIndices || []).map(function (x) { return x.name + ' ' + x.changePct + '%'; }).join('、');
      logLine('美股：' + (us || '无数据'), 'info');
      if (ev.adr && ev.adr.length) {
        logLine('中概：' + ev.adr.slice(0, 4).map(function (x) { return x.name + ' ' + x.changePct + '%'; }).join('、'), 'info');
      }
      $('progText').textContent = '正在逐只分析…';
    } else if (ev.type === 'item-start') {
      logLine('[' + ev.index + '/' + ev.total + '] ' + ev.code + ' 取数中…');
      $('progText').textContent = '正在分析 ' + ev.code + '（' + ev.index + '/' + ev.total + '）';
    } else if (ev.type === 'attempt') {
      logLine('    模型第 ' + ev.attempt + ' 次：' + (ev.ok ? '成功 ' + ev.elapsedMs + 'ms' : '失败 ' + ev.error),
        ev.ok ? 'ok' : 'err');
    } else if (ev.type === 'item-done') {
      done++;
      $('progBar').style.width = Math.round(done / total * 100) + '%';
      var cov = ev.coverage ? ev.coverage.covered + '/' + ev.coverage.total : '';
      if (ev.ok) {
        logLine('  ' + ev.name + '：' + ev.verdict + '｜确信度 ' + (ev.conviction || 0) + '｜' + cov + '｜' + ev.elapsedMs + 'ms', 'ok');
      } else {
        logLine('  ' + ev.name + ' 降级：' + (ev.reason || '未知原因'), 'err');
      }
    } else if (ev.type === 'done') {
      logLine('完成：成功 ' + ev.summary.succeeded + ' / ' + ev.summary.total, 'ok');
      $('progText').textContent = '完成，正在渲染结果…';
    } else if (ev.type === 'archived') {
      if (ev.file) logLine('已存档：' + ev.file);
    } else if (ev.type === 'quality') {
      /* SSE 新增的可信度事件：只往已有进度日志里追加文字，不改任何 DOM 结构 */
      var qBad = ev.status === 'degraded' || ev.status === 'failed';
      logLine('数据可信度：' + (ev.status || '未知') + ' · 已核对 ' + (ev.checked || 0) +
        ' 项 · 一致 ' + (ev.matched || 0) + ' · 冲突 ' + (ev.mismatched || 0) +
        ' · 未核对 ' + (ev.unverified || 0) +
        (ev.sampleNote ? '（' + ev.sampleNote + '）' : '') +
        (ev.message ? ' · ' + ev.message : ''), qBad ? 'err' : 'info');
      if (ev.tolerance) {
        logLine('  对账容差：' + (ev.tolerance.source === 'calibrated'
          ? '真实样本校准' : '内置经验值（未校准）'), 'info');
      }
      (ev.conflicts || []).slice(0, 3).forEach(function (c) {
        var pp = c.primary || {}, ss = c.secondary || {};
        logLine('  冲突 · ' + c.label + '：' + (pp.source || '主源') + ' ' + pp.value +
          ' ／ ' + (ss.source || '第二源') + ' ' + ss.value + '（差 ' + c.delta + '）', 'err');
      });
    } else if (ev.type === 'error') {
      HAD_ERROR = true;
      logLine('出错：' + ev.message, 'err');
      $('progSpin').style.display = 'none';
      $('progText').innerHTML = '<span style="color:#fca5a5">' + esc(ev.message) + '</span>';
      if (ev.stage === 'key') {
        logLine('去「设置」页填密钥，或改用「不调模型，只跑数据」。', 'info');
      }
    }
  };

  ES.onerror = function () {
    /* 服务端正常结束也会触发，统一交给 finishAnalyze 收尾 */
    finishAnalyze();
  };
}

function finishAnalyze() {
  if (!ES) return;
  ES.close();
  ES = null;
  $('btnRun').disabled = false;
  $('btnRunNoLlm').disabled = false;
  $('progSpin').style.display = 'none';

  if (HAD_ERROR) {
    /* 出错时保留进度卡片，让用户能读到原因，而不是一闪而过 */
    $('progText').textContent = '已中止，原因见下方日志';
    setTimeout(function () { setProgressVisible(false); }, 8000);
    return;
  }

  $('progText').textContent = '已结束';
  setTimeout(function () {
    setProgressVisible(false);
    loadLatest();
  }, 400);
}

function loadLatest() {
  api('/api/latest').then(function (r) {
    if (!r.payload) {
      $('resultArea').innerHTML = '<div class="empty"><div class="big">还没有分析结果</div>' +
        '<div>选好标的点「开始分析」</div></div>';
      return;
    }
    renderPayload(r.payload);
  }).catch(function (e) {
    $('resultArea').innerHTML = '<div class="banner bad">读取结果失败：' + esc(e.message) + '</div>';
  });
}

/* ---------------- 结果渲染 ---------------- */

function verdictClass(v) {
  var map = { '买入': 'v-买入', '加仓': 'v-加仓', '持有': 'v-持有', '减仓': 'v-减仓', '观望': 'v-观望' };
  return map[v] || 'v-观望';
}

function deltaClass(n) {
  if (!n && n !== 0) return 'var(--tx-2)';
  return n >= 0 ? 'var(--up)' : 'var(--down)';
}

function renderPayload(p) {
  var head = '<div class="row between" style="margin-bottom:14px">' +
    '<div><h2 style="margin:0">分析结果</h2>' +
    '<div class="muted" style="font-size:12px;margin-top:2px">' +
    esc(p.generatedAtText || '') + ' · ' + esc((p.model && p.model.name) || '') +
    ' · 成功 ' + (p.summary ? p.summary.succeeded : 0) + '/' + (p.summary ? p.summary.total : 0) +
    (p.summary && p.summary.degraded ? ' · 降级 ' + p.summary.degraded : '') +
    '</div></div>' +
    '<button class="sm" onclick="exportMd()">导出报告</button></div>';

  var cards = (p.results || []).map(renderResult).join('');
  $('resultArea').innerHTML = head + cards;

  var resEls = $('resultArea').querySelectorAll('.res');
  for (var i = 0; i < resEls.length; i++) {
    (function (el) {
      el.querySelector('.res-head').onclick = function () { el.classList.toggle('open'); };
    })(resEls[i]);
  }
  /* 默认展开第一张，免得用户以为只有这几行摘要 */
  if (resEls.length) resEls[0].classList.add('open');
}

function renderResult(r) {
  var a = r.analysis || {};
  var f = r.factors || {};
  var cov = f.coverage || {};
  var deg = a._degraded;

  var covPill = cov.covered + '/' + cov.total;
  var covCls = cov.covered === cov.total ? 'ok' : (cov.covered >= 3 ? 'warn' : 'bad');

  var html = '<div class="res' + (r.ok ? '' : '') + '">';

  /* 头部 */
  html += '<div class="res-head">' +
    '<div><div class="res-name">' + esc(r.name || r.symbol) + ' <span class="res-code">' + esc(r.symbol) + '</span></div>' +
    '<div class="muted" style="font-size:11.5px">' + esc(a.dataAsOf || '') + '</div></div>' +
    '<div class="res-meta">' +
    (deg ? '<span class="tag warn">规则降级</span>' : '') +
    '<span class="verdict ' + verdictClass(a.verdict) + '">' + esc(a.verdict || '—') + '</span>' +
    '<span class="muted" style="font-size:12px">确信度 <span class="mono">' + (a.conviction || 0) + '</span></span>' +
    '<span class="pill ' + covCls + '" style="font-size:10.5px">数据 ' + covPill + '</span>' +
    '<span class="muted" style="font-size:11.5px">' + (r.elapsedMs || 0) + 'ms</span>' +
    '<span class="caret"></span>' +
    '</div></div>';

  /* 身体 */
  html += '<div class="res-body">';

  /* 降级原因 */
  if (deg) {
    html += '<div class="banner warn" style="margin-top:14px;margin-bottom:0">' +
      '<div><strong>这不是模型结论。</strong>' + esc(a._degradedReason || '') +
      '<br>下方数字都是真实采集的，但结论由本机规则拼出，不要据此调仓。</div></div>';
  }

  /* 仓位建议 */
  if (a.positionAdvice) {
    html += '<div class="sec"><div class="sec-t">建议仓位</div>' +
      '<div class="advice">' + esc(a.positionAdvice) + '</div></div>';
  }

  /* 关键价位 */
  var kl = a.keyLevels || {};
  var hasKl = kl.stopLoss || kl.takeProfit || kl.support || kl.resistance;
  if (hasKl) {
    html += '<div class="sec"><div class="sec-t">关键价位' +
      '<span class="help" data-tip="止损＝跌破就该认错的价；止盈＝到了该考虑的价；支撑/压力＝近期高低点。" style="margin-left:6px">?</span>' +
      '</div><div class="kv">' +
      kvCell('止损 / 失效', kl.stopLoss, 'var(--down)') +
      kvCell('止盈 / 目标', kl.takeProfit, 'var(--up)') +
      kvCell('支撑', kl.support, '') +
      kvCell('压力', kl.resistance, '') +
      '</div></div>';
  }

  /* 触发 / 失效 */
  if ((a.triggers && a.triggers.length) || (a.invalidations && a.invalidations.length)) {
    html += '<div class="sec"><div class="sec-t">触发与失效' +
      '<span class="help" data-tip="触发条件＝满足才动手；失效条件＝出现就作废这个结论。后者比前者更该盯。" style="margin-left:6px">?</span>' +
      '</div>';
    if (a.triggers && a.triggers.length) {
      html += '<div style="font-size:12px;color:var(--up);margin-bottom:6px">触发</div><ul class="ev" style="margin-bottom:12px">' +
        a.triggers.map(function (t) {
          return '<li><div class="ev-o">' + esc(t.condition) + '</div>' +
            (t.value ? '<div class="ev-i mono" style="font-size:11.5px">' + esc(t.value) + '</div>' : '') + '</li>';
        }).join('') + '</ul>';
    }
    if (a.invalidations && a.invalidations.length) {
      html += '<div style="font-size:12px;color:var(--down);margin-bottom:6px">失效</div><ul class="ev">' +
        a.invalidations.map(function (t) {
          return '<li><div class="ev-o">' + esc(t.condition) + '</div>' +
            (t.value ? '<div class="ev-i mono" style="font-size:11.5px">' + esc(t.value) + '</div>' : '') + '</li>';
        }).join('') + '</ul>';
    }
    html += '</div>';
  }

  /* 五维打分 */
  var ds = a.dimensionScores || {};
  var dimLabels = { fundamental: '基本面', capital: '资金面', technical: '技术面', sentiment: '情绪面', macro: '宏观政策' };
  var dimBasis = {};
  (f.dims || []).forEach(function (d) {
    if (!d.available) dimBasis[d.key] = '数据不可用';
    else if (d.key === 'technical' && d.technical) dimBasis[d.key] = d.technical.trend || '';
    else dimBasis[d.key] = (d.items && d.items.length) ? d.items.length + ' 项因子' : '';
  });
  html += '<div class="sec"><div class="sec-t">五维打分' +
    '<span class="help" data-tip="每维度 -50 到 +50。分值为负不代表看空，只表示该维度当前偏弱。空白说明该维度没取到数据，记 0 分（中性）。" style="margin-left:6px">?</span>' +
    '</div><div class="dims">';
  Object.keys(dimLabels).forEach(function (k) {
    var v = Number(ds[k] || 0);
    var w = Math.min(Math.abs(v), 50) / 50 * 50;
    var left = v >= 0 ? 50 : 50 - w;
    var cls = v === 0 ? 'zero' : (v > 0 ? 'pos' : 'neg');
    html += '<div class="dim"><div class="dn">' + dimLabels[k] + '</div>' +
      '<div class="db"><i class="' + cls + '" style="left:' + left + '%;width:' + (v === 0 ? '' : w + '%') + '"></i></div>' +
      '<div class="dv">' + (v > 0 ? '+' : '') + v + '</div>' +
      '<div class="ds">' + esc(dimBasis[k] || '') + '</div></div>';
  });
  html += '</div></div>';

  /* 依据链条 */
  if (a.evidenceChain && a.evidenceChain.length) {
    html += '<div class="sec"><div class="sec-t">依据链条 · 因子 → 结论 → 建议' +
      '<span class="help" data-tip="每条都是「观测到什么 → 指向什么」。观测里必须有具体数值和时间，没有数值的条目可以直接忽略。" style="margin-left:6px">?</span>' +
      '</div><ul class="ev">' +
      a.evidenceChain.map(function (e) {
        return '<li><div class="ev-d">[' + esc(e.dimension || '') + '] ' + esc(e.factor || '') + '</div>' +
          '<div class="ev-o">' + esc(e.observation || '') + '</div>' +
          '<div class="ev-i">' + esc(e.implication || '') + '</div></li>';
      }).join('') + '</ul></div>';
  }

  /* 中美联动 */
  var link = f.usChinaLink || {};
  if ((link.relations && link.relations.length) || a.usChinaRelation) {
    html += '<div class="sec"><div class="sec-t">中美联动' +
      '<span class="help" data-tip="用美股隔夜表现判断今天是跟外盘走还是走自己的逻辑。方向相反＝背离，别按外盘外推。" style="margin-left:6px">?</span>' +
      '</div>';
    if (link.relations && link.relations.length) {
      html += link.relations.map(function (rel) {
        return '<div style="margin-bottom:8px;font-size:12.5px">' +
          '<span class="tag' + (rel.type === '背离' ? ' warn' : '') + '" style="margin-right:7px">' + esc(rel.type) + '</span>' +
          '<span style="color:var(--tx-2)">' + esc(rel.text) + '</span></div>';
      }).join('');
    } else if (a.usChinaRelation) {
      html += '<div style="font-size:12.5px;color:var(--tx-2)">' + esc(a.usChinaRelation) + '</div>';
    }
    if (link.channels && link.channels.length) {
      html += '<details class="note" style="margin-top:10px"><summary>四条传导路径</summary>' +
        link.channels.map(function (c) {
          return '<p><strong>' + esc(c.name) + '</strong>：' + esc(c.text) + '</p>';
        }).join('') + '</details>';
    }
    html += '</div>';
  }

  /* 风险与情景 */
  if ((a.risks && a.risks.length) || (a.scenarios && a.scenarios.length)) {
    html += '<div class="sec"><div class="sec-t">风险与情景</div>';
    if (a.risks && a.risks.length) {
      html += '<div class="tags" style="margin-bottom:10px">' +
        a.risks.map(function (x) { return '<span class="tag bad">' + esc(x) + '</span>'; }).join('') + '</div>';
    }
    if (a.scenarios && a.scenarios.length) {
      html += '<table class="tb"><tr><th>情景</th><th>条件</th><th>影响</th><th>应对</th></tr>' +
        a.scenarios.map(function (s) {
          return '<tr><td>' + esc(s.scenario) + '</td><td>' + esc(s.condition) + '</td>' +
            '<td>' + esc(s.impact) + '</td><td>' + esc(s.action) + '</td></tr>';
        }).join('') + '</table>';
    }
    html += '</div>';
  }

  /* 数据完整度 */
  html += '<div class="sec"><div class="sec-t">数据完整度</div>';
  html += '<div style="font-size:12.5px;color:var(--tx-2);margin-bottom:8px">' +
    '<span class="mono">' + covPill + '</span> 个维度可用' +
    ((cov.notApplicableDims && cov.notApplicableDims.length)
      ? '　<span class="tag">' + esc(cov.notApplicableDims.join('、')) + ' 维度不适用</span>' : '') +
    '</div>';

  if (cov.missing && cov.missing.length) {
    html += '<div class="tags" style="margin-bottom:8px">' +
      cov.missing.map(function (m) { return '<span class="tag warn">缺 ' + esc(m) + '</span>'; }).join('') + '</div>';
  } else {
    html += '<div class="tags" style="margin-bottom:8px"><span class="tag ok">没有真实数据缺口</span></div>';
  }

  if (cov.notApplicable && cov.notApplicable.length) {
    html += '<details class="note"><summary>不适用项（' + cov.notApplicable.length + '）—— 不是数据缺失</summary>' +
      '<ul>' + cov.notApplicable.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>' +
      '<p>ETF 这类标的没有财务报表、PE/PB、北向持股，是它本来就不具备，不算数据质量差。</p></details>';
  }

  (f.dims || []).forEach(function (d) {
    if (!d.available && d.missing && d.missing.length) return;
    var rows = '';
    if (d.key === 'technical' && d.technical) {
      var t = d.technical;
      rows = '<tr><td>趋势</td><td>' + esc(t.trend) + '</td><td>' + esc((t.trendBasis || []).join('；')) + '</td></tr>' +
        '<tr><td>均线</td><td class="n">MA5 ' + t.ma5 + ' / MA20 ' + t.ma20 + '</td><td>' + esc(t.pattern) + '，' + esc(t.patternNote || '') + '</td></tr>' +
        (t.denseZone ? '<tr><td>成交密集区</td><td class="n">' + t.denseZone.lower + '–' + t.denseZone.upper + '</td><td>占近 20 日成交量 ' + t.denseZone.sharePct + '%，是主要持仓成本区</td></tr>' : '') +
        '<tr><td>区间表现</td><td class="n">5日 ' + pct(t.ret5) + '｜20日 ' + pct(t.ret20) + '｜60日 ' + pct(t.ret60) + '</td><td>年化波动率 ' + t.volatility + '%</td></tr>' +
        (t.indicatorTrend ? '<tr><td>指标共振</td><td class="n">' + esc(t.indicatorTrend) + '</td><td>MACD/KDJ/RSI/BOLL/DMA 五指标多空统计（偏多加分、偏空减分，仅供参考）</td></tr>' : '') +
        '<tr><td>数据源</td><td colspan="2">' + esc(t.source || '') + '，样本 ' + esc(t.bars) + ' 根，截止 ' + esc(t.asOf) + '</td></tr>';
      var indHtml = '';
      if (t.indicators && t.indicators.ok && t.indicators.signals && t.indicators.signals.length) {
        indHtml = '<details class="note" style="margin-top:8px"><summary>技术指标明细（' + t.indicators.signals.length + ' 项）</summary>' +
          '<table class="tb"><tr><th>指标</th><th>状态</th><th>数值与解读</th></tr>' +
          t.indicators.signals.map(function (s) {
            return '<tr><td>' + esc(s.factor) + '</td><td>' + esc(s.value) + '</td><td>' + esc(s.detail) + '</td></tr>';
          }).join('') + '</table></details>';
      }
      if (rows) {
        html += '<details class="note" open><summary>' + esc(d.label) + ' 明细</summary>' +
          '<table class="tb"><tr><th>因子</th><th>数值</th><th>说明与来源</th></tr>' + rows + '</table>' + indHtml + '</details>';
      }
      return;
    } else if (d.items && d.items.length) {
      rows = d.items.map(function (it) {
        return '<tr><td>' + esc(it.factor) + '</td><td class="n">' + esc(it.value) + '</td>' +
          '<td>' + esc(it.detail) + '<br><span class="muted" style="font-size:11px">' +
          esc(it.asOf || '时间未取得') + ' · ' + esc(it.source || '') + '</span></td></tr>';
      }).join('');
    }
    if (rows) {
      html += '<details class="note"><summary>' + esc(d.label) + ' 明细</summary>' +
        '<table class="tb"><tr><th>因子</th><th>数值</th><th>说明与来源</th></tr>' + rows + '</table></details>';
    }
  });

  html += '</div>';

  /* 免责 */
  if (a.disclaimer) {
    html += '<div class="muted" style="font-size:11.5px;line-height:1.6;padding-top:12px;border-top:1px solid var(--line)">' +
      esc(a.disclaimer) + '</div>';
  }

  html += '</div></div>';
  return html;
}

function pct(v) {
  if (v === null || v === undefined || !isFinite(Number(v))) return '—';
  var n = Number(v);
  return (n >= 0 ? '+' : '') + n + '%';
}

function kvCell(k, v, color) {
  var val = (v === null || v === undefined || v === '') ? '—' : v;
  return '<div><div class="k">' + esc(k) + '</div><div class="v" style="' +
    (color && val !== '—' ? 'color:' + color : '') + '">' + esc(val) + '</div></div>';
}

function exportMd() {
  window.location.href = '/api/export';
}

/* ---------------- 记录 ---------------- */

function loadHistory() {
  var box = $('historyList');
  box.innerHTML = '<div class="empty"><span class="spin"></span></div>';
  api('/api/history').then(function (r) {
    if (!r.items || !r.items.length) {
      box.innerHTML = '<div class="empty"><div class="big">还没有记录</div><div>每次分析会自动存档在这里</div></div>';
      return;
    }
    box.innerHTML = r.items.map(function (it, i) {
      var h = it.head || {};
      var vs = h.summary && h.summary.verdicts ? Object.keys(h.summary.verdicts).map(function (k) {
        return k + ' ' + h.summary.verdicts[k];
      }).join(' · ') : '';
      var names = (h.names || []).join('、');
      return '<div class="card tight" style="margin-bottom:10px">' +
        '<div class="row between">' +
        '<div><div style="font-size:13px">' + esc(h.generatedAtText || it.file) + '</div>' +
        '<div class="muted" style="font-size:11.5px">' + esc(names) + (vs ? ' · ' + esc(vs) : '') + '</div></div>' +
        '<div class="row">' +
        '<span class="muted" style="font-size:11.5px">' + esc(h.model || '') + '</span>' +
        '<button class="sm" onclick="viewHistory(\'' + esc(it.file) + '\')">查看</button>' +
        '<button class="sm" onclick="location.href=\'/api/export?file=' + encodeURIComponent(it.file) + '\'">导出</button>' +
        '</div></div></div>';
    }).join('');
  }).catch(function (e) {
    box.innerHTML = '<div class="banner bad">' + esc(e.message) + '</div>';
  });
}

function viewHistory(file) {
  api('/api/history/item?file=' + encodeURIComponent(file)).then(function (r) {
    go('analyze');
    renderPayload(r.payload);
    window.scrollTo(0, 0);
  }).catch(function (e) { toast('读取失败：' + e.message, 'bad'); });
}

/* ---------------- 启动 ---------------- */

function tick() {
  var d = new Date();
  $('clock').textContent = ('0' + d.getHours()).slice(-2) + ':' +
    ('0' + d.getMinutes()).slice(-2) + ':' + ('0' + d.getSeconds()).slice(-2);
}
setInterval(tick, 1000); tick();

loadConfig().then(function () {
  /* 支持 #settings 这类直达地址，刷新后停在同一页 */
  var initView = (location.hash || '').replace('#', '');
  if (initView && document.getElementById('view-' + initView)) go(initView);

  /* 支持 ?code=XXXXXX 从仪表盘/策略/行业页直达带入 */
  try {
    var pm = new URLSearchParams(location.search);
    var dc = (pm.get('code') || '').trim();
    if (/^\d{6}$/.test(dc)) {
      var ni = document.getElementById('newCode');
      if (ni) {
        ni.value = dc;
        if (!initView) go('watch');   // 跳到「标的」页方便查看/加入清单
        lookupCode();
      }
    }
  } catch (e) {}

  loadLatest();
  $('newCode').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') lookupCode();
  });
});
