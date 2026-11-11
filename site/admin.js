/* Admin dashboard. All data comes from the admin_stats RPC, which the server only answers for an admin
   (see supabase/migrations/0007_telemetry.sql); this page holds no admin id and shows nothing without that answer. */
(function (root) {
  'use strict';
  var SUPABASE_URL = 'https://muhboxuefazapytpqzjc.supabase.co';
  var SUPABASE_KEY = 'sb_publishable_MrDFLihLJ8El-VQ3WvfhEQ_BArBKEWx';
  var client, days = 30, loadSeq = 0;
  var $ = function (id) { return document.getElementById(id); };

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = String(text);
    return e;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); return n; }
  var nf = new Intl.NumberFormat('en');
  function num(n) { return nf.format(Number(n) || 0); }
  function dur(s) {
    s = Math.round(Number(s) || 0);
    if (s < 60) return s + 's';
    var m = Math.round(s / 60);
    return m < 60 ? m + ' min' : Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
  }
  function show(id) {
    ['ad-loading', 'ad-out', 'ad-denied', 'ad-in'].forEach(function (x) { $(x).hidden = x !== id; });
  }
  function msg(id, text, err) { var m = $(id); m.textContent = text || ''; m.className = 'ad-msg' + (err ? ' err' : ''); }

  // ------------------------------------------------------------------ charts (inline SVG)
  var NS = 'http://www.w3.org/2000/svg';
  function s(tag, attrs, text) {
    var e = document.createElementNS(NS, tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    return e;
  }
  function shortDay(d) { var p = String(d).split('-'); return p.length === 3 ? p[1] + '-' + p[2] : String(d); }
  function chart(host, rows, key, kind, label, color) {
    clear(host);
    rows = rows || [];
    var total = rows.reduce(function (a, r) { return a + (Number(r[key]) || 0); }, 0);
    if (!rows.length || !total) { host.appendChild(el('p', 'ad-empty', 'No data in this range.')); return; }
    var W = 640, H = 220, L = 38, R = 8, T = 10, B = 26, pw = W - L - R, ph = H - T - B;
    var max = Math.max.apply(null, rows.map(function (r) { return Number(r[key]) || 0; }));
    var top = max <= 4 ? 4 : Math.ceil(max / 4) * 4;
    var svg = s('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': label + ': ' + rows.length + ' days, total ' + total + ', peak ' + max });
    svg.appendChild(s('title', {}, label));
    for (var g = 0; g <= 4; g++) {
      var y = T + ph - (ph * g / 4);
      svg.appendChild(s('line', { x1: L, x2: W - R, y1: y, y2: y, stroke: 'currentColor', 'stroke-opacity': g ? '.12' : '.35' }));
      svg.appendChild(s('text', { x: L - 6, y: y + 4, 'text-anchor': 'end', 'class': 'ad-axis' }, num(top * g / 4)));
    }
    var n = rows.length, step = pw / n;
    function x(i) { return L + step * i + step / 2; }
    function yv(v) { return T + ph - ph * (Number(v) || 0) / top; }
    if (kind === 'bar') {
      var bw = Math.max(2, step * 0.7);
      rows.forEach(function (r, i) {
        var v = Number(r[key]) || 0;
        var b = s('rect', { x: x(i) - bw / 2, y: yv(v), width: bw, height: Math.max(0, T + ph - yv(v)), fill: color, rx: 2 });
        b.appendChild(s('title', {}, r.day + ': ' + v));
        svg.appendChild(b);
      });
    } else {
      var pts = rows.map(function (r, i) { return x(i).toFixed(1) + ',' + yv(r[key]).toFixed(1); });
      svg.appendChild(s('polygon', { points: x(0).toFixed(1) + ',' + (T + ph) + ' ' + pts.join(' ') + ' ' + x(n - 1).toFixed(1) + ',' + (T + ph), fill: color, 'fill-opacity': '.14' }));
      svg.appendChild(s('polyline', { points: pts.join(' '), fill: 'none', stroke: color, 'stroke-width': 2.5, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
      rows.forEach(function (r, i) {
        var c = s('circle', { cx: x(i), cy: yv(r[key]), r: n > 45 ? 1.6 : 3, fill: color });
        c.appendChild(s('title', {}, r.day + ': ' + (Number(r[key]) || 0)));
        svg.appendChild(c);
      });
    }
    var every = Math.ceil(n / 7);
    rows.forEach(function (r, i) {
      if (i % every === 0 || i === n - 1) svg.appendChild(s('text', { x: x(i), y: H - 6, 'text-anchor': i === n - 1 ? 'end' : i === 0 ? 'start' : 'middle', 'class': 'ad-axis' }, shortDay(r.day)));
    });
    host.appendChild(svg);
  }

  function barList(host, rows, unit) {
    clear(host);
    if (!rows || !rows.length) { host.appendChild(el('p', 'ad-empty', 'No data in this range.')); return; }
    var max = Math.max.apply(null, rows.map(function (r) { return Number(r.n) || 0; })) || 1;
    var ul = el('ul', 'ad-bars');
    rows.forEach(function (r) {
      var li = el('li');
      var l = el('span', 'l', r.k); l.title = r.k;
      var t = el('span', 't'), i = el('i'); i.style.width = Math.max(2, 100 * (Number(r.n) || 0) / max) + '%'; t.appendChild(i);
      li.appendChild(l); li.appendChild(t); li.appendChild(el('span', 'n', num(r.n) + (unit ? ' ' + unit : '')));
      ul.appendChild(li);
    });
    host.appendChild(ul);
  }

  function table(host, cols, rows) {
    clear(host);
    if (!rows || !rows.length) { host.appendChild(el('p', 'ad-empty', 'No data in this range.')); return; }
    var wrap = el('div', 'ad-tblwrap'), t = el('table', 'ad-tbl'), tr = el('tr');
    cols.forEach(function (c, i) { var th = el('th', i ? 'r' : '', c[0]); th.scope = 'col'; tr.appendChild(th); });
    var thead = el('thead'); thead.appendChild(tr); t.appendChild(thead);
    var tb = el('tbody');
    rows.forEach(function (r) {
      var row = el('tr');
      cols.forEach(function (c, i) { row.appendChild(el('td', i ? 'r' : '', i ? num(r[c[1]]) : r[c[1]])); });
      tb.appendChild(row);
    });
    t.appendChild(tb); wrap.appendChild(t); host.appendChild(wrap);
  }

  function kpis(d) {
    var a = d.app || {}, w = d.web || {}, c = d.accounts || {}, se = a.session || {};
    var items = [
      ['Daily active (24h)', num(a.dau)], ['Weekly active (7d)', num(a.wau)], ['Monthly active (30d)', num(a.mau)],
      ['Installs, all time', num(a.installs_all)], ['Installs active in range', num(a.installs_range)], ['New installs', num(a.new_installs_range)],
      ['Avg session', dur(se.avg_s)], ['Median session', dur(se.median_s)], ['Total hours', num(se.total_hours)],
      ['Website visitors', num(w.visitors_range)], ['Page views', num(w.pageviews_range)],
      ['Companion accounts', num(c.total_users)], ['Signed up in range', num(c.signed_up_range)], ['Verified CH links', num(c.verified_ch_links)],
      ['Accounts with friends', num(c.users_with_friends)],
      ['Installs signed in', num(c.installs_signed_in) + ' / ' + num(c.installs_started)]
    ];
    var dl = clear($('ad-kpis'));
    items.forEach(function (it) { var box = el('div', 'ad-kpi'); box.appendChild(el('dt', '', it[0])); box.appendChild(el('dd', '', it[1])); dl.appendChild(box); });
  }

  function render(d) {
    kpis(d);
    var a = d.app || {}, w = d.web || {}, c = d.accounts || {};
    chart($('ch-active'), a.active_installs, 'n', 'line', 'Active installs per day', '#3a8ee8');
    chart($('ch-new'), a.new_installs, 'n', 'bar', 'New installs per day', '#3cc85a');
    barList($('l-versions'), a.versions, 'installs'); barList($('l-os'), a.os, 'installs'); barList($('l-locales'), a.locales, 'installs');
    barList($('l-views'), a.views, 'opens');
    table($('t-features'), [['Feature', 'name'], ['Events', 'events'], ['Installs', 'installs']], a.features);
    table($('t-events'), [['Event', 'name'], ['Events', 'events'], ['Installs', 'installs']], a.top_events);
    chart($('ch-web'), (w.daily || []).map(function (r) { return { day: r.day, n: r.visitors }; }), 'n', 'line', 'Website visitors per day', '#ffb52e');
    table($('t-pages'), [['Page', 'k'], ['Views', 'n'], ['Visitors', 'visitors']], w.top_pages);
    barList($('l-refs'), w.referrers, 'visitors'); barList($('l-dl'), w.download_clicks, 'clicks');
    chart($('ch-signups'), c.signups, 'n', 'bar', 'Sign-ups per day', '#ff7a1a');
    $('ad-gen').textContent = 'Last ' + d.days + ' days, updated ' + new Date(d.generated_at).toLocaleString() + '. Days are UTC.';
  }

  function denied(err) {
    return err && (err.code === '42501' || err.status === 403 || err.code === 'PGRST301' || /not authori[sz]ed|permission denied/i.test(err.message || ''));
  }

  function load() {
    var seq = ++loadSeq;
    msg('ad-status', 'Loading…'); $('ad-data').hidden = true;
    client.rpc('admin_stats', { days: days }).then(function (r) {
      if (seq !== loadSeq) return;
      if (r.error) {
        if (denied(r.error)) { clear($('ad-kpis')); $('ad-data').hidden = true; show('ad-denied'); return; }
        msg('ad-status', 'Could not load the statistics. Try Refresh.', true); return;
      }
      if (!r.data) { msg('ad-status', 'No data returned.', true); return; }
      render(r.data); $('ad-data').hidden = false; msg('ad-status', '');
    }).catch(function () { if (seq === loadSeq) msg('ad-status', 'Could not load the statistics. Try Refresh.', true); });
  }

  function signOut() { client.auth.signOut().finally(function () { $('ad-data').hidden = true; show('ad-out'); }); }

  function init() {
    if (!root.supabase || !root.supabase.createClient) { show('ad-out'); msg('ad-msg', 'Sign-in could not load. Reload the page.', true); return; }
    client = root.supabase.createClient(SUPABASE_URL, SUPABASE_KEY, { auth: { flowType: 'pkce', detectSessionInUrl: true, persistSession: true, autoRefreshToken: true } });
    var emailForm = $('ad-email-form'), codeForm = $('ad-code-form'), email = '';
    $('ad-google').addEventListener('click', function () {
      this.disabled = true;
      client.auth.signInWithOAuth({ provider: 'google', options: { redirectTo: location.origin + '/admin.html' } }).then(function (r) {
        if (r.error) { $('ad-google').disabled = false; msg('ad-msg', r.error.message, true); }
      });
    });
    emailForm.addEventListener('submit', function (e) {
      e.preventDefault();
      email = $('ad-email').value.trim();
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { msg('ad-msg', 'Enter a valid email address.', true); return; }
      $('ad-send').disabled = true; msg('ad-msg', 'Sending…');
      client.auth.signInWithOtp({ email: email, options: { shouldCreateUser: false } }).then(function (r) {
        $('ad-send').disabled = false;
        if (r.error) { msg('ad-msg', 'Could not send a code.', true); return; }
        emailForm.hidden = true; codeForm.hidden = false; msg('ad-msg', 'Code sent.'); $('ad-code').focus();
      });
    });
    codeForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var code = $('ad-code').value.replace(/\s/g, '');
      if (!/^\d{6,8}$/.test(code)) { msg('ad-msg', 'Enter the code from the email.', true); return; }
      $('ad-verify').disabled = true; msg('ad-msg', 'Checking…');
      client.auth.verifyOtp({ email: email, token: code, type: 'email' }).then(function (r) {
        $('ad-verify').disabled = false;
        msg('ad-msg', r.error ? 'That code did not work.' : '', !!r.error);
      });
    });
    Array.prototype.forEach.call(document.querySelectorAll('.ad-seg button'), function (b) {
      b.addEventListener('click', function () {
        days = Number(b.getAttribute('data-d'));
        Array.prototype.forEach.call(document.querySelectorAll('.ad-seg button'), function (x) { x.setAttribute('aria-pressed', x === b ? 'true' : 'false'); });
        load();
      });
    });
    $('ad-refresh').addEventListener('click', load);
    $('ad-out-btn').addEventListener('click', signOut);
    $('ad-denied-out').addEventListener('click', signOut);
    $('ad-purge').addEventListener('click', function () {
      if (!root.confirm('Delete all raw events older than 180 days? This cannot be undone.')) return;
      var b = this; b.disabled = true; msg('ad-purge-msg', 'Purging…');
      client.rpc('purge_telemetry', { keep_days: 180 }).then(function (r) {
        b.disabled = false;
        if (r.error) { msg('ad-purge-msg', 'Purge failed.', true); return; }
        msg('ad-purge-msg', 'Deleted ' + num(r.data) + ' events.'); load();
      });
    });
    client.auth.onAuthStateChange(function (event, session) {
      if (session && session.user) setTimeout(function () { show('ad-in'); load(); }, 0);
      else if (event === 'SIGNED_OUT') { $('ad-data').hidden = true; show('ad-out'); }
    });
    client.auth.getSession().then(function (r) {
      if (location.search.indexOf('code=') >= 0 || location.hash.indexOf('access_token') >= 0) history.replaceState(null, '', location.pathname);
      if (r.data && r.data.session) { show('ad-in'); load(); } else show('ad-out');
    }).catch(function () { show('ad-out'); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof window !== 'undefined' ? window : globalThis);
