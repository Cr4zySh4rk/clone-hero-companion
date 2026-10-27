"""Play history: captures Clone Hero's scorestats.json and computes the Profile stats.

Clone Hero overwrites <game folder>/scorestats.json after every finished song. This module
polls that file, appends one record per player to <config>/plays.jsonl (deduplicated by
client_id) and keeps a local copy of plays pulled from the cloud (plays_cloud.jsonl), so the
statistics combine every machine. Everything works offline and signed out.
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .library import norm_key, strip_tags
from .paths import config_dir

log = logging.getLogger(__name__)

SCORESTATS = "scorestats.json"
DIFFICULTIES = ("easy", "medium", "hard", "expert")
_INSTR_ALIASES = {
    "leadguitar": "guitar", "lead": "guitar", "guitar": "guitar", "bassguitar": "bass", "bass": "bass",
    "rhythmguitar": "rhythm", "rhythm": "rhythm", "drums": "drums", "drum": "drums", "prodrums": "drums",
    "keys": "keys", "keyboard": "keys", "vocals": "vocals", "vocal": "vocals", "guitarghl": "guitarghl",
    "ghlguitar": "guitarghl", "6fretguitar": "guitarghl", "bassghl": "bassghl", "ghlbass": "bassghl",
    "6fretbass": "bassghl", "guitarcoop": "guitar", "coopguitar": "guitar",
}


def plays_path() -> Path:
    return config_dir() / "plays.jsonl"


def cloud_plays_path() -> Path:
    return config_dir() / "plays_cloud.jsonl"


def lb_plays_path() -> Path:
    return config_dir() / "plays_leaderboard.jsonl"


LB_SOURCE = "leaderboard"
LB_REFRESH_S = 600          # fetch the linked account's public scores at most every 10 minutes
DEDUPE_WINDOW_S = 300       # same score within 5 minutes = the same play


# ------------------------------------------------------------------ parsing
def norm_instrument(v) -> str:
    s = re.sub(r"[^a-z0-9]", "", str(v if v is not None else "").lower())
    return _INSTR_ALIASES.get(s, s or "unknown")


def norm_difficulty(v) -> str:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        i = int(v)
        return DIFFICULTIES[i] if 0 <= i < 4 else "unknown"
    s = str(v or "").strip().lower()
    if s.isdigit():
        return norm_difficulty(int(s))
    return s if s in DIFFICULTIES else (s or "unknown")


def parse_timestamp(v, fallback: float) -> float:
    """scorestats' score_timestamp as epoch seconds (number, numeric string or ISO text)."""
    try:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            n = float(v)
        elif isinstance(v, str) and re.fullmatch(r"\d+(\.\d+)?", v.strip()):
            n = float(v)
        elif isinstance(v, str) and v.strip():
            d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.timestamp()
        else:
            return fallback
        if n > 1e17:           # .NET ticks
            return (n - 621355968000000000) / 1e7
        if n > 1e11:           # milliseconds
            return n / 1000
        return n if n > 1e8 else fallback
    except Exception:
        return fallback


def _num(v, default=0):
    try:
        return type(default)(float(v)) if v is not None and v != "" else default
    except (TypeError, ValueError):
        return default


def lb_row_to_play(r: dict) -> dict | None:
    """One CH Leaderboards score row (api.clonehero.net) as a play record, source 'leaderboard'.
    The leaderboard's songHash is not Clone Hero's chart checksum, so songs match by artist/title."""
    if not isinstance(r, dict) or r.get("scoreId") is None:
        return None
    ts = parse_timestamp(r.get("createdAt"), 0.0)
    if not ts:
        return None
    speed = int(round(_num(r.get("speed"), 100.0))) or 100
    hit, total = _num(r.get("notesHit")), _num(r.get("noteCount"))
    acc = round(hit / total, 5) if total > 0 else None
    if acc is None:
        try:
            acc = round(float(r.get("accuracy")) / 100, 5)
        except (TypeError, ValueError):
            pass
    dur = _num(r.get("duration"), 0.0) / 1000.0
    return {
        "client_id": f"lb|{r['scoreId']}", "source": LB_SOURCE,
        "played_at": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "checksum": None, "lb_song_hash": r.get("songHash"),
        "artist": strip_tags(str(r.get("artist") or "")), "name": strip_tags(str(r.get("name") or "")),
        "charter": strip_tags(str(r.get("charter") or "")),
        "instrument": norm_instrument(r.get("instrument")), "difficulty": norm_difficulty(r.get("difficulty")),
        "speed": speed, "score": _num(r.get("totalScore")), "stars": _num(r.get("stars")),
        "accuracy": acc, "notes_hit": hit, "total_notes": total, "max_streak": _num(r.get("maxCombo")),
        "fc": bool(r.get("isFc")), "completed": not r.get("failedInRun"),
        "duration_s": round(dur / (speed / 100.0), 1) if dur else None, "machine": "CH Leaderboards",
    }


