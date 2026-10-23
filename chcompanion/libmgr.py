"""Library manager: a rich, cached index of every song plus the actions the app
offers on a selection (favourite, rate, disable, delete, rename, export pack,
duplicates, setlists, audio preview)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import setlists
from .library import Library, Song, norm_key, strip_tags
from .paths import config_dir
from .store import Store

log = logging.getLogger(__name__)
INSTR = {"guitar": "G", "bass": "B", "rhythm": "R", "drums": "D", "keys": "K", "vocals": "V",
         "guitarghl": "6", "bassghl": "6B"}


def song_id(song: Song) -> str:
    return hashlib.sha1(str(song.path).encode("utf-8", "replace")).hexdigest()[:14]


def _sig(song: Song) -> str:
    try:
        if song.is_sng:
            st = song.path.stat()
            return f"{st.st_size}:{int(st.st_mtime)}"
        st = song.path.stat()
        parts = [int(st.st_mtime)]
        for n in ("song.ini", "notes.mid", "notes.chart"):
            p = song.path / n
            if p.exists():
                s2 = p.stat()
                parts += [s2.st_size, int(s2.st_mtime)]
        return ":".join(map(str, parts))
    except OSError:
        return ""


class LibraryManager:
    def __init__(self, data_dir: Path, library: Library, store: Store):
        self.data_dir = data_dir
        self.lib = library
        self.store = store
        self.disabled_dir = data_dir / "Disabled Songs"
        self.disabled = Library([self.disabled_dir]) if self.disabled_dir.exists() else Library([])
        self.cache_path = config_dir() / "library_cache.json"
        try:
            self.cache: dict = json.loads(self.cache_path.read_text("utf-8"))
        except Exception:
            self.cache = {}
        self.lock = threading.RLock()
        self.by_id: dict[str, Song] = {}
        self.enriching = False
        self.enrich_progress = (0, 0)
        self.version = 0
        self._save_timer = None
        self._save_lock = threading.Lock()
        self._fav_lock = threading.Lock()
        self._favs_lock = threading.RLock()
        self.on_fav = None          # callback(songs, value) after favourites change (cloud sync)

    # ------------------------------------------------------------ index
    def rescan(self):
        self.lib.scan()
        self.disabled = Library([self.disabled_dir]) if self.disabled_dir.exists() else Library([])
        if self.disabled_dir.exists():
            self.disabled.scan()
        self._reindex()
        self.start_enrich()

    def _reindex(self):
        with self.lock:
            self.by_id = {song_id(s): s for s in self.lib.songs + self.disabled.songs}
            self.version += 1

    def get(self, sid: str) -> Song | None:
        with self.lock:
            if not self.by_id:
                self._reindex()
            return self.by_id.get(sid)

    def songs(self, ids: list[str]) -> list[Song]:
        return [s for s in (self.get(i) for i in ids) if s is not None]

    def root_of(self, song: Song) -> Path | None:
        for r in self.lib.roots + [self.disabled_dir]:
            try:
                song.path.relative_to(r)
                return r
            except ValueError:
                continue
        return None

    def entry(self, s: Song, favsets=None, copies: int = 1) -> dict:
        m = s.meta
        per = self.store.song(s.key)
        c = self.cache.get(str(s.path), {})
        diffs = {}
        for inst in INSTR:
            try:
                v = int(float(m.get(f"diff_{inst}", "-1") or -1))
            except ValueError:
                v = -1
            if v >= 0:
                diffs[inst] = v
        root = self.root_of(s)
        rel = str(s.path.relative_to(root)) if root else s.path.name
        group = rel.split(os.sep)[0] if os.sep in rel else ""
        try:
            added = int(s.path.stat().st_mtime)
        except OSError:
            added = 0
        return {
            "id": song_id(s), "key": s.key, "name": s.name, "artist": s.artist,
            "album": strip_tags(m.get("album", "")), "year": strip_tags(m.get("year", "")).lstrip(", "),
            "genre": strip_tags(m.get("genre", "")),
            "charter": strip_tags(m.get("charter", "") or m.get("frets", "")),
            "length": s.length_s, "sng": s.is_sng, "path": str(s.path), "rel": rel, "group": group,
            "disabled": root == self.disabled_dir, "diffs": diffs,
            "video": c.get("vres"), "hasVideo": c.get("hasVideo"),
            "lyrics": c.get("lyrics"), "art": c.get("art"),
            "fav": self.is_fav(s, favsets), "copies": copies, "rating": int(per.get("rating", 0) or 0),
            "offset": per.get("video_offset"), "added": added,
            "videoFailed": bool(per.get("video_failed")),
        }

    def entries(self) -> list[dict]:
        with self.lock:
            if not self.by_id:
                self._reindex()
            songs = list(self.by_id.values())
        self._ensure_fav_migrated()
        sets = self._fav_sets()
        counts: dict[str, int] = {}
        for s in songs:
            counts[s.key] = counts.get(s.key, 0) + 1
        return [self.entry(s, sets, counts[s.key]) for s in songs]

    # ------------------------------------------------------------ enrichment
    def start_enrich(self, force: bool = False):
        if self.enriching:
            return
        threading.Thread(target=self._enrich, args=(force,), daemon=True, name="enrich").start()

    def _enrich_one(self, s: Song, force: bool):
        from . import ingame
        sig = _sig(s)
        key = str(s.path)
        c = self.cache.get(key)
        if c and c.get("sig") == sig and not force:
            return
        files = {f.lower() for f in s.list_files()}
        art = any(f in files for f in ("album.jpg", "album.png", "album.jpeg"))
        has_video = any(f.startswith("video.") for f in files)
        per = self.store.song(s.key)
        lyr = "none"
        try:
            if s.chart_lyrics():
                lyr = "added" if per.get("game_lyrics") else "chart"
        except Exception:
            lyr = "none"
        vres = 0
        if has_video:
            try:
                vres = ingame.current_video_res(s, self.store)
            except Exception:
                vres = 0
        with self.lock:
            self.cache[key] = {"sig": sig, "art": art, "hasVideo": has_video, "vres": vres if has_video else None,
                               "lyrics": lyr}

    def _enrich(self, force: bool):
        self.enriching = True
        try:
            with self.lock:
                songs = list(self.by_id.values())
            todo = [s for s in songs if force or self.cache.get(str(s.path), {}).get("sig") != _sig(s)]
            self.enrich_progress = (0, len(todo))
            done = 0
            with ThreadPoolExecutor(max_workers=min(6, (os.cpu_count() or 2) + 1)) as ex:
                for _ in ex.map(lambda s: self._safe_enrich(s, force), todo):
                    done += 1
                    self.enrich_progress = (done, len(todo))
                    if done % 50 == 0:
                        self.version += 1
            self.save_cache()
            self.version += 1
        finally:
            self.enriching = False

    def _safe_enrich(self, s: Song, force: bool):
        try:
            self._enrich_one(s, force)
        except Exception as e:
            log.debug("enrich %s: %s", s.display, e)

    def refresh_song(self, s: Song):
        """Re-read one song after it was changed."""
        from .library import parse_ini, _read_text
        from .sng import open_sng
        try:
            if s.is_sng:
                s.meta = open_sng(str(s.path)).metadata
            elif (s.path / "song.ini").exists():
                s.meta = parse_ini(_read_text(s.path / "song.ini"))
        except Exception:
            pass
        self._safe_enrich(s, True)
        self.version += 1
        self.save_cache()

    def save_cache(self):
        """Write the library cache. Many workers finish at once, so writes are serialised,
        and a failed write is only logged: it's a cache, never a reason to fail a job."""
        with self.lock:
            data = json.dumps(self.cache)
        with self._save_lock:
            tmp = self.cache_path.with_name(f"{self.cache_path.name}.{os.getpid()}.tmp")
            try:
                tmp.write_text(data, "utf-8")
                os.replace(tmp, self.cache_path)
            except OSError as e:
                log.warning("Could not save the library cache: %s", e)
                try:
                    tmp.unlink()
                except OSError:
                    pass

    # ------------------------------------------------------------ actions
    FAV_SETLIST = "Favourites"

    def set_flag(self, ids: list[str], **values):
        """Song-level flags (rating...). Favourites are per chart: see set_fav."""
        for s in self.songs(ids):
            self.store.update_song(s.key, **values)
        self.version += 1

    # ------------------------------------------------------------ favourites (per chart)
    # A favourite belongs to one chart, not to a song: the same song often exists as several charts.
    # A chart is identified by its checksum (the Clone Hero md5, as used by setlists) when known, else by
    # "artist - name | charter | path relative to its songs folder". Both are recorded so a favourite
    # survives a move (checksum) or a missing hash (path).
    @staticmethod
    def charter_of(s: Song) -> str:
        return strip_tags(s.meta.get("charter", "") or s.meta.get("frets", ""))

    def legacy_key(self, s: Song) -> str:
        """The old cloud/song-level identity: norm_key(artist - name)|norm_key(charter)."""
        return norm_key(s.display) + "|" + norm_key(self.charter_of(s))

    def chart_pid(self, s: Song) -> str:
        root = self.root_of(s)
        rel = s.path.relative_to(root).as_posix() if root else s.path.name
        return self.legacy_key(s) + "|" + rel

    def chart_md5(self, s: Song, compute: bool = False) -> str | None:
        c = self.cache.get(str(s.path), {})
        h = c.get("md5")
        if not h and compute:
            try:
                h = setlists.song_hash(s)
            except Exception:
                h = None
            if h:
                c = self.cache.setdefault(str(s.path), {})
                c["md5"], c["md5sig"] = h, _sig(s)
        return h.upper() if h else None

    def chart_key(self, s: Song, compute: bool = False) -> str:
        return self.chart_md5(s, compute) or self.chart_pid(s)

    def _fav_sets(self) -> tuple[set, set]:
        favs = self.store.get("favs", {}) or {}
        return ({v["md5"] for v in favs.values() if v.get("md5")}, {v["pid"] for v in favs.values() if v.get("pid")})

    def is_fav(self, s: Song, sets=None) -> bool:
        md5s, pids = sets or self._fav_sets()
        h = self.chart_md5(s)
        return bool((h and h in md5s) or self.chart_pid(s) in pids)

    def set_fav(self, songs: list[Song], value: bool, notify: bool = True, _migrate: bool = True) -> list[Song]:
        """Star or unstar exactly these charts (never their other copies). Returns the charts changed."""
        if _migrate:
            self._ensure_fav_migrated()
        changed = []
        with self._favs_lock:
            favs = dict(self.store.get("favs", {}) or {})
            for s in songs:
                md5, pid = self.chart_md5(s, compute=value), self.chart_pid(s)
                hit = [k for k, v in favs.items() if (md5 and v.get("md5") == md5) or v.get("pid") == pid]
                if value and not hit:
                    favs[md5 or pid] = {"md5": md5, "pid": pid, "t": int(time.time())}
                    changed.append(s)
                elif not value and hit:
                    for k in hit:
                        favs.pop(k, None)
                    changed.append(s)
            if changed:
                self.store.set("favs", favs)
        if changed:
            self.save_cache()
            self.version += 1
            if notify and self.on_fav:
                try:
                    self.on_fav(changed, value)
                except Exception as e:
                    log.debug("cloud favourite hook failed: %s", e)
            if self.fav_setlist_enabled():
                threading.Thread(target=self.sync_fav_setlist, daemon=True, name="fav-setlist").start()
        return changed

    def _ensure_fav_migrated(self):
        """One-time, idempotent: old song-level favourites (songs[key].fav) become favourites of each
        chart that matched the song. A legacy favourite with no chart in the library yet is kept for later."""
        legacy = [k for k, v in (self.store.get("songs", {}) or {}).items() if v.get("fav")]
        if not legacy:
            return
        with self.lock:
            if not self.by_id:
                self._reindex()
            songs = list(self.by_id.values())
        by_key: dict[str, list[Song]] = {}
        for s in songs:
            by_key.setdefault(s.key, []).append(s)
        done = []
        for k in legacy:
            if k in by_key:
                self.set_fav(by_key[k], True, notify=False, _migrate=False)
                done.append(k)
        for k in done:
            self.store.update_song(k, fav=None)

    def fav_ids(self) -> list[str]:
        self._ensure_fav_migrated()
        with self.lock:
            if not self.by_id:
                self._reindex()
            items = list(self.by_id.items())
        sets = self._fav_sets()
        return [sid for sid, s in items if self.is_fav(s, sets)]

    def fav_setlist_enabled(self) -> bool:
        s = self.store.get("settings", {}) or {}
        return s.get("fav_setlist", True) is not False

    def sync_fav_setlist(self) -> int:
        """Keep a 'Favourites' setlist in Clone Hero matching the favourites here (newest last)."""
        with self._fav_lock:
            return self._sync_fav_setlist()

    def _sync_fav_setlist(self) -> int:
        try:
            with self.lock:
                self._hash_index()
                hashes = []
                for sid in self.fav_ids():
                    s = self.by_id.get(sid)
                    h = s and self.cache.get(str(s.path), {}).get("md5")
                    if h and h not in hashes:
                        hashes.append(h)
            p = setlists.setlists_dir(self.data_dir) / (self.FAV_SETLIST + ".setlist")
            if not hashes and not p.exists():
                return 0
            setlists.save(self.data_dir, self.FAV_SETLIST, [(h, 100) for h in hashes])
            return len(hashes)
        except Exception as e:
            log.warning("Could not update the Favourites setlist: %s", e)
            return 0

    def disable(self, ids: list[str], enable: bool = False) -> int:
        n = 0
        for s in self.songs(ids):
            root = self.root_of(s)
            if root is None:
                continue
            is_disabled = root == self.disabled_dir
            if enable == is_disabled:
                rel = s.path.relative_to(root)
                target_root = self.lib.roots[0] if enable else self.disabled_dir
                dst = target_root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists():
                    continue
                shutil.move(str(s.path), dst)
                n += 1
        self.rescan()
        return n

    def delete(self, ids: list[str]) -> int:
        n = 0
        for s in self.songs(ids):
            try:
                try:
                    from send2trash import send2trash
                    send2trash(str(s.path))
                except ImportError:
                    trash = self.data_dir / "Deleted Songs"
                    trash.mkdir(exist_ok=True)
                    shutil.move(str(s.path), trash / s.path.name)
                n += 1
            except Exception as e:
                log.warning("delete %s: %s", s.path, e)
        self.rescan()
        return n

    def rename(self, ids: list[str], template: str = "{artist} - {name}") -> int:
        from .sources.install import safe_name, unique
        n = 0
        for s in self.songs(ids):
            charter = strip_tags(s.meta.get("charter", ""))
            new = safe_name(template.format(artist=s.artist, name=s.name, charter=charter,
                                            album=strip_tags(s.meta.get("album", "")),
                                            year=strip_tags(s.meta.get("year", ""))).replace("()", "").strip())
            dst = s.path.with_name(new + (".sng" if s.is_sng else ""))
            if dst == s.path:
                continue
            dst = unique(dst)
            s.path.rename(dst)
            n += 1
        self.rescan()
        return n

    def export_pack(self, ids: list[str], name: str) -> Path:
        from .sources.install import safe_name
        out_dir = self.data_dir / "Chart Packs"
        out_dir.mkdir(exist_ok=True)
        out = out_dir / (safe_name(name or "Chart Pack") + ".zip")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
            for s in self.songs(ids):
                if s.is_sng:
                    z.write(s.path, s.path.name)
                    continue
                for f in s.path.rglob("*"):
                    if f.is_file() and not f.name.endswith((".chc-backup", ".chc-tmp")):
                        z.write(f, f"{s.path.name}/{f.relative_to(s.path)}")
        return out

    def duplicates(self) -> list[list[dict]]:
        groups: dict[str, list[Song]] = {}
        with self.lock:
            for s in self.lib.songs:
                groups.setdefault(norm_key(f"{s.artist} {s.name}".replace("(wavegroup)", "")), []).append(s)
        out = []
        for g in groups.values():
            if len(g) > 1:
                out.append([self.entry(s) for s in g])
        out.sort(key=lambda g: (g[0]["artist"].lower(), g[0]["name"].lower()))
        return out

    # ------------------------------------------------------------ setlists
    def setlists(self) -> list[dict]:
        index = {}
        out = []
        for sl in setlists.list_all(self.data_dir):
            if not index:
                index = self._hash_index()
            out.append({"name": sl["name"], "songs": [
                {"hash": h, "speed": sp, "id": index.get(h)} for h, sp in sl["entries"]]})
        return out

    def hash_to_id(self) -> dict[str, str]:
        """Chart MD5 (upper case, as in Clone Hero's scorestats) -> song id."""
        with self.lock:
            return {k.upper(): v for k, v in self._hash_index().items()}

    def key_to_id(self) -> dict[str, str]:
        with self.lock:
            if not self.by_id:
                self._reindex()
            out: dict[str, str] = {}
            for sid, s in self.by_id.items():
                out.setdefault(s.key, sid)
            return out

    def _hash_index(self) -> dict[str, str]:
        idx = {}
        changed = False
        for s in self.lib.songs:
            key, sig = str(s.path), _sig(s)
            c = self.cache.setdefault(key, {})
            if c.get("md5sig") != sig:
                c["md5"] = setlists.song_hash(s)
                c["md5sig"] = sig
                changed = True
            if c.get("md5"):
                idx[c["md5"]] = song_id(s)
        if changed:
            self.save_cache()
        return idx

    def setlist_add(self, name: str, ids: list[str]) -> int:
        self._hash_index()
        hashes = [self.cache.get(str(s.path), {}).get("md5") for s in self.songs(ids)]
        return setlists.add(self.data_dir, name, [h for h in hashes if h])

    def setlist_remove(self, name: str, ids: list[str]):
        self._hash_index()
        setlists.remove(self.data_dir, name, [self.cache.get(str(s.path), {}).get("md5") for s in self.songs(ids)])

    # ------------------------------------------------------------ media
    def audio_stream_cmd(self, s: Song, start: float = 0.0, seconds: float = 0.0) -> tuple[list[str], Path | None]:
        """ffmpeg command that mixes the song's stems to an MP3 stream on stdout."""
        from .align import chart_audio_files, ffmpeg_path
        ff = ffmpeg_path()
        names = [n for n in chart_audio_files(s.list_files())]
        names += [n for n in s.list_files() if n.lower().rsplit(".", 1)[0] == "crowd"][:0]
        if not ff or not names:
            raise RuntimeError("no audio")
        tmp = None
        if s.is_sng:
            import tempfile
            tmp = Path(tempfile.mkdtemp(prefix="chc-play-", dir=config_dir()))
            paths = []
            for n in names:
                p = tmp / n
                p.write_bytes(s.read_file(n) or b"")
                paths.append(p)
        else:
            paths = [s.path / n for n in names]
        cmd = [ff, "-hide_banner", "-loglevel", "error"]
        for p in paths:
            cmd += ["-ss", f"{max(0.0, start):.2f}"] + (["-t", str(seconds)] if seconds else []) + ["-i", str(p)]
        if len(paths) > 1:
            cmd += ["-filter_complex", f"amix=inputs={len(paths)}:normalize=0:duration=longest,alimiter=limit=0.95"]
        cmd += ["-ac", "2", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3", "-"]
        return cmd, tmp


def reveal(path: Path):
    """Show a file/folder in Finder / Explorer / the file manager."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path if path.is_dir() else path.parent)])
    except Exception as e:
        log.warning("reveal: %s", e)


def find_game(data_dir: Path, configured: str | None = None) -> Path | None:
    from .paths import find_games
    found = find_games(configured, limit=1)
    if found:
        return found[0]
    cands = []
    home = Path.home()
    if sys.platform == "darwin":
        cands += [data_dir / "Clone Hero.app", Path("/Applications/Clone Hero.app"), home / "Applications/Clone Hero.app"]
    elif sys.platform == "win32":
        pf = [os.environ.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
        for base in filter(None, pf):
            cands += [Path(base) / "Clone Hero" / "Clone Hero.exe", Path(base) / "Programs" / "Clone Hero" / "Clone Hero.exe"]
        cands += [data_dir / "Clone Hero.exe"]
    else:
        cands += [home / "Clone Hero" / "clonehero", home / "clonehero" / "clonehero", data_dir / "clonehero",
                  home / "Applications" / "clonehero"]
    for c in cands:
        if c.exists():
            return c
    return None


def launch_game(path: Path):
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen([str(path)], cwd=str(path.parent), start_new_session=True)
    time.sleep(0.1)
