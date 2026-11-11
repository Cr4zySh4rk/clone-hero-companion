/* Anonymous page statistics (see /privacy.html). No cookies, no localStorage, no IP addresses stored.
   Sends: the page path (no query), the referrer's domain, a screen-width bucket, and a random per-tab id kept in
   sessionStorage. Does nothing when the browser sends Do Not Track or Global Privacy Control. */
(function (w) {
  'use strict';
  var URL_ = 'https://muhboxuefazapytpqzjc.supabase.co/rest/v1/rpc/track';
  var KEY = 'sb_publishable_MrDFLihLJ8El-VQ3WvfhEQ_BArBKEWx';
  var n = w.navigator || {};
  var dnt = n.doNotTrack === '1' || n.doNotTrack === 'yes' || w.doNotTrack === '1' || n.msDoNotTrack === '1';
  if (dnt || n.globalPrivacyControl === true || w.chcTrack) { w.chcTrack = w.chcTrack || function () {}; return; }

  function uuid() {
    if (w.crypto && w.crypto.randomUUID) return w.crypto.randomUUID();
    var b = new Uint8Array(16);
    if (w.crypto && w.crypto.getRandomValues) w.crypto.getRandomValues(b); else for (var i = 0; i < 16; i++) b[i] = Math.random() * 256;
    b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
    var h = Array.prototype.map.call(b, function (x) { return ('0' + x.toString(16)).slice(-2); }).join('');
    return h.slice(0, 8) + '-' + h.slice(8, 12) + '-' + h.slice(12, 16) + '-' + h.slice(16, 20) + '-' + h.slice(20);
  }
  var sid = null;
  try { sid = w.sessionStorage.getItem('chc_sid'); } catch (e) { /* blocked */ }
  if (!sid) { sid = uuid(); try { w.sessionStorage.setItem('chc_sid', sid); } catch (e) { /* kept in memory for this page */ } }

  function send(name, props) {
    try {
      var body = JSON.stringify({ events: [{ source: 'web', session_id: sid, name: name, props: props || {}, locale: ((n.language || '').split(',')[0] || '').slice(0, 20).replace(/[^A-Za-z0-9-]/g, '') || undefined }] });
      w.fetch(URL_, { method: 'POST', keepalive: true, headers: { 'Content-Type': 'application/json', apikey: KEY, Authorization: 'Bearer ' + KEY }, body: body }).catch(function () {});
    } catch (e) { /* never break the page */ }
  }
  w.chcTrack = send;

  function refDomain() {
    try {
      if (!document.referrer) return '';
      var h = new URL(document.referrer).hostname.replace(/^www\./, '');
      return h === location.hostname.replace(/^www\./, '') ? '' : h.slice(0, 40);
    } catch (e) { return ''; }
  }
  function widthBucket() {
    var s = (w.screen && w.screen.width) || w.innerWidth || 0;
    return s < 480 ? 'xs' : s < 768 ? 'sm' : s < 1200 ? 'md' : 'lg';
  }
  send('pageview', { path: (location.pathname || '/').slice(0, 40), ref_domain: refDomain(), w_bucket: widthBucket() });

  // download buttons (built by app.js from the GitHub release, so listen on the document)
  document.addEventListener('click', function (e) {
    var a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
    if (!a) return;
    var h = a.getAttribute('href') || '';
    if (!/github\.com\/[^/]+\/[^/]+\/releases/i.test(h) && a.id !== 'dl-btn') return;
    var os = /macos|\.dmg|\.pkg/i.test(h) ? 'macos' : /windows|\.msi|\.exe/i.test(h) ? 'windows' : /linux|appimage|\.tar\.gz|\.deb/i.test(h) ? 'linux' : 'other';
    send('download_click', { os: os });
  }, true);
})(window);