def parse_scorestats(data: dict, mtime: float = 0.0, machine: str = "",
                     duration: Callable[[str, str, str], float | None] | None = None) -> list[dict]:
    """One play record per player in a scorestats.json document."""
    if not isinstance(data, dict):
        return []
    checksum = str(data.get("checksum") or "").strip().upper()
    artist = strip_tags(str(data.get("artist_name") or ""))
    name = strip_tags(str(data.get("song_name") or ""))
    charter = strip_tags(str(data.get("charter_name") or ""))
    ts_raw = data.get("score_timestamp")
    ts = parse_timestamp(ts_raw, mtime or datetime.now(timezone.utc).timestamp())
    speed = _num(data.get("playback_speed"), 100.0)
    speed = int(round(speed * 100)) if 0 < speed <= 5 else int(round(speed or 100))
    length = duration(checksum, artist, name) if duration else None
    out = []
    for i, p in enumerate(data.get("players") or []):
        if not isinstance(p, dict):
            continue
        hit, total = _num(p.get("notes_hit")), _num(p.get("total_notes"))
        explicit = next((p[k] for k in ("full_combo", "is_full_combo", "fc", "is_fc") if k in p), None)
        fc = bool(explicit) if explicit is not None else (total > 0 and hit == total)
        completed = p.get("completed", p.get("song_completed", True))
        out.append({
            "client_id": f"{checksum}|{ts_raw if ts_raw is not None else int(ts)}|{i}",
            "played_at": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "checksum": checksum or None, "artist": artist, "name": name, "charter": charter,
            "instrument": norm_instrument(p.get("instrument")),
            "difficulty": norm_difficulty(p.get("difficulty")),
            "speed": speed, "score": _num(p.get("score")), "stars": _num(p.get("stars")),
            "accuracy": round(hit / total, 5) if total > 0 else None,
            "notes_hit": hit, "total_notes": total, "max_streak": _num(p.get("max_streak")),
            "fc": fc, "completed": bool(completed) if completed is not None else True,
            "duration_s": round(length / (speed / 100.0), 1) if length else None,
            "machine": machine or socket.gethostname(),
            "raw": {k: v for k, v in p.items() if isinstance(v, (int, float, str, bool)) or v is None},
        })
    return out


