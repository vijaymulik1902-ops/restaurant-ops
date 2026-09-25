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
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: 'index', intersect: false },   // crosshair-style hover
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: function (ctx) { return rupees(ctx.parsed.y); } } }
      },
      scales: {
        x: { ticks: { color: INK, maxRotation: 0, autoSkip: true }, grid: { display: false } },
        y: { beginAtZero: true, ticks: { color: INK, callback: function (v) { return rupees(v); } },
             grid: { color: GRID }, border: { display: false } }
      }
    };
  }

  if (data && window.Chart) {
    var daily = document.getElementById('daily-chart');
    if (daily) {
      new window.Chart(daily, {
        type: 'line',
        data: {
          labels: data.days.map(function (d) { return d.slice(5); }),  // MM-DD
          datasets: [{ data: data.daily, borderColor: SERIES, backgroundColor: SERIES,
                       borderWidth: 2, pointRadius: data.daily.length > 40 ? 0 : 4, pointHoverRadius: 6,
                       tension: 0 }]
        },
        options: baseOptions()
      });
    }
    var hours = document.getElementById('hours-chart');
    if (hours) {
      var opts = baseOptions();
      opts.plugins.tooltip.callbacks.afterLabel = function (ctx) { return data.hourBills[ctx.dataIndex] + ' bills'; };
      new window.Chart(hours, {
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
