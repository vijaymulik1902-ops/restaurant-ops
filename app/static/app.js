/* Live updates, timers and small UI helpers. No build step; plain ES2017.
 *
 * Every page is fully rendered by the server. This script then:
 *  1. opens EventSource(data-stream) and, for each small event, re-fetches only
 *     the affected card's partial (htmx.ajax GET of its data-card-url);
 *  2. on every (re)connect, reloads each [data-list-url] region in full, so
 *     nothing missed while disconnected is ever lost;
 *  3. ticks timers locally and turns cards red at the deadline the server gave.
 */
(function () {
  'use strict';
  var body = document.body;

  /* ---------- toasts, beep, vibrate ---------- */
  function toast(message, kind, ms) {
    var box = document.getElementById('toasts');
    if (!box) return;
    var el = document.createElement('div');
    el.className = 'toast ' + (kind || '');
    el.setAttribute('role', 'alert');
    el.textContent = message;
    box.appendChild(el);
    setTimeout(function () { el.remove(); }, ms || 5000);
  }

  // Server flash messages (after a redirect) show as a toast; the inline copy stays for no-JS
  (function () {
    var box = document.getElementById('flash');
    var f = box && box.querySelector('.flash');
    if (!f) return;
    var kind = /flash-(\w+)/.exec(f.className);
    kind = kind ? kind[1] : '';
    toast(f.textContent.trim(), { error: 'error', ok: 'ok', sent: 'ok', warn: 'warn' }[kind] || '', kind === 'error' ? 8000 : 5000);
    box.hidden = true;
  })();

  var audioCtx = null;
  function unlockAudio() {
    // iOS only allows audio after a user gesture; create the context on the first tap
    if (audioCtx) return;
    var Ctx = window.AudioContext || window.webkitAudioContext;
    if (Ctx) { try { audioCtx = new Ctx(); } catch (e) { audioCtx = null; } }
  }
  document.addEventListener('pointerdown', unlockAudio, { once: true, capture: true });

  function beep() {
    if (!audioCtx) return;
    try {
      if (audioCtx.state === 'suspended') audioCtx.resume();
      [0, 0.25].forEach(function (offset) {
        var osc = audioCtx.createOscillator();
        var gain = audioCtx.createGain();
        osc.frequency.value = 880;
        gain.gain.value = 0.25;
        osc.connect(gain); gain.connect(audioCtx.destination);
        var t = audioCtx.currentTime + offset;
        osc.start(t); osc.stop(t + 0.15);
      });
    } catch (e) { /* sound is a nice-to-have */ }
  }

  function alertFoodReady(d) {
    if (navigator.vibrate) navigator.vibrate([200, 100, 200]);
    beep();
    toast('Table ' + d.table_number + ': ' + d.qty + ' × ' + d.name + ' READY', 'ready', 8000);
    rememberAlert(d);
  }

  /* ---------- waiter "Alerts" sheet: recent food-ready alerts, kept for this tab only ---------- */
  var ALERT_KEY = 'readyAlerts';
  function readAlerts() {
    try { return JSON.parse(sessionStorage.getItem(ALERT_KEY) || '[]'); } catch (e) { return []; }
  }
  function rememberAlert(d) {
    var list = readAlerts();
    list.unshift({ t: Date.now(), table: d.table_number, qty: d.qty, name: d.name, order: d.order_id, seen: false });
    list = list.slice(0, 20);
    try { sessionStorage.setItem(ALERT_KEY, JSON.stringify(list)); } catch (e) { /* not kept */ }
    renderAlerts(list);
  }
  function renderAlerts(list) {
    list = list || readAlerts();
    var unseen = list.filter(function (a) { return !a.seen; }).length;
    document.querySelectorAll('[data-alert-count]').forEach(function (el) {
      el.textContent = unseen > 9 ? '9+' : String(unseen);
      el.hidden = unseen === 0;
    });
    var ul = document.getElementById('alert-list');
    if (!ul) return;
    ul.textContent = '';
    list.forEach(function (a) {
      var li = document.createElement('li');
      var link = document.createElement('a');
      link.href = a.order ? '/orders/' + a.order : '/floor';
      var time = new Date(a.t);
      link.textContent = 'Table ' + a.table + ': ' + a.qty + ' × ' + a.name + ' ready · ' +
        String(time.getHours()).padStart(2, '0') + ':' + String(time.getMinutes()).padStart(2, '0');
      li.appendChild(link);
      ul.appendChild(li);
    });
    var empty = document.getElementById('alert-empty');
    if (empty) empty.hidden = list.length > 0;
  }
  renderAlerts();

  /* ---------- sheets (More / Me / Alerts) ---------- */
  var openSheet = null, sheetOpener = null;
  function closeSheet() {
    if (!openSheet) return;
    openSheet.hidden = true;
    document.querySelectorAll('.sheet-backdrop').forEach(function (b) { b.hidden = true; });
    openSheet = null;
    if (sheetOpener) sheetOpener.focus();
  }
  document.addEventListener('click', function (e) {
    var opener = e.target.closest('[data-sheet-open]');
    if (opener) {
      var sheet = document.getElementById('sheet-' + opener.dataset.sheetOpen);
      if (!sheet) return;
      closeSheet();
      sheet.hidden = false;
      document.querySelectorAll('.sheet-backdrop').forEach(function (b) { b.hidden = false; });
      openSheet = sheet; sheetOpener = opener;
      var first = sheet.querySelector('a, button:not(.sheet-close)') || sheet.querySelector('button');
      if (first) first.focus();
      if (opener.dataset.sheetOpen === 'alerts') {  // opening the list marks everything seen
        var list = readAlerts().map(function (a) { a.seen = true; return a; });
        try { sessionStorage.setItem(ALERT_KEY, JSON.stringify(list)); } catch (err) { /* ignore */ }
        renderAlerts(list);
      }
      return;
    }
    if (e.target.closest('[data-sheet-close]')) closeSheet();
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeSheet(); });

  /* ---------- sidebar collapse (laptops), remembered per device ---------- */
  document.querySelectorAll('[data-nav-collapse]').forEach(function (btn) {
    function apply(collapsed) {
      body.classList.toggle('nav-collapsed', collapsed);
      btn.setAttribute('aria-pressed', collapsed ? 'true' : 'false');
      btn.setAttribute('aria-label', collapsed ? 'Expand menu' : 'Collapse menu');
    }
    var saved = false;
    try { saved = localStorage.getItem('navCollapsed') === '1'; } catch (e) { /* default open */ }
    apply(saved);
    btn.addEventListener('click', function () {
      var next = !body.classList.contains('nav-collapsed');
      apply(next);
      try { localStorage.setItem('navCollapsed', next ? '1' : '0'); } catch (e) { /* not remembered */ }
    });
  });

  /* ---------- "Orders" tab = floor filtered to tables with an open order ---------- */
  function applyFloorHash() {
    if (body.dataset.page !== 'floor') return;
    var board = document.getElementById('board');
    var orders = location.hash === '#orders';
    if (board) {
      if (orders) board.dataset.filter = 'active'; else delete board.dataset.filter;
    }
    document.querySelectorAll('[data-nav="floor"], [data-nav="orders"]').forEach(function (a) {
      var mine = (a.dataset.nav === 'orders') === orders;
      if (location.pathname === '/floor' && mine) a.setAttribute('aria-current', 'page');
      else if (location.pathname === '/floor') a.removeAttribute('aria-current');
    });
  }
  window.addEventListener('hashchange', applyFloorHash);
  applyFloorHash();

  // Server-side errors on HTMX requests arrive as an HX-Trigger "flash" event
  body.addEventListener('flash', function (e) {
    var msg = e.detail && (e.detail.message || e.detail.value);
    if (msg) toast(msg, 'error');
  });
  body.addEventListener('htmx:responseError', function (e) {
    if (e.detail.xhr.getResponseHeader('HX-Trigger')) return; // server already sent a "flash" message
    toast(e.detail.xhr.status === 403 ? 'Not allowed' : 'Something went wrong, try again', 'error');
  });
  body.addEventListener('htmx:sendError', function () { toast('No connection to the server', 'error'); });

  /* ---------- timers ---------- */
  // Ages come from the server as seconds at render time; count up from there locally
  // (so a phone with a wrong clock still shows the right age).
  function stamp(root) {
    var now = Date.now();
    var els = (root.querySelectorAll ? root.querySelectorAll('[data-age], [data-late-in], [data-refresh-in]') : []);
    Array.prototype.forEach.call(els, function (el) { if (!el._t0) el._t0 = now; });
    if (root.dataset && (root.dataset.age || root.dataset.lateIn || root.dataset.refreshIn) && !root._t0) root._t0 = now;
  }
  function tick() {
    var now = Date.now();
    document.querySelectorAll('[data-age]').forEach(function (el) {
      var secs = Number(el.dataset.age) + (now - (el._t0 || now)) / 1000;
      el.textContent = Math.floor(secs / 60) + 'm';
    });
    document.querySelectorAll('[data-late-in]').forEach(function (el) {
      var left = Number(el.dataset.lateIn) - (now - (el._t0 || now)) / 1000;
      el.classList.toggle('late', left <= 0);
    });
    // A reservation's hold starts / turns late / ends with no event: re-read that card once
    document.querySelectorAll('[data-refresh-in]').forEach(function (el) {
      var left = Number(el.dataset.refreshIn) - (now - (el._t0 || now)) / 1000;
      if (left <= 0 && !el._refreshing) { el._refreshing = true; refreshCard(el); }
    });
    var kempty = document.getElementById('kempty');
    var kboard = document.querySelector('.kboard');
    if (kempty && kboard) kempty.hidden = kboard.children.length > 0;
  }
  htmx.onLoad(function (el) { stamp(el); tick(); });
  setInterval(tick, 15000);

  /* ---------- motion: only to show what changed (CSS does the animating) ---------- */
  var freshTickets = {};   // item ids that just arrived by KOT: print them in
  var readied = {};        // item ids a chef just marked ready: slip in after the slide-out
  var priorStatus = {};    // table card id -> status before refresh: crossfade from it

  htmx.onLoad(function (el) {
    if (!el.classList) return;
    if (el.classList.contains('kcard')) {
      if (freshTickets[el.id]) { el.classList.add('feed-in'); delete freshTickets[el.id]; }
      if (readied[el.id]) { el.classList.add('slip-in'); delete readied[el.id]; }
    }
    if (el.classList.contains('tcard') && priorStatus[el.id]) {
      if (priorStatus[el.id] !== el.dataset.status) {
        el.dataset.from = priorStatus[el.id];
        setTimeout(function () { delete el.dataset.from; }, 400);
      }
      delete priorStatus[el.id];
    }
  });
  body.addEventListener('htmx:beforeSwap', function (e) {
    var elt = e.detail.requestConfig && e.detail.requestConfig.elt;
    if (elt && elt.getAttribute && /\/ready$/.test(elt.getAttribute('hx-post') || '')) {
      readied[e.detail.target.id] = true;
    }
  });

  /* ---------- live updates ---------- */
  function refreshCard(el) {
    if (el && el.dataset.cardUrl) {
      if (el.classList.contains('tcard')) priorStatus[el.id] = el.dataset.status;
      htmx.ajax('GET', el.dataset.cardUrl, { target: el, swap: 'outerHTML' });
    }
  }
  // Manager Home: re-read the whole live block (tiles + mini map) at most every 2 s
  var homeTimer = null;
  function refreshHome() {
    if (!document.querySelector('[data-home]') || homeTimer) return;
    homeTimer = setTimeout(function () {
      homeTimer = null;
      htmx.ajax('GET', '/home', { target: '#home-live', select: '#home-live', swap: 'outerHTML' });
    }, 2000);
  }
  var bookingsTimer = null;
  function reloadBookings() {  // several events can arrive together: one reload per second at most
    if (bookingsTimer) return;
    bookingsTimer = setTimeout(function () {
      bookingsTimer = null;
      document.querySelectorAll('[data-bookings]').forEach(function (el) {
        htmx.ajax('GET', el.dataset.listUrl, { target: el, swap: 'innerHTML' });
      });
    }, 1000);
  }
  function reloadLists() {
    refreshHome();
    document.querySelectorAll('[data-list-url]').forEach(function (el) {
      htmx.ajax('GET', el.dataset.listUrl, { target: el, swap: 'innerHTML' });
    });
  }
  var orderRefreshTimer = null;
  function refreshOrder(orderId) {
    var el = document.getElementById('order-items');
    if (!el || !orderId || el.dataset.orderId !== String(orderId)) return;
    // Don't yank the list away while someone is picking a cancel reason; try again shortly
    if (el.querySelector('details[open]') || el.contains(document.activeElement)) {
      clearTimeout(orderRefreshTimer);
      orderRefreshTimer = setTimeout(function () { refreshOrder(orderId); }, 4000);
      return;
    }
    refreshCard(el);
  }

  function setDishAvailable(d) {
    var row = document.querySelector('.menu-row[data-menu-id="' + d.menu_item_id + '"]');
    if (!row) return;
    row.classList.toggle('unavailable', !d.available);
    row.querySelectorAll('input, button').forEach(function (el) { el.disabled = !d.available; });
    var label = row.querySelector('.na-label');
    if (label) label.hidden = d.available;
    if (!d.available && row.querySelector('.stepper input').value !== '0') {
      toast(d.name + ' just became unavailable', 'error');
    }
  }
  // The read-only cooking summary above the tickets: re-fetched on any event for this station
  var summaryWasOpen = false;
  function refreshSummary() {
    var el = document.getElementById('cook-summary');
    if (!el) return;
    summaryWasOpen = !!el.querySelector('details.cook-more[open]');
    refreshCard(el);
  }
  htmx.onLoad(function (el) {
    if (el.id === 'cook-summary' && summaryWasOpen) {
      var more = el.querySelector('details.cook-more');
      if (more) more.open = true;
    }
  });

  function kitchenBoard(channel) {
    var board = document.querySelector('.kboard');
    if (!board || channel !== 'station:' + board.dataset.station) return null;
    return board;
  }

  var handlers = {
    table: function (d) {
      refreshHome();
      refreshCard(document.getElementById('table-' + d.table_id));
      refreshOrder(d.order_id);
    },
    item: function (d) {
      refreshHome();
      if (kitchenBoard(d.channel)) {
        refreshCard(document.getElementById('item-' + d.item_id));
        refreshSummary();
      }
      refreshOrder(d.order_id);
    },
    item_ready: function (d) {
      alertFoodReady(d);
      refreshOrder(d.order_id);
    },
    kot: function (d) {
      refreshHome();
      var board = kitchenBoard(d.channel);
      if (board) {
        (d.items || []).forEach(function (it) {
          if (document.getElementById('item-' + it.item_id)) return;
          var slot = document.createElement('div');
          slot.id = 'item-' + it.item_id;
          freshTickets[slot.id] = true;
          slot.dataset.cardUrl = '/kitchen/items/' + it.item_id + '/card?station=' + board.dataset.station;
          board.appendChild(slot); // newest last: board stays oldest-first
          refreshCard(slot);
        });
        refreshSummary();
      }
      refreshOrder(d.order_id);
    },
    order: function (d) { refreshOrder(d.order_id); },
    menu: function (d) { setDishAvailable(d); },
    menu_changed: function () { refreshCard(document.getElementById('menu-block')); },
    bill: function () { refreshHome(); },
    booking: function (d) {  // ids + status only (never names or phones)
      if (d.table_id) refreshCard(document.getElementById('table-' + d.table_id));
      reloadBookings();
      refreshHome();
    }
  };

  var streamUrl = body.dataset.stream;
  var es = null;
  var retryMs = 1000;
  var streamStopped = false;
  var reconnectTimer = null;
  // Some networks (e.g. Cloudflare quick tunnels, some corporate proxies) hold a streamed
  // response until it ends, so events never arrive. The server pings every 15 s; if we hear
  // nothing for 40 s, reload the lists every 10 s instead.
  var SILENT_LIMIT_MS = 40000, POLL_MS = 10000;
  var lastHeard = Date.now(), pollTimer = null;
  var statusEl = document.getElementById('live-status');

  function connect() {
    clearTimeout(reconnectTimer); // never two streams (wake-up + pending retry)
    if (es && es.readyState !== 2) es.close();
    es = new EventSource(streamUrl);
    lastHeard = Date.now();
    es.addEventListener('ping', function () { lastHeard = Date.now(); });
    es.onopen = function () {
      retryMs = 1000;
      if (statusEl) statusEl.hidden = true;
      // Full reload on every (re)connect: covers anything that changed in between
      reloadLists();
    };
    Object.keys(handlers).forEach(function (type) {
      es.addEventListener(type, function (ev) {
        lastHeard = Date.now();
        var data;
        try { data = JSON.parse(ev.data); } catch (e) { return; }
        handlers[type](data);
      });
    });
    es.onerror = function () {
      es.close();
      if (streamStopped) return;
      if (statusEl) statusEl.hidden = false;
      // Logged out (or deactivated)? Stop retrying and go to the login screen.
      fetch('/auth/check', { cache: 'no-store', credentials: 'same-origin' })
        .then(function (r) {
          if (r.status === 401) { stopStream(); window.location.href = '/login'; return; }
          scheduleReconnect();
        })
        .catch(scheduleReconnect); // server unreachable: keep trying
    };
  }
  function scheduleReconnect() {
    if (streamStopped) return;
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, retryMs);
    retryMs = Math.min(retryMs * 2, 15000);
  }
  function stopStream() {
    streamStopped = true;
    clearInterval(pollTimer);
    clearTimeout(reconnectTimer);
    if (es) es.close();
  }
  function startPolling() {
    if (pollTimer || streamStopped) return;
    if (es) es.close();
    clearTimeout(reconnectTimer);
    if (statusEl) {
      statusEl.textContent = 'Live updates are delayed on this network: refreshing every 10 s';
      statusEl.classList.add('polling');
      statusEl.hidden = false;
    }
    reloadLists();
    pollTimer = setInterval(reloadLists, POLL_MS);
  }
  if (streamUrl && window.EventSource) {
    connect();
    setInterval(function () {
      if (!pollTimer && !streamStopped && document.visibilityState === 'visible'
          && Date.now() - lastHeard > SILENT_LIMIT_MS) startPolling();
    }, 5000);
  }

  // Phones suspend background tabs; reconnect as soon as the screen is visible again
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible' && !pollTimer) lastHeard = Date.now();  // phone just woke: give the stream a fresh 40 s
    if (document.visibilityState === 'visible' && streamUrl && es && es.readyState === 2 && !streamStopped && !pollTimer) {
      retryMs = 1000;
      connect();
    }
  });

  /* ---------- forms ---------- */
  // Send-once forms: block a second submit (double tap). Buttons are only disabled
  // after the browser has collected the form data, so the clicked button's value is sent.
  document.addEventListener('submit', function (e) {
    var form = e.target;
    // Destructive forms ask first (e.g. Deactivate on the Staff page)
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) { e.preventDefault(); return; }
    if (form.getAttribute('action') === '/logout') stopStream(); // no reconnect attempts while leaving
    if (!form.hasAttribute('data-once')) return;
    if (form.dataset.sent) { e.preventDefault(); return; }
    form.dataset.sent = '1';
    var sendLabel = form.id === 'kot-form' && form.querySelector('.send-label');
    if (sendLabel) sendLabel.textContent = 'Sending…';
    setTimeout(function () {
      form.querySelectorAll('button[type="submit"], button:not([type])').forEach(function (b) {
        b.disabled = true; b.setAttribute('aria-busy', 'true');
      });
    }, 0);
  });
  // Coming back via the browser Back button restores a "sent" form from cache: re-arm it
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    document.querySelectorAll('form[data-once]').forEach(function (form) {
      delete form.dataset.sent;
      form.querySelectorAll('button').forEach(function (b) { b.disabled = false; b.removeAttribute('aria-busy'); });
    });
  });

  /* ---------- order screen: steppers, search, notes, draft ---------- */
  // The waiter's picks are kept in sessionStorage (this tab only) so tapping "Mark served",
  // cancelling an item, or a failed send never wipes a half-built order. Cleared once a KOT is sent.
  var kotForm = document.getElementById('kot-form');
  var draftKey = kotForm && ('draft:' + kotForm.dataset.draftKey);
  function readDraft() {
    try { return JSON.parse(sessionStorage.getItem(draftKey) || '{}'); } catch (e) { return {}; }
  }
  function saveDraft() {
    if (!draftKey) return;
    var draft = {};
    kotForm.querySelectorAll('.stepper input, input.note').forEach(function (i) {
      if (i.value && i.value !== '0') draft[i.name] = i.value;
    });
    try { sessionStorage.setItem(draftKey, JSON.stringify(draft)); } catch (e) { /* private mode */ }
  }
  function restoreDraft() {
    if (!draftKey) return;
    if (body.hasAttribute('data-kot-sent')) {
      // Page loaded right after a successful send: start a fresh draft (once only)
      body.removeAttribute('data-kot-sent');
      try { sessionStorage.removeItem(draftKey); } catch (e) { /* ignore */ }
      confirmSent();
      return;
    }
    var draft = readDraft();
    Object.keys(draft).forEach(function (name) {
      var input = kotForm.elements[name];
      // The server's re-rendered values (after a failed send) win over the draft
      if (input && (!input.value || input.value === '0')) input.value = draft[name];
    });
    kotForm.querySelectorAll('.stepper input').forEach(function (i) { updateRow(i, true); });
  }

  // Shown on the page the send redirected to, i.e. only after the server accepted the KOT
  function confirmSent() {
    var btn = document.getElementById('send-btn');
    var label = btn && btn.querySelector('.send-label');
    if (!label) return;
    btn.classList.add('sent');
    label.textContent = 'Sent ✓';
    setTimeout(function () { btn.classList.remove('sent'); label.textContent = 'Send to kitchen'; }, 1400);
  }

  function updateRow(input, skipSave) {
    var row = input.closest('.menu-row');
    var qty = parseInt(input.value, 10) || 0;
    if (row) {
      row.classList.toggle('picked', qty > 0);
      var note = row.querySelector('.note');
      if (note) note.hidden = qty <= 0 && !note.value;
    }
    var count = 0, total = 0;
    document.querySelectorAll('#kot-form .stepper input').forEach(function (i) {
      var q = Math.max(0, parseInt(i.value, 10) || 0);
      var r = i.closest('.menu-row');
      if (i.disabled || !q) return;
      count += q;
      total += q * Number(r && r.dataset.price || 0);
    });
    // Display only: the server prices every KOT itself; the client never sends totals
    var label = document.getElementById('send-count');
    if (label) {
      label.textContent = count
        ? count + (count === 1 ? ' item' : ' items') + ' · ₹' + (total / 100).toLocaleString('en-IN')
        : '';
    }
    if (!skipSave) saveDraft();
  }
  document.addEventListener('click', function (e) {  // "Note" opens that dish's note field
    var nb = e.target.closest('[data-note-for]');
    if (!nb) return;
    var field = document.querySelector('#kot-form input[name="' + nb.dataset.noteFor + '"]');
    if (field) { field.hidden = false; field.focus(); }
  });
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-step]');
    if (!btn) return;
    var input = btn.parentElement.querySelector('input');
    var next = Math.min(99, Math.max(0, (parseInt(input.value, 10) || 0) + Number(btn.dataset.step)));
    input.value = next;
    updateRow(input);
  });
  document.addEventListener('input', function (e) {
    if (e.target.matches('.stepper input')) updateRow(e.target);
    if (e.target.matches('#kot-form input.note')) saveDraft();
    if (e.target.id === 'menu-search') {
      var q = e.target.value.trim().toLowerCase();
      document.querySelectorAll('.menu-row').forEach(function (row) {
        row.hidden = q && row.dataset.name.indexOf(q) === -1;
      });
      document.querySelectorAll('.menu-cat').forEach(function (cat) {
        cat.hidden = !cat.querySelector('.menu-row:not([hidden])');
        if (q) cat.open = true;
      });
    }
  });

  restoreDraft();

  // The menu block was reloaded (price change, new/renamed/archived dish): put the
  // waiter's picks back from the draft and re-apply any search they had typed.
  htmx.onLoad(function (el) {
    if (el.id !== 'menu-block') return;
    restoreDraft();
    var search = document.getElementById('menu-search');
    if (search && search.value) search.dispatchEvent(new Event('input', { bubbles: true }));
  });

  /* ---------- "Dim kitchen" (chef screens): dark variant, remembered per device ---------- */
  document.querySelectorAll('[data-dim-toggle]').forEach(function (btn) {
    var html = document.documentElement;
    btn.setAttribute('aria-pressed', html.getAttribute('data-theme') === 'dark' ? 'true' : 'false');
    btn.addEventListener('click', function () {
      var dim = html.getAttribute('data-theme') !== 'dark';
      html.setAttribute('data-theme', dim ? 'dark' : 'light');
      btn.setAttribute('aria-pressed', dim ? 'true' : 'false');
      try { localStorage.setItem('dimKitchen', dim ? '1' : '0'); } catch (e) { /* not remembered */ }
    });
  });

  /* ---------- insights: suggested questions + clear box after asking ---------- */
  var aiPending = false, aiPoll = null;
  function aiStage(text) { var el = document.getElementById('ai-loading'); if (el) el.textContent = text; }
  body.addEventListener('htmx:beforeRequest', function (e) {
    if (!e.detail.elt || e.detail.elt.id !== 'ai-form') return;
    if (aiPending) { e.preventDefault(); return; }  // one question at a time
    aiPending = true;
    aiStage('Thinking...');
    aiPoll = setInterval(function () {  // show which step the server is on
      fetch('/insights/progress', { cache: 'no-store', credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { if (aiPending && d && d.stage) aiStage(d.stage); })
        .catch(function () {});
    }, 700);
  });
  body.addEventListener('htmx:afterRequest', function (e) {
    if (!e.detail.elt || e.detail.elt.id !== 'ai-form') return;
    aiPending = false;
    clearInterval(aiPoll);
  });
  document.addEventListener('click', function (e) {
    var chip = e.target.closest('.chip-q');
    if (!chip || aiPending) return;
    var box = document.getElementById('ai-question');
    var form = document.getElementById('ai-form');
    if (!box || !form) return;
    box.value = chip.dataset.question;
    form.requestSubmit();
  });
  body.addEventListener('htmx:afterRequest', function (e) {
    if (e.detail.elt && e.detail.elt.id === 'ai-form' && e.detail.successful) {
      document.getElementById('ai-question').value = '';
      var log = document.getElementById('chat-log');
      if (log && log.lastElementChild) log.lastElementChild.scrollIntoView({ block: 'nearest' });
    }
  });

  /* ---------- floor: section chips (a style rule, so it survives card swaps) ---------- */
  document.addEventListener('click', function (e) {
    var chip = e.target.closest('[data-section-filter]');
    if (!chip) return;
    var sec = chip.dataset.sectionFilter;
    var rule = document.getElementById('section-filter-style');
    if (!rule) { rule = document.createElement('style'); rule.id = 'section-filter-style'; document.head.appendChild(rule); }
    rule.textContent = sec ? '#board .tcard:not([data-section="' + sec.replace(/[^A-Za-z0-9]/g, '') + '"]) { display: none; }' : '';
    document.querySelectorAll('[data-section-filter]').forEach(function (c) {
      c.classList.toggle('active', c === chip);
      c.setAttribute('aria-pressed', c === chip ? 'true' : 'false');
    });
  });

  /* ---------- counter: bill panel beside the tables (sheet on phones) ---------- */
  var panel = document.getElementById('counter-panel');
  if (panel) {
    document.addEventListener('click', function (e) {
      var card = e.target.closest('#board a.tcard');
      if (!card || e.ctrlKey || e.metaKey || e.shiftKey || !/^\/counter\/orders\//.test(card.getAttribute('href'))) return;
      e.preventDefault();
      panel.classList.remove('panel-idle');
      htmx.ajax('GET', card.getAttribute('href'), { target: '#counter-panel', select: '#bill-panel', swap: 'innerHTML' });
    });
    document.addEventListener('click', function (e) {
      if (!e.target.closest('[data-panel-close]')) return;
      panel.classList.add('panel-idle');
      var idle = document.createElement('p');
      idle.className = 'panel-empty';
      idle.textContent = 'Tap a table to see its bill here.';
      panel.replaceChildren(idle);
    });
  }

  /* ---------- counter: status filter ---------- */
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('.filter-btn');
    if (!btn) return;
    var board = document.getElementById('board');
    board.dataset.filter = btn.dataset.filter;
    document.querySelectorAll('.filter-btn').forEach(function (b) {
      b.classList.toggle('outline', b !== btn);
    });
  });

  /* ---------- login: choosing a staff badge reveals the PIN section below the grid ---------- */
  var pinPanel = document.getElementById('pin-panel');
  document.querySelectorAll('.staff-pick input[name="name"]').forEach(function (radio) {
    radio.addEventListener('change', function () {
      if (!pinPanel) return;
      var wasHidden = pinPanel.hidden;
      pinPanel.hidden = false;
      if (wasHidden) {
        pinPanel.classList.remove('revealing');
        void pinPanel.offsetWidth;  // restart the fade/slide
        pinPanel.classList.add('revealing');
      }
      var who = document.getElementById('for-who');
      if (who) who.textContent = 'PIN for ' + radio.value;
      var p = document.getElementById('pin');
      if (p) p.value = '';
      var still = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      var phone = window.matchMedia && window.matchMedia('(max-width: 899px)').matches;
      pinPanel.scrollIntoView({ block: phone ? 'start' : 'nearest', behavior: still ? 'auto' : 'smooth' });
    });
  });

  /* ---------- login keypad ---------- */
  var pin = document.getElementById('pin');
  if (pin) {
    document.querySelectorAll('.keypad [data-key]').forEach(function (key) {
      key.addEventListener('click', function () {
        var k = key.dataset.key;
        if (k === 'clear') pin.value = '';
        else if (k === 'back') pin.value = pin.value.slice(0, -1);
        else if (pin.value.length < 8) pin.value += k;
        // Auto-submit once the PIN is as long as this person's role uses (4, or 6 for managers)
        var form = document.getElementById('login-form');
        var who = form.querySelector('input[name="name"]:checked');
        if (who && pin.value.length === Number(who.dataset.pinLength || 4)) form.requestSubmit();
      });
    });
  }
})();