# ------------------------------------------------------------------ storage
class PlayLog:
    """Local play history (plays.jsonl) plus the cache of cloud plays (plays_cloud.jsonl)."""

    def __init__(self, local: Path | None = None, cloud: Path | None = None, lb: Path | None = None):
        self.local_path = local or plays_path()
        self.cloud_path = cloud or cloud_plays_path()
        self.lb_path = lb or lb_plays_path()
        self.lock = threading.Lock()
        self.lb_refreshing = False

    @staticmethod
    def _read(path: Path) -> list[dict]:
        out = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(r, dict) and r.get("client_id"):
                        out.append(r)
        except FileNotFoundError:
            pass
        return out

    def local(self) -> list[dict]:
        with self.lock:
            return self._read(self.local_path)

    def cloud(self) -> list[dict]:
        with self.lock:
            return self._read(self.cloud_path)

    def leaderboard(self) -> list[dict]:
        """Plays imported from the linked CH Leaderboards account (never uploaded to the cloud)."""
        with self.lock:
            return self._read(self.lb_path)

    def set_leaderboard(self, records: list[dict]):
        """Replace the imported leaderboard plays (they can always be re-fetched)."""
        with self.lock:
            tmp = self.lb_path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            os.replace(tmp, self.lb_path)

    def clear_leaderboard(self):
        with self.lock:
            try:
                self.lb_path.unlink()
            except FileNotFoundError:
                pass

    def lb_age(self, now: float | None = None) -> float | None:
        """Seconds since the leaderboard plays were last refreshed (None = never)."""
        import time
        try:
            return (now or time.time()) - self.lb_path.stat().st_mtime
        except OSError:
            return None

    def all(self, include_leaderboard: bool = True) -> list[dict]:
        """Local plus cloud plays (one record per client_id), then leaderboard plays that don't
        duplicate any of them (same song, instrument, difficulty and score within a few minutes)."""
        seen, out = set(), []
        for r in self.local() + self.cloud():
            if r["client_id"] not in seen:
                seen.add(r["client_id"])
                out.append(r)
        if include_leaderboard:
            out += dedupe_leaderboard(self.leaderboard(), out)
        return out

    def append(self, records: list[dict]) -> list[dict]:
        """Add records not yet known locally; returns the ones actually added."""
        with self.lock:
            have = {r["client_id"] for r in self._read(self.local_path)}
            new = []
            for r in records:
                if r["client_id"] not in have:
                    have.add(r["client_id"])
                    new.append(r)
            if new:
                with self.local_path.open("a", encoding="utf-8") as f:
                    for r in new:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
            return new

    def add_cloud(self, records: list[dict]) -> int:
        with self.lock:
            have = {r["client_id"] for r in self._read(self.cloud_path)}
            new = [r for r in records if r.get("client_id") and r["client_id"] not in have]
            if new:
                with self.cloud_path.open("a", encoding="utf-8") as f:
                    for r in new:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
            return len(new)

    def clear_cloud(self):
        with self.lock:
            try:
                self.cloud_path.unlink()
            except FileNotFoundError:
                pass


def dedupe_leaderboard(lb: list[dict], own: list[dict]) -> list[dict]:
    """Leaderboard plays that no recorded play already covers."""
    idx: dict[tuple, list[float]] = defaultdict(list)
    for r in own:
        t = parse_timestamp(r.get("played_at"), 0.0)
        idx[(song_ident(r), r.get("instrument"), r.get("difficulty"), int(r.get("score") or 0))].append(t)
    out, seen = [], set()
    for r in lb:
        if r["client_id"] in seen:
            continue
        seen.add(r["client_id"])
        t = parse_timestamp(r.get("played_at"), 0.0)
        ts = idx.get((song_ident(r), r.get("instrument"), r.get("difficulty"), int(r.get("score") or 0)), ())
        if not any(abs(t - x) <= DEDUPE_WINDOW_S for x in ts):
            out.append(r)
    return out


# ------------------------------------------------------------------ capture
def candidate_files(game_dir: Path | None, data_dir: Path | None) -> list[Path]:
    """Where scorestats.json may live: next to the game, or in its data folder."""
    out = []
    for d in (game_dir, data_dir):
        if d:
            p = Path(d) / SCORESTATS
            if p not in out:
                out.append(p)
    return out


class PlayWatcher(threading.Thread):
    """Polls scorestats.json and records every finished song."""

    def __init__(self, playlog: PlayLog, locate: Callable[[], list[Path]],
                 duration: Callable[[str, str, str], float | None] | None = None,
                 on_new: Callable[[list[dict]], None] | None = None, poll: float = 2.0):
        super().__init__(daemon=True, name="playstats")
        self.playlog, self.locate, self.duration, self.on_new, self.poll = playlog, locate, duration, on_new, poll
        self._seen: dict[str, float] = {}
        self._stop_evt = threading.Event()

    def check(self) -> list[dict]:
        added: list[dict] = []
        for p in self.locate():
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if self._seen.get(str(p)) == mt:
                continue
            self._seen[str(p)] = mt
            try:
                data = json.loads(p.read_text("utf-8-sig"))
            except (OSError, ValueError):
                self._seen.pop(str(p), None)      # half-written: try again next poll
                continue
            added += self.playlog.append(parse_scorestats(data, mt, duration=self.duration))
        if added:
            log.info("Recorded %d play%s", len(added), "" if len(added) == 1 else "s")
            if self.on_new:
                try:
                    self.on_new(added)
                except Exception:
                    log.exception("play callback failed")
        return added

    def run(self):
        while not self._stop_evt.is_set():
            try:
                self.check()
            except Exception:
                log.exception("scorestats check failed")
            self._stop_evt.wait(self.poll)

    def stop(self):
        self._stop_evt.set()


