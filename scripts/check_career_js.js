#!/usr/bin/env node
/* Compares site/career.js computeStats() with chcompanion.playstats.compute() on a generated fixture.
 * Usage (from the repo root): node scripts/check_career_js.js     (needs python3 with the app's dependencies)
 * Both sides run in UTC with a fixed "now", so day/hour buckets line up. */
process.env.TZ = 'UTC';
const { execFileSync } = require('child_process');
const fs = require('fs'), os = require('os'), path = require('path');
const root = path.resolve(__dirname, '..');
const { computeStats } = require(path.join(root, 'site', 'career.js'));
const NOW = '2026-11-10T12:00:00+00:00';

let s = 42; const rnd = () => (s = (s * 1103515245 + 12345) % 2147483648) / 2147483648;
const songs = [['Metallica', 'Master of Puppets'], ['Rush', 'Tom Sawyer'], ['Muse', 'Hysteria'], ['Sigur Rós', 'Hoppípolla'], ['AC/DC', 'Thunderstruck'], ['<b>X</b>', 'Café']];
const ins = ['guitar', 'bass', 'drums', 'keys', 'vocals', null], dif = ['easy', 'medium', 'hard', 'expert'];
const plays = [];
for (let i = 0; i < 120; i++) {
  const sg = songs[Math.floor(rnd() * songs.length)], tn = Math.floor(rnd() * 1500), hit = Math.floor(tn * rnd());
  plays.push({
    played_at: new Date(Date.parse(NOW) - Math.floor(rnd() * 150 * 864e5)).toISOString(), artist: sg[0], name: sg[1],
    instrument: ins[Math.floor(rnd() * ins.length)], difficulty: dif[Math.floor(rnd() * 4)], score: Math.floor(rnd() * 200000),
    stars: rnd() < .1 ? null : Math.floor(rnd() * 8), accuracy: rnd() < .15 ? null : Math.round(rnd() * 1e4) / 1e4, notes_hit: hit, total_notes: tn,
    max_streak: Math.floor(hit * rnd()), fc: rnd() < .15, duration_s: rnd() < .1 ? null : Math.round(rnd() * 3000) / 10,
  });
}
const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'career-')), 'plays.json');
fs.writeFileSync(file, JSON.stringify(plays));
const py = `
import json, sys
from datetime import datetime
from chcompanion import playstats
plays = json.load(open(sys.argv[1]))
r = playstats.compute(plays, now=datetime.fromisoformat(sys.argv[2]))
print(json.dumps({"totals": r["totals"], "weekday": r["weekday"], "hour": r["hour"],
  "starCounts": r.get("starCounts") or r["totals"].get("starCounts"), "heat": sum(d["n"] for d in r["heatmap"]["days"]), "most": [c["ident"] for c in r["mostPlayed"]][:5],
  "best": [c["ident"] for c in r["best"]][:5], "pbs": [p["score"] for p in r["personalBests"]]}))`;
const ref = JSON.parse(execFileSync('python3', ['-c', py, file, NOW], { cwd: root, env: { ...process.env, TZ: 'UTC' } }));
const js = computeStats(plays, new Date(NOW));

let bad = 0;
function eq(label, a, b) {
  const ok = typeof a === 'number' && typeof b === 'number' ? Math.abs(a - b) < 1e-9 : JSON.stringify(a) === JSON.stringify(b);
  if (!ok) { bad++; console.log('MISMATCH', label, 'js=', JSON.stringify(a), 'py=', JSON.stringify(b)); }
}
for (const k of ['plays', 'hours', 'songs', 'avgAccuracy', 'fcs', 'fiveStars', 'totalScore', 'notesHit', 'streak', 'longestStreak']) eq('totals.' + k, js.totals[k], ref.totals[k]);
eq('weekday', js.weekday, ref.weekday); eq('hour', js.hour, ref.hour);
eq('heatmap total', js.heatmap.days.reduce((t, d) => t + d.n, 0), ref.heat);
eq('mostPlayed', js.mostPlayed.map(c => c.ident).slice(0, 5), ref.most);
eq('best', js.best.map(c => c.ident).slice(0, 5), ref.best);
eq('personalBests', js.personalBests.map(p => p.score), ref.pbs);
// starCounts: compare once the app's compute() returns it (a separate app change).
if (ref.starCounts) { for (const k of ['5', '6', '7']) eq('starCounts.' + k, js.totals.starCounts[k], ref.starCounts[k]); }
else console.log('note: python compute() has no starCounts yet; JS values', JSON.stringify(js.totals.starCounts));
console.log(bad ? bad + ' mismatches' : 'OK: career.js agrees with playstats.compute() on ' + plays.length + ' plays');
process.exit(bad ? 1 : 0);
