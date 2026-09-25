/* Sales report: two charts (Chart.js, only if app/static/chart.umd.min.js is present)
 * and a sortable menu table. No innerHTML anywhere: rows are moved, never rebuilt. */
(function () {
  'use strict';

  var dataEl = document.getElementById('sales-data');
  var data = dataEl ? JSON.parse(dataEl.textContent) : null;

  /* ---------- charts ---------- */
  // Single series each, so no legend: the heading names the chart. Colour: categorical
  // slot 1 (blue, dark step), validated against the app's dark surface.
  var SERIES = '#3987e5';
  var INK = '#c3c2b7';          // secondary text
  var GRID = 'rgba(255,255,255,0.08)';

  function rupees(paise) {
    return '₹' + (paise / 100).toLocaleString('en-IN', { maximumFractionDigits: 0 });
  }

  function baseOptions() {
    return {
      responsive: true,
      maintainAspectRatio: false,  // the .chart-box sets the height; width follows the phone
      resizeDelay: 100,            // rotate/resize on mobile without thrashing
      animation: false,
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
  window.addEventListener('pageshow', function (e) { if (e.persisted) renderCharts(); });

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
