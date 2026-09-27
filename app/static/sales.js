/* Sales report: two charts (Chart.js, only if app/static/chart.umd.min.js is present)
 * and a sortable menu table. No innerHTML anywhere: rows are moved, never rebuilt. */
(function () {
  'use strict';

  var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var dataEl = document.getElementById('sales-data');
  var data = dataEl ? JSON.parse(dataEl.textContent) : null;

  /* ---------- charts ---------- */
  // Single series each, so no legend: the heading names the chart. Colour: brass (the app's
  // primary accent), validated against the wine surface. Axis text in the muted rose-grey.
  var SERIES = '#b8892a';       // brass, one step deeper: passes the chart lightness band + 3:1 on the wine surface
  var INK = '#cdb8bd';          // secondary text
  var GRID = 'rgba(243, 231, 211, 0.09)';

  function rupees(paise) {
    return '₹' + (paise / 100).toLocaleString('en-IN', { maximumFractionDigits: 0 });
  }

  function baseOptions() {
    return {
      responsive: true,
      maintainAspectRatio: false,  // the .chart-box sets the height; width follows the phone
      resizeDelay: 100,            // rotate/resize on mobile without thrashing
      animation: animateOnce ? { duration: 500, easing: 'easeOutCubic' } : false,  // once, on first draw
      layout: { padding: { top: 12, right: 12, left: 4 } },  // room so edge points aren't clipped
      interaction: { mode: 'index', intersect: false },   // crosshair-style hover
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: function (ctx) { return rupees(ctx.parsed.y); } } }
      },
      scales: {
        x: { ticks: { color: INK, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 }, grid: { display: false } },
        y: { beginAtZero: true, grace: '20%',  // ~20% headroom above the tallest point/bar
             ticks: { color: INK, callback: function (v) { return rupees(v); } },
             grid: { color: GRID }, border: { display: false } }
      }
    };
  }

  // Never stack two charts on one canvas: destroy whatever is there, then draw.
  // (Presets are separate page loads; this also covers back/forward-cache restores.)
  function draw(canvas, config) {
    var existing = window.Chart.getChart(canvas);
    if (existing) existing.destroy();
    return new window.Chart(canvas, config);
  }

  var animateOnce = !reduceMotion;

  function renderCharts() {
    if (!data || !window.Chart) return;
    var daily = document.getElementById('daily-chart');
    if (daily) {
      draw(daily, {
        type: 'line',
        data: {
          labels: data.days.map(function (d) { return d.slice(5); }),  // MM-DD
          datasets: [{ data: data.daily, borderColor: SERIES, backgroundColor: SERIES, clip: false,
                       // Today isn't over: dash the segment into it so a low value doesn't read as a crash
                       segment: { borderDash: function (ctx) {
                         return data.lastDayInProgress && ctx.p1DataIndex === data.daily.length - 1 ? [4, 4] : undefined;
                       } },
                       borderWidth: 2, pointRadius: data.daily.length > 40 ? 0 : 4, pointHoverRadius: 6,
                       tension: 0 }]
        },
        options: (function () {
          var o = baseOptions();
          o.scales.x.offset = data.daily.length < 4;  // few points: keep them off the chart edges
          o.plugins.tooltip.callbacks.label = function (ctx) {
            var today = data.lastDayInProgress && ctx.dataIndex === data.daily.length - 1;
            return rupees(ctx.parsed.y) + (today ? ' (today, so far)' : '');
          };
          return o;
        })()
      });
    }
    var hours = document.getElementById('hours-chart');
    if (hours) {
      var opts = baseOptions();
      opts.plugins.tooltip.callbacks.afterLabel = function (ctx) {
        var n = data.hourBills[ctx.dataIndex];
        return n + (n === 1 ? ' bill' : ' bills');
      };
      draw(hours, {
        type: 'bar',
        data: {
          labels: data.hours.map(function (_, h) { return (h < 10 ? '0' : '') + h; }),
          datasets: [{ data: data.hours, backgroundColor: SERIES, borderRadius: { topLeft: 4, topRight: 4 },
                       borderSkipped: 'bottom', maxBarThickness: 28 }]
        },
        options: opts
      });
    }
  }
  renderCharts();
  animateOnce = false;  // redraws (back/forward cache) appear instantly
  window.addEventListener('pageshow', function (e) { if (e.persisted) renderCharts(); });

  /* ---------- the four headline figures count up once on load ---------- */
  function countUp(el) {
    var finalText = el.textContent;
    var m = finalText.match(/^(-?)₹([\d,]+)\.(\d{2})$/);
    if (!m) return;
    var target = Number(m[2].replace(/,/g, '')) + Number(m[3]) / 100;
    var sign = m[1];
    var start = null;
    function frame(ts) {
      if (start === null) start = ts;
      var t = Math.min(1, (ts - start) / 600);
      var eased = 1 - Math.pow(1 - t, 3);
      el.textContent = sign + '₹' + (target * eased).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      if (t < 1 && !done) requestAnimationFrame(frame); else el.textContent = finalText;
    }
    var done = false;
    requestAnimationFrame(frame);
    // Money must never be left mid-count: animation frames pause in background tabs and
    // may not run at all, so the exact server figure is always restored on a timer too.
    setTimeout(function () { done = true; el.textContent = finalText; }, 700);
  }
  if (!reduceMotion) document.querySelectorAll('.stats strong').forEach(countUp);

  /* ---------- sortable menu table ---------- */
  var table = document.getElementById('menu-table');
  if (!table) return;
  var tbody = table.tBodies[0];
  table.querySelectorAll('th').forEach(function (th, col) {
    th.tabIndex = 0;
    th.setAttribute('aria-sort', 'none');
    function sort() {
      var numeric = th.dataset.sortType === 'num';
      var desc = th.getAttribute('aria-sort') !== 'descending'; // numbers: biggest first on first tap
      if (!numeric) desc = th.getAttribute('aria-sort') === 'ascending';
      var rows = Array.prototype.slice.call(tbody.rows);
      rows.sort(function (a, b) {
        var x = a.cells[col].dataset.v, y = b.cells[col].dataset.v;
        var cmp = numeric ? Number(x) - Number(y) : x.localeCompare(y);
        return desc ? -cmp : cmp;
      });
      rows.forEach(function (r) { tbody.appendChild(r); });  // moves existing rows
      table.querySelectorAll('th').forEach(function (h) { h.setAttribute('aria-sort', 'none'); });
      th.setAttribute('aria-sort', desc ? 'descending' : 'ascending');
    }
    th.addEventListener('click', sort);
    th.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sort(); } });
  });
})();