# ------------------------------------------------------------------ statistics
def song_ident(r: dict) -> str:
    """Song identity: artist + title. Chart checksums and charters are left out on purpose so a
    recorded play and a CH Leaderboards score of the same song (whose hashes differ) match."""
    return r.get("_ident") or norm_key(f"{r.get('artist', '')} - {r.get('name', '')}")


def split_chart_versions(plays: list[dict]) -> list[dict]:
    """CH Leaderboards tells chart versions of one song apart by songHash (same artist/title/charter,
    different hashes count separately in its FC and songs-played totals). Keep those apart; recorded
    plays have no such hash and stay under the plain artist/title identity."""
    hashes: dict[str, set] = defaultdict(set)
    for p in plays:
        if p.get("lb_song_hash"):
            hashes[song_ident(p)].add(p["lb_song_hash"])
    if not any(len(v) > 1 for v in hashes.values()):
        return plays
    return [{**p, "_ident": f"{song_ident(p)}#{p['lb_song_hash'][:10]}"}
            if p.get("lb_song_hash") and len(hashes[song_ident(p)]) > 1 else p for p in plays]


def _local_dt(r: dict) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(r["played_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone()


def _acc(r: dict) -> float | None:
    a = r.get("accuracy")
    if a is None and r.get("total_notes"):
        a = (r.get("notes_hit") or 0) / r["total_notes"]
    return a


def facets(plays: list[dict]) -> dict:
    return {"instruments": sorted({p.get("instrument") or "unknown" for p in plays}),
            "difficulties": [d for d in DIFFICULTIES if any(p.get("difficulty") == d for p in plays)]}


def compute(plays: list[dict], instrument: str = "", difficulty: str = "", now: datetime | None = None) -> dict:
    """All Profile numbers for the given plays (already merged across machines)."""
    now = (now or datetime.now()).astimezone()
    plays = split_chart_versions(plays)
    fac = facets(plays)
    rows = [p for p in plays if (not instrument or p.get("instrument") == instrument)
            and (not difficulty or p.get("difficulty") == difficulty)]
    dts = [(p, _local_dt(p)) for p in rows]
    dts = [(p, d) for p, d in dts if d]
    total_s = sum(float(p.get("duration_s") or 0) for p, _ in dts)
    accs = [a for a in (_acc(p) for p, _ in dts) if a is not None]
    days = {d.date() for _, d in dts}
    today = now.date()
    # streaks
    longest = run = 0
    prev = None
    for day in sorted(days):
        run = run + 1 if prev and day - prev == timedelta(days=1) else 1
        longest = max(longest, run)
        prev = day
    cur = 0
    day = today if today in days else today - timedelta(days=1)
    while day in days:
        cur += 1
        day -= timedelta(days=1)
    # heatmap: last 26 weeks, weeks start on Monday
    start = today - timedelta(days=today.weekday()) - timedelta(weeks=25)
    per_day = Counter(d.date() for _, d in dts if d.date() >= start)
    heat = [{"d": (start + timedelta(days=i)).isoformat(), "n": per_day.get(start + timedelta(days=i), 0)}
            for i in range((today - start).days + 1)]
    weekday = [0] * 7
    hour = [0] * 24
    for _, d in dts:
        weekday[d.weekday()] += 1
        hour[d.hour] += 1
    # per song
    by_song: dict[str, list[dict]] = defaultdict(list)
    for p, _ in dts:
        by_song[song_ident(p)].append(p)

    def card(ps: list[dict]) -> dict:
        last = max(ps, key=lambda p: p.get("played_at") or "")
        accs_ = [a for a in (_acc(p) for p in ps) if a is not None]
        best = max(ps, key=lambda p: p.get("score") or 0)
        return {"ident": song_ident(last), "checksum": next((p["checksum"] for p in ps if p.get("checksum")), None), "artist": last.get("artist"),
                "name": last.get("name"), "charter": last.get("charter"), "plays": len(ps),
                "avgAccuracy": round(sum(accs_) / len(accs_), 4) if accs_ else None,
                "bestAccuracy": round(max(accs_), 4) if accs_ else None,
                "bestScore": best.get("score") or 0, "bestStars": max(int(p.get("stars") or 0) for p in ps),
                "notes": max(int(p.get("total_notes") or 0) for p in ps), "last": last.get("played_at"),
                "fc": any(p.get("fc") for p in ps)}
    cards = [card(ps) for ps in by_song.values()]
    most = sorted(cards, key=lambda c: (-c["plays"], c["last"] or ""), reverse=False)[:10]
    best = sorted([c for c in cards if c["bestAccuracy"] is not None and (c["plays"] >= 2 or c["notes"] >= 200)],
                  key=lambda c: (-c["bestAccuracy"], -c["plays"]))[:10]
    # Needs practice: every song played at least once, ranked by your personal best (lowest first).
    # Personal best accuracy, not raw score, so long and short songs compare fairly.
    needs = sorted([c for c in cards if c["bestAccuracy"] is not None],
                   key=lambda c: (c["bestAccuracy"], c["bestScore"], -c["plays"]))[:10]
    recent = [{"playedAt": p.get("played_at"), "artist": p.get("artist"), "name": p.get("name"),
               "checksum": p.get("checksum"), "charter": p.get("charter"), "score": p.get("score") or 0,
               "stars": p.get("stars") or 0, "accuracy": _acc(p), "difficulty": p.get("difficulty"),
               "instrument": p.get("instrument"), "fc": bool(p.get("fc")), "speed": p.get("speed") or 100}
              for p, _ in sorted(dts, key=lambda t: t[0].get("played_at") or "", reverse=True)[:15]]
    # personal bests: best score per song+instrument+difficulty
    pb: dict[tuple, dict] = {}
    for p, _ in dts:
        k = (song_ident(p), p.get("instrument"), p.get("difficulty"))
        if k not in pb or (p.get("score") or 0) > (pb[k].get("score") or 0):
            pb[k] = p
    pbs = [{"playedAt": p.get("played_at"), "artist": p.get("artist"), "name": p.get("name"),
            "checksum": p.get("checksum"), "charter": p.get("charter"), "score": p.get("score") or 0,
            "stars": p.get("stars") or 0, "accuracy": _acc(p), "difficulty": p.get("difficulty"),
            "instrument": p.get("instrument"), "fc": bool(p.get("fc"))}
           for p in sorted(pb.values(), key=lambda p: p.get("score") or 0, reverse=True)[:15]]
    # FCs and 5-star charts count distinct charts (song + instrument + difficulty) ever achieved,
    # the way CH Leaderboards' totalFcs does, not plays.
    charts: dict[tuple, list[dict]] = defaultdict(list)
    for p, _ in dts:
        charts[(song_ident(p), p.get("instrument"), p.get("difficulty"))].append(p)
    n_lb = sum(1 for p, _ in dts if p.get("source") == LB_SOURCE)
    return {
        "totals": {"plays": len(dts), "hours": round(total_s / 3600, 1), "songs": len(by_song),
                   "avgAccuracy": round(sum(accs) / len(accs), 4) if accs else None,
                   "fcs": sum(1 for ps in charts.values() if any(p.get("fc") for p in ps)),
                   "fiveStars": sum(1 for ps in charts.values() if any((p.get("stars") or 0) >= 5 for p in ps)),
                   "totalScore": int(sum(p.get("score") or 0 for p, _ in dts)),
                   "streak": cur, "longestStreak": longest},
        "heatmap": {"start": start.isoformat(), "days": heat},
        "weekday": weekday, "hour": hour,
        "mostPlayed": most, "best": best, "needsPractice": needs, "recent": recent, "personalBests": pbs,
        "playSources": {"app": len(dts) - n_lb, "leaderboard": n_lb},
        "facets": fac, "instrument": instrument, "difficulty": difficulty,
    }
