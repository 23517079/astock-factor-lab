/* public/quality.js — 数据可信度状态条（策略 / 美股联动 / 持仓三页共用） */
(function () {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function fmt(v) {
    if (v == null || v === '') return '—';
    if (typeof v === 'number' && isFinite(v)) {
      return Math.abs(v) >= 1000
        ? v.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
        : String(Math.round(v * 10000) / 10000);
    }
    return String(v);
  }

  function render(el, d) {
    var t = d.totals || {};
    var scopes = d.scopes || {};
    var conflicts = [];
    Object.keys(scopes).forEach(function (k) {
      (scopes[k].conflicts || []).forEach(function (c) { conflicts.push(c); });
    });
    var bad = conflicts.length > 0;
    var color = bad ? '#b42318' : '#0f6e56';

    el.style.cssText = 'margin:0 0 16px;padding:10px 14px;border-radius:12px;' +
      'background:#f8fafd;border:1px solid var(--m-line,#e6ebf2);' +
      'color:var(--m-tx2,#5b6472);font-size:12.5px;line-height:1.7;display:block';

    var btnCss = 'font-size:11.5px;padding:3px 10px;border:1px solid var(--m-line2,#d8e0ea);' +
      'border-radius:8px;background:#fff;color:var(--m-tx2,#5b6472);cursor:pointer';
    var html = '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">' +
      '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' + color + '"></span>' +
      '<b style="color:var(--m-tx1,#1c2430);font-weight:600">数据可信度</b>' +
      '<span>已核对 <b>' + (t.checked || 0) + '</b> 项 · 一致 <b style="color:#0f6e56">' +
      (t.matched || 0) + '</b> · 冲突 <b style="color:' + (bad ? '#b42318' : 'inherit') + '">' +
      (t.mismatched || 0) + '</b> · 未核对 <b>' + (t.unverified || 0) + '</b></span>' +
      '<button id="mqToggle" type="button" style="' + btnCss + '">详细</button>' +
      '<span style="font-size:11px;color:var(--m-tx3,#8a93a1)">' +
      esc(d.asOf ? String(d.asOf).replace('T', ' ').slice(0, 19) : '尚未运行') + '</span></div>';

    if (bad) {
      html += '<div style="margin-top:6px;color:#b42318">两源数值不一致：页面保留主源数字，' +
        '展开可看到两家的具体数值与差值，请自行判断。</div>';
    }
    html += '<div id="mqDetail" style="display:none;margin-top:10px;border-top:1px solid ' +
      'var(--m-line,#e6ebf2);padding-top:10px">';
    if (conflicts.length) {
      html += '<div style="margin-bottom:8px"><b>冲突明细</b></div>';
      conflicts.slice(0, 20).forEach(function (c) {
        html += '<div style="margin:4px 0">· ' + esc(c.label) + '：' +
          esc((c.primary || {}).source || '主源') + ' <b>' + fmt((c.primary || {}).value) + '</b> ／ ' +
          esc((c.secondary || {}).source || '第二源') + ' <b>' + fmt((c.secondary || {}).value) +
          '</b>（差 ' + fmt(c.delta) + '）</div>';
      });
    }
    html += '<div style="margin:10px 0 6px"><b>覆盖清单</b></div>';
    (d.catalog || []).forEach(function (c) {
      var tag = c.kind === 'independent' ? '双源核对'
        : (c.kind === 'single-source' ? '单源·未核对' : '新鲜度校验');
      html += '<div style="margin:3px 0">· ' + esc(c.label) +
        ' <span style="color:#8a93a1">[' + tag + ']</span> ' + esc(c.primary) +
        (c.secondary ? ' ↔ ' + esc(c.secondary) : '') + '</div>';
    });
    /* 行情时点错位：有第二源但不可比，既不算一致也不算冲突，单独说明 */
    var skewScopes = Object.keys(scopes).filter(function (k) { return (scopes[k].timeSkew || 0) > 0; });
    if (skewScopes.length) {
      var skewTotal = 0, skewReason = '';
      skewScopes.forEach(function (k) {
        skewTotal += scopes[k].timeSkew || 0;
        (scopes[k].items || []).forEach(function (it) {
          if (!skewReason && it.comparability === 'time-skew') skewReason = it.reason;
        });
      });
      html += '<div style="margin:10px 0 4px;color:#b42318"><b>行情时点错位</b>：' +
        skewTotal + ' 项涨跌幅未做一致性判定（不计入已验证，也不算冲突）</div>' +
        '<div style="color:var(--m-tx3,#8a93a1)">· ' + esc(skewReason || '两源行情时点不同') + '</div>';
    }
    html += '<div style="margin-top:8px;color:var(--m-tx3,#8a93a1)">' + esc(d.note || '') + '</div>';
    if (d.tolerance) {
      var tv = d.tolerance.values || {};
      html += '<div style="margin-top:6px;color:var(--m-tx3,#8a93a1)">对账容差：' +
        esc(d.tolerance.source === 'calibrated' ? '真实样本校准' : '内置经验值（未校准）') +
        ' · 现价 ' + esc(String(tv.priceAbs)) + ' 元 / 相对 ' + esc(String(tv.priceRel)) +
        ' / 上限 ' + esc(String(tv.priceMax)) + ' 元 · 涨跌幅 ' + esc(String(tv.pctAbs)) + ' 个百分点' +
        (d.tolerance.sample ? '（' + esc(String(d.tolerance.sample)) + ' 条跨源观测）' : '') +
        (d.tolerance.caveats && d.tolerance.caveats.length
          ? '<div style="margin-top:4px">· ' + esc(d.tolerance.caveats[1] || d.tolerance.caveats[0]) + '</div>'
          : '') +
        '</div>';
    }
    html += '</div>';
    el.innerHTML = html;

    var btn = document.getElementById('mqToggle');
    if (btn) {
      btn.onclick = function () {
        var box = document.getElementById('mqDetail');
        var show = box.style.display === 'none';
        box.style.display = show ? 'block' : 'none';
        btn.textContent = show ? '收起' : '详细';
      };
    }
  }

  function mount(id) {
    var el = document.getElementById(id);
    if (!el) return;
    el.style.cssText = 'margin:0 0 16px;padding:10px 14px;border-radius:12px;' +
      'background:#f8fafd;border:1px solid var(--m-line,#e6ebf2);' +
      'color:var(--m-tx2,#5b6472);font-size:12.5px;display:block';
    el.textContent = '正在核对数据源…';
    fetch('/api/data-quality').then(function (r) { return r.json(); })
      .then(function (d) { render(el, d); })
      .catch(function (e) { el.textContent = '数据可信度检查不可用：' + e.message; });
  }

  window.MiloQuality = { mount: mount, render: render };
})();
