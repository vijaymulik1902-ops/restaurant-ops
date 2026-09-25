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
  }

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
    var els = (root.querySelectorAll ? root.querySelectorAll('[data-age], [data-late-in]') : []);
    Array.prototype.forEach.call(els, function (el) { if (!el._t0) el._t0 = now; });
    if (root.dataset && (root.dataset.age || root.dataset.lateIn) && !root._t0) root._t0 = now;
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
    var kempty = document.getElementById('kempty');
    var kboard = document.querySelector('.kboard');
    if (kempty && kboard) kempty.hidden = kboard.children.length > 0;
  }
  htmx.onLoad(function (el) { stamp(el); tick(); });
  setInterval(tick, 15000);

  /* ---------- live updates ---------- */
  function refreshCard(el) {
    if (el && el.dataset.cardUrl) {
      htmx.ajax('GET', el.dataset.cardUrl, { target: el, swap: 'outerHTML' });
    }
  }
  function reloadLists() {
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
  function kitchenBoard(channel) {
    var board = document.querySelector('.kboard');
    if (!board || channel !== 'station:' + board.dataset.station) return null;
    return board;
  }

  var handlers = {
    table: function (d) {
      refreshCard(document.getElementById('table-' + d.table_id));
      refreshOrder(d.order_id);
    },
    item: function (d) {
      if (kitchenBoard(d.channel)) refreshCard(document.getElementById('item-' + d.item_id));
      refreshOrder(d.order_id);
    },
    item_ready: function (d) {
      alertFoodReady(d);
      refreshOrder(d.order_id);
    },
    kot: function (d) {
      var board = kitchenBoard(d.channel);
      if (board) {
        (d.items || []).forEach(function (it) {
          if (document.getElementById('item-' + it.item_id)) return;
          var slot = document.createElement('div');
          slot.id = 'item-' + it.item_id;
          slot.dataset.cardUrl = '/kitchen/items/' + it.item_id + '/card?station=' + board.dataset.station;
          board.appendChild(slot); // newest last: board stays oldest-first
          refreshCard(slot);
        });
      }
      refreshOrder(d.order_id);
    },
    order: function (d) { refreshOrder(d.order_id); },
    menu: function (d) { setDishAvailable(d); },
    bill: function () { /* table events already refresh the card */ }
  };

  var streamUrl = body.dataset.stream;
  var es = null;
  var retryMs = 1000;
  var statusEl = document.getElementById('live-status');

  function connect() {
    es = new EventSource(streamUrl);
    es.onopen = function () {
      retryMs = 1000;
      if (statusEl) statusEl.hidden = true;
      // Full reload on every (re)connect: covers anything that changed in between
      reloadLists();
    };
    Object.keys(handlers).forEach(function (type) {
      es.addEventListener(type, function (ev) {
        var data;
        try { data = JSON.parse(ev.data); } catch (e) { return; }
        handlers[type](data);
      });
    });
    es.onerror = function () {
      es.close();
      if (statusEl) statusEl.hidden = false;
      setTimeout(connect, retryMs);
      retryMs = Math.min(retryMs * 2, 15000);
    };
  }
  if (streamUrl && window.EventSource) connect();

  // Phones suspend background tabs; reconnect as soon as the screen is visible again
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible' && streamUrl && es && es.readyState === 2) {
      retryMs = 1000;
      connect();
    }
  });

  /* ---------- forms ---------- */
  // Send-once forms: block a second submit (double tap). Buttons are only disabled
  // after the browser has collected the form data, so the clicked button's value is sent.
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form.hasAttribute('data-once')) return;
    if (form.dataset.sent) { e.preventDefault(); return; }
    form.dataset.sent = '1';
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
      try { sessionStorage.removeItem(draftKey); } catch (e) { /* ignore */ }
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

  function updateRow(input, skipSave) {
    var row = input.closest('.menu-row');
    var qty = parseInt(input.value, 10) || 0;
    if (row) {
      row.classList.toggle('picked', qty > 0);
      var note = row.querySelector('.note');
      if (note) note.hidden = qty <= 0 && !note.value;
    }
    var count = 0;
    document.querySelectorAll('#kot-form .stepper input').forEach(function (i) {
      count += Math.max(0, parseInt(i.value, 10) || 0);
    });
    var label = document.getElementById('send-count');
    if (label) label.textContent = count ? '(' + count + ')' : '';
    if (!skipSave) saveDraft();
  }
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

  /* ---------- login keypad ---------- */
  var pin = document.getElementById('pin');
  if (pin) {
    document.querySelectorAll('.keypad [data-key]').forEach(function (key) {
      key.addEventListener('click', function () {
        var k = key.dataset.key;
        if (k === 'clear') pin.value = '';
        else if (k === 'back') pin.value = pin.value.slice(0, -1);
        else if (pin.value.length < 4) pin.value += k;
        var form = document.getElementById('login-form');
        if (pin.value.length === 4 && form.querySelector('input[name="name"]:checked')) form.requestSubmit();
      });
    });
  }
})();
