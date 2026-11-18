/* nav.js — 顶部导航（所有页面共用），并探测伴侣服务在线状态 */
(function () {
  var PAGES = [
    { key: 'dashboard', label: '仪表盘', href: 'index.html' },
    { key: 'analysis', label: '个股深度分析', href: 'analysis.html' },
    { key: 'strategy', label: '策略扫描', href: 'strategy.html' },
    { key: 'portfolio', label: '持仓分析', href: 'portfolio.html' },
    { key: 'us', label: '美股联动', href: 'us.html' },
    { key: 'etf', label: 'ETF监控', href: 'etf.html' },
    { key: 'industry', label: '行业强度', href: 'industry.html' },
    { key: 'picks', label: '智能选股', href: 'picks.html' },
    { key: 'factors', label: '因子体检', href: 'factors.html' }
  ];

  window.MiloNav = {
    mount: function (current) {
      var nav = document.createElement('nav');
      nav.className = 'topnav';
      var brand = document.createElement('div');
      brand.className = 'brand';
      brand.innerHTML = '<span class="logo">📈</span><span>A 股分析终端</span>';
      nav.appendChild(brand);

      PAGES.forEach(function (p) {
        var a = document.createElement('a');
        a.className = 'navlink' + (p.key === current ? ' active' : '');
        a.href = p.href;
        a.textContent = p.label;
        nav.appendChild(a);
      });

      var sp = document.createElement('div'); sp.className = 'spacer'; nav.appendChild(sp);
      var st = document.createElement('div'); st.className = 'status';
      st.innerHTML = '<span class="dot"></span><span id="navStatus">策略引擎: 检查中</span>';
      nav.appendChild(st);

      document.body.insertBefore(nav, document.body.firstChild);

      fetch('/api/sequoia/api/health')
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var el = document.getElementById('navStatus');
          var dot = nav.querySelector('.status .dot');
          var on = !!(d && d.ok);
          if (el) el.textContent = on ? '策略引擎: 已连接 (' + (d.symbols || 0) + ' 标的)' : '策略引擎: 离线';
          if (dot) dot.className = 'dot' + (on ? ' ok' : '');
        })
        .catch(function () {
          var el = document.getElementById('navStatus');
          if (el) el.textContent = '策略引擎: 离线';
        });
    }
  };
})();
