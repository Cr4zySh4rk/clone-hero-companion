/* Clone Hero Companion: Career page.
 *
 * Signed-in users see their own play stats, computed in the browser from the `plays` rows the desktop app
 * synced to Supabase. The stats code (computeStats) is a port of chcompanion/playstats.py compute(), so the
 * numbers agree with the app's Profile. scripts/check_career_js.js compares the two.
 *
 * Rank / XP (documented on the page too):
 *   XP            = floor(notes hit / 20) + 50 * plays
 *   XP to reach rank R = round(500 * (R - 1) ^ 1.7)     (rank 1 starts at 0 XP)
 * The exponent makes each rank cost more than the last, like GTA Online: rank 2 needs 500 XP, rank 10 about
 * 21,000, rank 50 about 390,000, rank 100 about 1.2 million.
 */
(function (root) {
  'use strict';

  var SUPABASE_URL = 'https://muhboxuefazapytpqzjc.supabase.co';
  // Public "publishable" key (same one the desktop app ships). Row-level security protects the data.
  var SUPABASE_KEY = 'sb_publishable_MrDFLihLJ8El-VQ3WvfhEQ_BArBKEWx';
  var REDIRECT_TO = 'https://cloneherocompanion.com/career.html';
  var LB_SITE = 'https://leaderboards.clonehero.net';
  var PAGE = 1000;
  var DIFFICULTIES = ['easy', 'medium', 'hard', 'expert'];
  var PLAY_COLUMNS = 'id,played_at,checksum,artist,name,charter,instrument,difficulty,speed,score,stars,accuracy,notes_hit,total_notes,max_streak,fc,completed,duration_s';

  /* ------------------------------------------------------------------ rank */
  var XP_BASE = 500, XP_POWER = 1.7, XP_PER_PLAY = 50, NOTES_PER_XP = 20;

  function xpForRank(r) { return r <= 1 ? 0 : Math.round(XP_BASE * Math.pow(r - 1, XP_POWER)); }
  function xpTotal(totals) {
    return Math.floor((totals.notesHit || 0) / NOTES_PER_XP) + XP_PER_PLAY * (totals.plays || 0);
  }
  function rankInfo(xp) {
    var r = Math.max(1, Math.floor(Math.pow(xp / XP_BASE, 1 / XP_POWER)) + 1);
    while (xpForRank(r + 1) <= xp) r++;
    while (r > 1 && xpForRank(r) > xp) r--;
    var lo = xpForRank(r), hi = xpForRank(r + 1);
    return { rank: r, xp: xp, lo: lo, hi: hi, pct: hi > lo ? Math.min(1, (xp - lo) / (hi - lo)) : 0 };
  }

  /* ------------------------------------------------------------------ stats (port of playstats.compute) */
  function normKey(s) {
    s = String(s == null ? '' : s).replace(/<[^>]*>/g, '').trim();
    s = s.normalize('NFKD').toLowerCase().replace(/ß/g, 'ss').replace(/[̀-ͯ]/g, '');
    return s.replace(/[^0-9a-z]+/g, ' ').trim();
  }
  function songIdent(p) { return normKey((p.artist || '') + ' - ' + (p.name || '')); }
  function acc(p) {
    var a = p.accuracy;
    if (a == null && p.total_notes) a = (p.notes_hit || 0) / p.total_notes;
    return a == null ? null : Number(a);
  }
  function dayNum(d) { return Math.floor(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) / 864e5); }
  function dayIso(n) { return new Date(n * 864e5).toISOString().slice(0, 10); }
  function mondayIndex(d) { return (d.getDay() + 6) % 7; }
  function r4(x) { return Math.round(x * 1e4) / 1e4; }
  function sum(arr, f) { var t = 0; for (var i = 0; i < arr.length; i++) t += f(arr[i]); return t; }
  function maxBy(arr, f) { var b = arr[0], bv = f(b); for (var i = 1; i < arr.length; i++) { var v = f(arr[i]); if (v > bv) { b = arr[i]; bv = v; } } return b; }
  function str(v) { return v == null ? '' : String(v); }

  function computeStats(plays, now) {
    now = now || new Date();
    var dts = [];
    plays.forEach(function (p) {
      var d = new Date(p.played_at);
      if (!isNaN(d.getTime())) dts.push({ p: p, d: d });
    });
    var rows = dts.map(function (x) { return x.p; });
    var totalS = sum(rows, function (p) { return Number(p.duration_s) || 0; });
    var accs = rows.map(acc).filter(function (a) { return a != null; });
    var daySet = {};
    dts.forEach(function (x) { daySet[dayNum(x.d)] = true; });
    var today = dayNum(now);
    var dayList = Object.keys(daySet).map(Number).sort(function (a, b) { return a - b; });
    var longest = 0, run = 0, prev = null;
    dayList.forEach(function (day) {
      run = prev !== null && day - prev === 1 ? run + 1 : 1;
      longest = Math.max(longest, run);
      prev = day;
    });
    var cur = 0, day = daySet[today] ? today : today - 1;
    while (daySet[day]) { cur++; day--; }

    // heatmap: last 26 weeks, weeks start on Monday
    var start = today - mondayIndex(now) - 25 * 7;
    var perDay = {};
    dts.forEach(function (x) { var n = dayNum(x.d); if (n >= start) perDay[n] = (perDay[n] || 0) + 1; });
    var heat = [];
    for (var i = 0; i <= today - start; i++) heat.push({ d: dayIso(start + i), n: perDay[start + i] || 0 });
    var weekday = [0, 0, 0, 0, 0, 0, 0], hour = [];
    for (i = 0; i < 24; i++) hour.push(0);
    dts.forEach(function (x) { weekday[mondayIndex(x.d)]++; hour[x.d.getHours()]++; });

    // per song
    var bySong = {}, order = [];
    rows.forEach(function (p) {
      var k = songIdent(p);
      if (!bySong[k]) { bySong[k] = []; order.push(k); }
      bySong[k].push(p);
    });
    function card(ps, ident) {
      var last = ps[0];
      ps.forEach(function (p) { if (str(p.played_at) > str(last.played_at)) last = p; });
      var as = ps.map(acc).filter(function (a) { return a != null; });
      var best = maxBy(ps, function (p) { return p.score || 0; });
      return {
        ident: ident, artist: last.artist, name: last.name, charter: last.charter, plays: ps.length,
        avgAccuracy: as.length ? r4(sum(as, function (a) { return a; }) / as.length) : null,
        bestAccuracy: as.length ? r4(Math.max.apply(null, as)) : null,
        bestScore: best.score || 0,
        bestStars: Math.max.apply(null, ps.map(function (p) { return Math.floor(p.stars || 0); })),
        notes: Math.max.apply(null, ps.map(function (p) { return Math.floor(p.total_notes || 0); })),
        last: last.played_at, fc: ps.some(function (p) { return p.fc; })
      };
    }
    var cards = order.map(function (k) { return card(bySong[k], k); });
    var most = cards.slice().sort(function (a, b) {
      return (b.plays - a.plays) || (str(a.last) < str(b.last) ? -1 : str(a.last) > str(b.last) ? 1 : 0);
    }).slice(0, 10);
    var best = cards.filter(function (c) { return c.bestAccuracy != null && (c.plays >= 2 || c.notes >= 200); })
      .sort(function (a, b) { return (b.bestAccuracy - a.bestAccuracy) || (b.plays - a.plays); }).slice(0, 10);
    var needs = cards.filter(function (c) { return c.bestAccuracy != null; })
      .sort(function (a, b) { return (a.bestAccuracy - b.bestAccuracy) || (a.bestScore - b.bestScore) || (b.plays - a.plays); }).slice(0, 10);

    function playRow(p) {
      return { playedAt: p.played_at, artist: p.artist, name: p.name, charter: p.charter, score: p.score || 0,
        stars: p.stars || 0, accuracy: acc(p), difficulty: p.difficulty, instrument: p.instrument,
        fc: !!p.fc, speed: p.speed || 100 };
    }
    var recent = rows.slice().sort(function (a, b) { return str(b.played_at) < str(a.played_at) ? -1 : str(b.played_at) > str(a.played_at) ? 1 : 0; })
      .slice(0, 15).map(playRow);

    // charts (song + instrument + difficulty): personal bests, and distinct FC / 5-star counts
    var charts = {}, chartOrder = [];
    rows.forEach(function (p) {
      var k = songIdent(p) + '\u0000' + p.instrument + '\u0000' + p.difficulty;
      if (!charts[k]) { charts[k] = []; chartOrder.push(k); }
      charts[k].push(p);
    });
    var pbs = chartOrder.map(function (k) { return maxBy(charts[k], function (p) { return p.score || 0; }); })
      .sort(function (a, b) { return (b.score || 0) - (a.score || 0); }).slice(0, 15).map(playRow);
    var fcs = 0, five = 0, starCounts = { '5': 0, '6': 0, '7': 0 };
    chartOrder.forEach(function (k) {
      if (charts[k].some(function (p) { return p.fc; })) fcs++;
      if (charts[k].some(function (p) { return (p.stars || 0) >= 5; })) five++;
      // best (highest) stars of the chart: exactly 5, 6 (gold) or 7
      var top = Math.max.apply(null, charts[k].map(function (p) { return Math.floor(p.stars || 0); }));
      if (starCounts[top] !== undefined) starCounts[top]++;
    });

    var totals = {
      plays: rows.length, hours: Math.round(totalS / 360) / 10, seconds: totalS, songs: order.length,
      avgAccuracy: accs.length ? r4(sum(accs, function (a) { return a; }) / accs.length) : null,
      fcs: fcs, fiveStars: five, starCounts: starCounts,
      totalScore: Math.floor(sum(rows, function (p) { return p.score || 0; })),
      notesHit: Math.floor(sum(rows, function (p) { return p.notes_hit || 0; })),
      streak: cur, longestStreak: longest
    };

    // extras the app's Profile does not show
    var withStars = rows.filter(function (p) { return p.stars != null; });
    var sized = rows.filter(function (p) { return acc(p) != null && (p.total_notes || 0) >= 100; });
    if (!sized.length) sized = rows.filter(function (p) { return acc(p) != null; });
    var firstMs = null;
    dts.forEach(function (x) { if (firstMs === null || x.d.getTime() < firstMs) firstMs = x.d.getTime(); });
    var weeks = firstMs === null ? 1 : Math.max(1, (today - dayNum(new Date(firstMs)) + 1) / 7);
    var extras = {
      firstPlay: firstMs === null ? null : new Date(firstMs),
      avgStars: withStars.length ? sum(withStars, function (p) { return p.stars || 0; }) / withStars.length : null,
      bestAccuracyRun: sized.length ? playRow(maxBy(sized, function (p) { return acc(p) * 1e12 + (p.score || 0); })) : null,
      highestScore: rows.length ? playRow(maxBy(rows, function (p) { return p.score || 0; })) : null,
      longestNoteStreak: rows.length ? maxBy(rows, function (p) { return p.max_streak || 0; }) : null,
      playsPerWeek: rows.length ? rows.length / weeks : 0,
      topWeekday: rows.length ? weekday.indexOf(Math.max.apply(null, weekday)) : null,
      topHour: rows.length ? hour.indexOf(Math.max.apply(null, hour)) : null
    };
    if (extras.longestNoteStreak) {
      extras.longestNoteStreak = { streak: extras.longestNoteStreak.max_streak || 0, artist: extras.longestNoteStreak.artist, name: extras.longestNoteStreak.name };
    }

    // per instrument
    var inst = {};
    rows.forEach(function (p) { var k = p.instrument || 'unknown'; (inst[k] = inst[k] || []).push(p); });
    var instruments = Object.keys(inst).map(function (k) {
      var ps = inst[k], as = ps.map(acc).filter(function (a) { return a != null; });
      var ch = {}, fcn = 0, dc = {};
      ps.forEach(function (p) {
        var ck = songIdent(p) + '\u0000' + p.difficulty;
        if (p.fc && !ch[ck]) { ch[ck] = 1; fcn++; }
        dc[p.difficulty || 'unknown'] = (dc[p.difficulty || 'unknown'] || 0) + 1;
      });
      var fav = null, favN = 0;
      Object.keys(dc).forEach(function (d) {
        var better = dc[d] > favN || (dc[d] === favN && DIFFICULTIES.indexOf(d) > DIFFICULTIES.indexOf(fav));
        if (better) { fav = d; favN = dc[d]; }
      });
      return {
        instrument: k, plays: ps.length, seconds: sum(ps, function (p) { return Number(p.duration_s) || 0; }),
        avgAccuracy: as.length ? sum(as, function (a) { return a; }) / as.length : null, fcs: fcn, favDifficulty: fav
      };
    }).sort(function (a, b) { return b.plays - a.plays; });

    return {
      totals: totals, extras: extras, instruments: instruments,
      heatmap: { start: dayIso(start), days: heat }, weekday: weekday, hour: hour,
      mostPlayed: most, best: best, needsPractice: needs, recent: recent, personalBests: pbs
    };
  }

  /* ------------------------------------------------------------------ app snapshot (profiles.career)
   * The desktop app uploads its own numbers on each cloud sync: star and FC counts that also use Clone Hero's
   * scoredata.bin, and the linked CH Leaderboards account's stats and ranks (browsers cannot call that API: no CORS). */
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function validSnapshot(c) {
    return c && typeof c === 'object' && c.v === 1 && c.totals && typeof c.totals === 'object' ? c : null;
  }
  function rankOf(ranks, names) {
    for (var i = 0; ranks && i < names.length; i++) if (isNum(ranks[names[i]]) && ranks[names[i]] > 0) return ranks[names[i]];
    return null;
  }
  // The app's own full combo and 5/6/7-star counts (plays, CH Leaderboards rows and Clone Hero's scoredata.bin) come from
  // the snapshot: it sees more than the synced plays do. The CH Leaderboards account figures are shown separately, verbatim.
  // FCs keep the app's rule: never lower than what the plays here show.
  function applySnapshot(s, snap) {
    snap = validSnapshot(snap);
    if (!snap) return null;
    var t = s.totals, st = snap.totals;
    if (isNum(st.fcs)) t.fcs = Math.max(t.fcs, st.fcs);
    if (isNum(st.fiveOrMore)) t.fiveStars = Math.max(t.fiveStars, st.fiveOrMore);
    if (st.starCounts) ['5', '6', '7'].forEach(function (k) { if (isNum(st.starCounts[k])) t.starCounts[k] = st.starCounts[k]; });
    return snap;
  }

  var api = { computeStats: computeStats, xpTotal: xpTotal, xpForRank: xpForRank, rankInfo: rankInfo, normKey: normKey,
    applySnapshot: applySnapshot, rankOf: rankOf };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof document === 'undefined') return;

  /* ------------------------------------------------------------------ formatting */
  var nf = new Intl.NumberFormat();
  var nf1 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
  var pf = new Intl.NumberFormat(undefined, { style: 'percent', minimumFractionDigits: 1, maximumFractionDigits: 1 });
  var df = new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'long', day: 'numeric' });
  var dfs = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  var tf = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' });
  var wdf = new Intl.DateTimeFormat(undefined, { weekday: 'long' });
  var hf = new Intl.DateTimeFormat(undefined, { hour: 'numeric' });
  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function n(v) { return nf.format(Math.round(Number(v) || 0)); }
  function pct(v) { return v == null ? '–' : pf.format(v); }
  function dur(s) {
    var m = Math.round(s / 60);
    if (m < 60) return nf.format(m) + ' min';
    return nf1.format(s / 3600) + ' h';
  }
  function dateOf(v) { var d = new Date(v); return isNaN(d.getTime()) ? '' : dfs.format(d); }
  function weekdayName(i) { return wdf.format(new Date(2024, 0, 1 + i)); } // 2024-01-01 is a Monday
  function hourName(h) { return hf.format(new Date(2024, 0, 1, h)); }
  function cap(s) { s = str(s); return s ? s.charAt(0).toUpperCase() + s.slice(1) : s; }
  var INSTR = { guitar: 'Lead Guitar', bass: 'Bass Guitar', rhythm: 'Rhythm guitar', drums: 'Drums', keys: 'Keys', vocals: 'Vocals',
    guitarghl: 'GHL guitar', bassghl: 'GHL bass', unknown: 'Other' };
  function instName(i) { return INSTR[i] || cap(i) || 'Other'; }
  var COLORS = ['var(--green)', 'var(--red)', 'var(--yellow)', 'var(--blue)', 'var(--orange)'];
  function title(p) { return (p.artist ? p.artist + ' – ' : '') + (p.name || 'Unknown song'); }
  function stars(v) { v = Math.max(0, Math.min(5, Math.floor(v || 0))); return '<span class="cr-stars" aria-label="' + v + ' stars" title="' + v + ' stars">' + '★'.repeat(v) + '</span>'; }

  /* ------------------------------------------------------------------ rendering */
  function stat(label, value, opts) {
    opts = opts || {};
    return '<div class="cr-stat"' + (opts.c ? ' style="--c:' + opts.c + '"' : '') + '><dt>' + esc(label) + '</dt><dd>' + esc(value) + '</dd>' +
      (opts.sub ? '<span class="cr-sub">' + esc(opts.sub) + '</span>' : '') + '</div>';
  }
  function grid(items) { return '<dl class="cr-grid">' + items.join('') + '</dl>'; }

  // One set of cards, no duplicated concepts: when the app's snapshot carries the CH Leaderboards profile, its values
  // (verbatim, with "Rank #n") are the source for every stat CH has; otherwise the numbers computed from the plays.
  function chStats(s) {
    var st = s.snap && s.snap.stats;
    return st && (isNum(st.totalScore) || isNum(st.totalFcs) || isNum(st.notesHit)) ? st : null;
  }
  function overview(s) {
    var t = s.totals, st = chStats(s), rk = (s.snap && s.snap.ranks) || {};
    function sub(names) { var r = rankOf(rk, names); return r ? 'Rank #' + n(r) : ''; }
    function card(v, chLabel, appValue, appLabel, c, names, appSub) {
      return st && isNum(v) ? stat(chLabel, n(v), { c: c, sub: sub(names) }) : stat(appLabel, appValue, { c: c, sub: appSub });
    }
    function chOnly(v, label, c, names) { return st && isNum(v) ? stat(label, n(v), { c: c, sub: sub(names) }) : ''; }
    var hrs = st && isNum(st.totalTimePlayedMs) ? Math.round(st.totalTimePlayedMs / 3600000) : null;
    var sc = t.starCounts || {};
    var cards = [
      card(st && st.totalFcs, 'Full combos', n(t.fcs), 'Full combos', COLORS[0], ['totalFcs'], 'distinct charts'),
      chOnly(st && st.totalPfcs, 'Perfect FCs', COLORS[3], ['totalPfcs']),
      card(st && st.totalScore, 'Total score', n(t.totalScore), 'Total score', COLORS[4], ['totalScore']),
      card(st && st.songsPlayed, 'Songs played', n(t.songs), 'Songs', COLORS[2], []),
      card(hrs, 'Hours played', dur(t.seconds), 'Time played', COLORS[0], ['hoursPlayed']),
      card(st && st.notesHit, 'Notes hit', n(t.notesHit), 'Notes hit', COLORS[1], ['notesHit']),
      chOnly(st && st.totalStarsEarned, 'Stars earned', COLORS[2], ['starsEarned', 'totalStarsEarned']),
      chOnly(st && st.highestCombo, 'Highest combo', COLORS[1], []),
      chOnly(s.snap && s.snap.submissionCount, 'Submissions', COLORS[4], ['submissions']),
      stat('Plays', n(t.plays), { c: COLORS[1] }), stat('Average accuracy', pct(t.avgAccuracy), { c: COLORS[3] }),
      stat('5-star songs', n(sc[5]), { c: COLORS[2], sub: 'best result, distinct charts' }), stat('6-star songs', n(sc[6]), { c: COLORS[4], sub: 'gold stars' }),
      stat('7-star songs', n(sc[7]), { c: COLORS[1], sub: 'best result, distinct charts' }),
      stat('Current streak', n(t.streak) + (t.streak === 1 ? ' day' : ' days'), { c: COLORS[4] }),
      stat('Longest streak', n(t.longestStreak) + (t.longestStreak === 1 ? ' day' : ' days'), { c: COLORS[2] })
    ].filter(Boolean);
    return grid(cards);
  }
  function performance(s) {
    var t = s.totals, x = s.extras, g = [];
    g.push(stat('5 stars or gold', n(t.fiveStars), { c: COLORS[2], sub: 'distinct charts' }));
    g.push(stat('Best accuracy run', x.bestAccuracyRun ? pct(x.bestAccuracyRun.accuracy) : '–', { c: COLORS[3], sub: x.bestAccuracyRun ? title(x.bestAccuracyRun) : '' }));
    g.push(stat('Highest score', x.highestScore ? n(x.highestScore.score) : '–', { c: COLORS[4], sub: x.highestScore ? title(x.highestScore) : '' }));
    g.push(stat('Longest note streak', x.longestNoteStreak ? n(x.longestNoteStreak.streak) : '–', { c: COLORS[1], sub: x.longestNoteStreak ? title(x.longestNoteStreak) : '' }));
    g.push(stat('Average stars', x.avgStars == null ? '–' : nf1.format(x.avgStars), { c: COLORS[2], sub: 'per play' }));
    return grid(g);
  }
  function instruments(s) {
    var total = s.totals.plays || 1;
    return '<div class="cr-cards">' + s.instruments.map(function (it, i) {
      var c = COLORS[i % COLORS.length];
      return '<article class="cr-card" style="--c:' + c + '"><h3>' + esc(instName(it.instrument)) + '</h3>' +
        '<div class="cr-bar" role="img" aria-label="' + Math.round(it.plays / total * 100) + '% of your plays"><i style="width:' + (it.plays / total * 100).toFixed(1) + '%"></i></div>' +
        '<dl class="cr-mini"><div><dt>Plays</dt><dd>' + n(it.plays) + '</dd></div><div><dt>Time</dt><dd>' + esc(dur(it.seconds)) + '</dd></div>' +
        '<div><dt>Accuracy</dt><dd>' + pct(it.avgAccuracy) + '</dd></div><div><dt>Full combos</dt><dd>' + n(it.fcs) + '</dd></div>' +
        '<div class="cr-wide"><dt>Favourite difficulty</dt><dd>' + esc(it.favDifficulty ? cap(it.favDifficulty) : '–') + '</dd></div></dl></article>';
    }).join('') + '</div>';
  }
  function habits(s) {
    var t = s.totals, x = s.extras;
    var out = grid([
      stat('Current streak', n(t.streak) + (t.streak === 1 ? ' day' : ' days'), { c: COLORS[4] }),
      stat('Longest streak', n(t.longestStreak) + (t.longestStreak === 1 ? ' day' : ' days'), { c: COLORS[2] }),
      stat('Most active day', x.topWeekday == null ? '–' : weekdayName(x.topWeekday), { c: COLORS[3] }),
      stat('Most active hour', x.topHour == null ? '–' : hourName(x.topHour), { c: COLORS[0] }),
      stat('Plays per week', nf1.format(x.playsPerWeek), { c: COLORS[1] })
    ]);
    var days = s.heatmap.days, mx = Math.max.apply(null, days.map(function (d) { return d.n; }).concat([1]));
    var active = days.filter(function (d) { return d.n > 0; }).length;
    var cells = '', lead = 0; // the heatmap starts on a Monday, so column-flow fills Monday..Sunday
    days.forEach(function (d) {
      var l = d.n === 0 ? 0 : Math.min(4, Math.ceil(d.n / mx * 4));
      cells += '<i data-l="' + l + '" title="' + esc(d.d + ': ' + d.n + (d.n === 1 ? ' play' : ' plays')) + '"></i>';
    });
    var weeks = Math.ceil(days.length / 7);
    out += '<h3 class="cr-h">Last 26 weeks</h3><div class="cr-heatwrap"><div class="cr-heat" style="--weeks:' + weeks + '" role="img" aria-label="Heatmap of plays per day over the last 26 weeks: ' +
      n(active) + ' active days">' + cells + '</div>' +
      '<p class="cr-legend" aria-hidden="true">Less <i data-l="0"></i><i data-l="1"></i><i data-l="2"></i><i data-l="3"></i><i data-l="4"></i> More</p></div>';
    var wmax = Math.max.apply(null, s.weekday.concat([1]));
    out += '<h3 class="cr-h">By weekday</h3><ul class="cr-wk">' + s.weekday.map(function (v, i) {
      return '<li><span>' + esc(weekdayName(i).slice(0, 3)) + '</span><b style="--w:' + (v / wmax * 100).toFixed(1) + '%" aria-hidden="true"></b><em>' + n(v) + '</em></li>';
    }).join('') + '</ul>';
    return out;
  }
  function songList(heading, rows, right, note) {
    if (!rows.length) return '';
    return '<div class="cr-list"><h3>' + esc(heading) + '</h3>' + (note ? '<p class="cr-note">' + esc(note) + '</p>' : '') + '<ol>' + rows.map(function (r) {
      return '<li><span class="cr-t"><b>' + esc(r.name || 'Unknown song') + '</b><small>' + esc(r.artist || '') + '</small></span><span class="cr-r">' + right(r) + '</span></li>';
    }).join('') + '</ol></div>';
  }
  function songs(s) {
    return '<div class="cr-lists">' +
      songList('Most played', s.mostPlayed, function (c) { return n(c.plays) + ' plays'; }) +
      songList('Best songs', s.best, function (c) { return pct(c.bestAccuracy); }, 'Highest personal-best accuracy') +
      songList('Needs practice', s.needsPractice, function (c) { return pct(c.bestAccuracy); }, 'Lowest personal-best accuracy') +
      songList('Personal bests', s.personalBests, function (p) {
        return n(p.score) + '<small>' + esc(instName(p.instrument) + ' · ' + cap(p.difficulty)) + '</small>';
      }, 'Best score per song, instrument and difficulty') + '</div>';
  }
  function recent(s) {
    return '<ol class="cr-recent">' + s.recent.map(function (p) {
      return '<li><span class="cr-t"><b>' + esc(p.name || 'Unknown song') + '</b><small>' + esc(p.artist || '') + ' · ' +
        esc(instName(p.instrument) + ', ' + cap(p.difficulty)) + (p.speed && p.speed !== 100 ? ' · ' + esc(p.speed) + '%' : '') + ' · ' + esc(dateOf(p.playedAt)) + '</small></span>' +
        '<span class="cr-r">' + n(p.score) + '<small>' + pct(p.accuracy) + ' ' + stars(p.stars) + (p.fc ? ' <span class="cr-fc">FC</span>' : '') + '</small></span></li>';
    }).join('') + '</ol>';
  }

  var TABS = [['overview', 'Overview', overview], ['performance', 'Performance', performance], ['instruments', 'Instruments', instruments],
    ['habits', 'Habits', habits], ['songs', 'Songs', songs], ['recent', 'Recent plays', recent]];

  function renderCareer(el, profile, user, s) {
    var meta = (user && user.user_metadata) || {};
    var name = (profile && profile.display_name) || meta.full_name || meta.name || (user && user.email ? user.email.split('@')[0] : 'Player');
    var avatar = (profile && profile.avatar_url) || meta.avatar_url || '';
    var xp = xpTotal(s.totals), ri = rankInfo(xp);
    var since = s.extras.firstPlay ? df.format(s.extras.firstPlay) : null;
    var initial = esc((name.trim().charAt(0) || '?').toUpperCase());
    var head = '<header class="cr-head"><div class="cr-id">' +
      '<div class="cr-avatar" aria-hidden="true">' + initial + (/^https:\/\//i.test(avatar) ? '<img alt="" referrerpolicy="no-referrer" src="' + esc(avatar) + '">' : '') + '</div>' +
      '<div><p class="cr-eyebrow">Career</p><h1>' + esc(name) + '</h1><p class="cr-since">' + (since ? 'Member since ' + esc(since) : 'No plays yet') + '</p></div></div>' +
      '<div class="cr-rank"><div class="cr-rank-n"><span>Rank</span><b>' + n(ri.rank) + '</b></div><div class="cr-xp">' +
      '<div class="cr-xpbar" role="progressbar" aria-label="Progress to rank ' + (ri.rank + 1) + '" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + Math.round(ri.pct * 100) + '"><i style="width:' + (ri.pct * 100).toFixed(1) + '%"></i></div>' +
      '<p>' + n(ri.xp - ri.lo) + ' / ' + n(ri.hi - ri.lo) + ' XP to rank ' + n(ri.rank + 1) + ' <small>(' + n(ri.xp) + ' XP total)</small></p></div></div>' +
      '<button type="button" class="cr-ghost" id="cr-signout">Sign out</button></header>';
    var body = '';
    if (!s.totals.plays) {
      body = '<div class="cr-empty"><h2>No plays yet</h2><p>Stats come from plays the Clone Hero Companion app has synced. Sign in to the app with this account, play a few songs in Clone Hero, and they will show up here.</p>' +
        '<p><a href="/#download">Get the app</a> &middot; <a href="/guide.html">Read the guide</a></p></div>';
    } else {
      body = '<div class="tabs cr-tabs" role="tablist" aria-label="Career categories">' + TABS.map(function (t, i) {
        return '<button type="button" role="tab" id="cr-tab-' + t[0] + '" aria-controls="cr-panel-' + t[0] + '" aria-selected="' + (i === 0) + '" tabindex="' + (i === 0 ? 0 : -1) + '">' + t[1] + '</button>';
      }).join('') + '</div>' + TABS.map(function (t, i) {
        return '<div class="tabpanel" role="tabpanel" id="cr-panel-' + t[0] + '" aria-labelledby="cr-tab-' + t[0] + '" tabindex="0"' + (i ? ' hidden' : '') + '>' +
          (t[0] === 'overview' ? lbCard(profile, s) + t[2](s) : t[2](s)) + '</div>';
      }).join('');
    }
    body += '<details class="cr-how"><summary>How rank works</summary><p>XP = notes hit &divide; 20 + 50 for every play. Reaching rank <i>R</i> takes 500 &times; (<i>R</i>&minus;1)<sup>1.7</sup> XP, so every rank costs more than the one before: rank 2 is 500 XP, rank 10 about 21,000, rank 50 about 390,000. It is just for fun and is not a CH Leaderboards rank.</p></details>';
    el.innerHTML = head + body;
    var img = el.querySelector('.cr-avatar img');
    if (img) img.addEventListener('error', function () { img.remove(); });
    initTabs(el);
  }
  // CH Leaderboards' API sends no CORS headers (checked), so browsers cannot call it. The desktop app uploads the linked
  // account's stats and ranks with its career snapshot; without one, link out.
  function lbCard(profile, s) {
    var snap = s.snap, u = (snap && snap.ch_username) || (profile && profile.ch_username);
    if (!u) return '';
    var link = snap && isNum(snap.ch_account_id) ? LB_SITE + '/users/' + Math.floor(snap.ch_account_id) : LB_SITE;
    if (!chStats(s)) {
      return '<p class="cr-lb">Linked CH Leaderboards account: <b>' + esc(u) + '</b>. Its figures are not available on this page yet; open the Clone Hero Companion app once while signed in to sync them here, or <a href="' + link + '" rel="noopener">open CH Leaderboards</a> and search for your name.</p>';
    }
    var since = snap.createdAt && !isNaN(new Date(snap.createdAt).getTime()) ? ' &middot; Since ' + esc(dfs.format(new Date(snap.createdAt))) : '';
    function top(title, rows, key, fmt) {
      if (!rows || !rows.length) return '';
      return '<p class="cr-note"><b>' + title + ':</b> ' + rows.slice(0, 5).map(function (r) { return esc(fmt(r[key])) + ' (' + n(r.plays) + ')'; }).join(' &middot; ') + '</p>';
    }
    return '<p class="cr-note cr-chline">CH Leaderboards: <b>' + esc(u) + '</b>' + since + ' &middot; ' + esc(chAsOf(s)) +
      ' &middot; <a href="' + link + '" rel="noopener">View on CH Leaderboards</a></p>' +
      top('Top artists played', snap.topArtists, 'artist', function (a) { return a; }) +
      top('Top instruments', snap.topInstruments, 'instrument', instName);
  }
  function chAsOf(s) {
    var d = s.snapAt ? new Date(s.snapAt) : null;
    return d && !isNaN(d.getTime()) ? 'CH data as of ' + dfs.format(d) + ', ' + tf.format(d) : 'CH data from the app\u2019s last sync';
  }
  function initTabs(el) {
    var tabs = Array.prototype.slice.call(el.querySelectorAll('[role=tab]'));
    function select(tab, focus) {
      tabs.forEach(function (t) {
        var on = t === tab;
        t.setAttribute('aria-selected', on);
        t.tabIndex = on ? 0 : -1;
        document.getElementById(t.getAttribute('aria-controls')).hidden = !on;
      });
      if (focus) tab.focus();
    }
    tabs.forEach(function (t, i) {
      t.addEventListener('click', function () { select(t); });
      t.addEventListener('keydown', function (e) {
        var j = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
        if (j === undefined) return;
        e.preventDefault();
        select(tabs[(j + tabs.length) % tabs.length], true);
      });
    });
  }

  /* ------------------------------------------------------------------ data + auth */
  var $ = function (id) { return document.getElementById(id); };
  var client = null, shownFor = null;

  function fetchAllPlays(uid) {
    var all = [];
    function page(from) {
      return client.from('plays').select(PLAY_COLUMNS).eq('user_id', uid)
        .order('played_at', { ascending: false }).order('id', { ascending: false }).range(from, from + PAGE - 1)
        .then(function (r) {
          if (r.error) throw r.error;
          all = all.concat(r.data || []);
          return (r.data || []).length === PAGE ? page(from + PAGE) : all;
        });
    }
    return page(0);
  }
  function show(which) {
    ['cr-loading', 'cr-out', 'cr-in'].forEach(function (id) { $(id).hidden = id !== which; });
  }
  function msg(id, text, bad) {
    var m = $(id);
    m.textContent = text || '';
    m.className = 'cr-msg' + (bad ? ' bad' : '');
  }
  function loadCareer(session) {
    var uid = session.user.id;
    if (shownFor === uid) return;
    shownFor = uid;
    show('cr-loading');
    msg('cr-load-msg', 'Loading your stats…');
    Promise.all([
      client.from('profiles').select('display_name,avatar_url,ch_username').eq('id', uid).maybeSingle(),
      fetchAllPlays(uid),
      // the app's snapshot; a failure here (column not there yet) only means "no snapshot"
      client.from('profiles').select('career,career_at,ch_username').eq('id', uid).maybeSingle()
        .then(function (r) { return r; }, function () { return { error: true }; })
    ]).then(function (res) {
      if (res[0].error) res[0] = { data: null };
      var snapRow = res[2] && !res[2].error ? res[2].data : null, stats = computeStats(res[1], new Date());
      stats.snap = applySnapshot(stats, snapRow && snapRow.career);
      stats.snapAt = stats.snap && snapRow.career_at ? snapRow.career_at : null;
      renderCareer($('cr-in'), res[0].data, session.user, stats);
      show('cr-in');
      $('cr-signout').addEventListener('click', signOut);
      $('cr-in').querySelector('h1').focus && $('cr-in').querySelector('h1').setAttribute('tabindex', '-1');
    }).catch(function (e) {
      shownFor = null;
      show('cr-loading');
      msg('cr-load-msg', 'Could not load your stats: ' + ((e && e.message) || 'network error') + '. Reload the page to try again.', true);
    });
  }
  function signOut() {
    shownFor = null;
    client.auth.signOut().finally(function () { show('cr-out'); });
  }

  function init() {
    if (!root.supabase || !root.supabase.createClient) {
      show('cr-out');
      msg('cr-msg', 'Sign-in could not load. Check your connection and reload the page.', true);
      return;
    }
    client = root.supabase.createClient(SUPABASE_URL, SUPABASE_KEY, { auth: { flowType: 'pkce', detectSessionInUrl: true, persistSession: true, autoRefreshToken: true } });
    var q = new URLSearchParams(location.search + '&' + location.hash.replace(/^#/, ''));
    if (q.get('error_description')) msg('cr-msg', q.get('error_description').replace(/\+/g, ' '), true);

    var emailForm = $('cr-email-form'), codeForm = $('cr-code-form'), email = '';
    $('cr-google').addEventListener('click', function () {
      this.disabled = true;
      client.auth.signInWithOAuth({ provider: 'google', options: { redirectTo: REDIRECT_TO } }).then(function (r) {
        if (r.error) { $('cr-google').disabled = false; msg('cr-msg', r.error.message, true); }
      });
    });
    emailForm.addEventListener('submit', function (e) {
      e.preventDefault();
      email = $('cr-email').value.trim();
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { msg('cr-msg', 'Enter a valid email address.', true); return; }
      $('cr-send').disabled = true;
      msg('cr-msg', 'Sending…');
      // Same as the desktop app: shouldCreateUser (create_user) is true.
      client.auth.signInWithOtp({ email: email, options: { shouldCreateUser: true } }).then(function (r) {
        $('cr-send').disabled = false;
        if (r.error) { msg('cr-msg', r.error.status === 429 ? 'Too many codes requested. Wait a few minutes and try again.' : r.error.message, true); return; }
        emailForm.hidden = true; codeForm.hidden = false;
        $('cr-sent-to').textContent = email;
        msg('cr-msg', 'Code sent. It can take a minute to arrive.');
        $('cr-code').focus();
      });
    });
    codeForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var code = $('cr-code').value.replace(/\s/g, '');
      if (!/^\d{6}$/.test(code)) { msg('cr-msg', 'The code is 6 digits.', true); return; }
      $('cr-verify').disabled = true;
      msg('cr-msg', 'Checking…');
      client.auth.verifyOtp({ email: email, token: code, type: 'email' }).then(function (r) {
        $('cr-verify').disabled = false;
        if (r.error) { msg('cr-msg', r.error.status === 429 ? 'Too many attempts. Wait a few minutes.' : 'That code did not work. Check it and try again.', true); return; }
        msg('cr-msg', '');
      });
    });
    $('cr-back').addEventListener('click', function () { codeForm.hidden = true; emailForm.hidden = false; msg('cr-msg', ''); $('cr-email').focus(); });

    client.auth.onAuthStateChange(function (event, session) {
      if (session && session.user) setTimeout(function () { loadCareer(session); }, 0);
      else if (event === 'SIGNED_OUT') { shownFor = null; show('cr-out'); }
    });
    client.auth.getSession().then(function (r) {
      if (location.search.indexOf('code=') >= 0 || location.hash.indexOf('access_token') >= 0) history.replaceState(null, '', location.pathname);
      if (r.data && r.data.session) loadCareer(r.data.session); else show('cr-out');
    }).catch(function () { show('cr-out'); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof window !== 'undefined' ? window : globalThis);
