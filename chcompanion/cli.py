"""Command line entry point."""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
import webbrowser

from . import __version__, youtube
from .app import Companion, serve
from .installer import InstallError, install_video
from .library import Library
from .paths import config_dir, find_data_dir, read_settings, song_dirs
from .store import Store

BANNER = r"""
  Clone Hero Companion {v} - music videos + sing-along lyrics for Clone Hero
"""


def _setup_logging(verbose: bool):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname).1s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("urllib3",):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _common(p: argparse.ArgumentParser):
    p.add_argument("--data-dir", help="Clone Hero data folder (the one containing settings.ini, Logs/ and Songs/)")
    p.add_argument("--songs-dir", action="append", default=[], help="extra song folder to index (repeatable)")
    p.add_argument("-v", "--verbose", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="CloneHeroCompanion",
                                 description="Shows the current Clone Hero song's music video and synced lyrics.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")

    run = sub.add_parser("run", help="watch the game and serve the companion page (default)")
    _common(run)
    run.add_argument("--host", default="127.0.0.1",
                     help="address to listen on (use 0.0.0.0 to open the page from a TV/phone on your network)")
    run.add_argument("--port", type=int, default=47823)
    run.add_argument("--no-browser", action="store_true", help="don't open the page automatically")
    run.add_argument("--no-youtube", action="store_true", help="only use video files found in song folders")
    run.add_argument("--no-lrclib", action="store_true", help="only use lyrics contained in the charts")
    run.add_argument("--auto-install", action="store_true",
                     help="also save each played song's video into its folder so Clone Hero shows it in-game")
    run.add_argument("--max-height", type=int, default=1080, help="max video height to stream (default 1080)")

    inst = sub.add_parser("install-videos", help="download music videos into song folders for in-game playback")
    _common(inst)
    inst.add_argument("filter", nargs="*", help="only songs whose 'Artist - Name' contains these words")
    inst.add_argument("--force", action="store_true", help="replace existing video files")
    inst.add_argument("--limit", type=int, default=0, help="stop after N downloads")
    inst.add_argument("--max-height", type=int, default=720)
    inst.add_argument("--dry-run", action="store_true", help="only show which video would be used")

    doc = sub.add_parser("doctor", help="check the setup")
    _common(doc)

    ly = sub.add_parser("lyrics", help="print the synced lyrics found for a song (debugging)")
    _common(ly)
    ly.add_argument("song", help="'Artist - Name' as shown in the game")
    return ap


def _load(args) -> tuple:
    data_dir = find_data_dir(args.data_dir)
    if data_dir is None:
        print("Could not find your Clone Hero data folder.\n"
              "Start Clone Hero once, or pass it with --data-dir \"/path/to/Clone Hero\".")
        sys.exit(2)
    roots = song_dirs(data_dir, args.songs_dir)
    lib = Library(roots)
    return data_dir, lib


def cmd_doctor(args) -> int:
    data_dir, lib = _load(args)
    print(f"Data folder : {data_dir}")
    print(f"Song folders: {', '.join(str(r) for r in lib.roots) or '(none)'}")
    n = lib.scan()
    sng = sum(1 for s in lib.songs if s.is_sng)
    vids = sum(1 for s in lib.songs if not s.is_sng and s.local_video())
    print(f"Songs       : {n} ({sng} .sng packages, {vids} with a video file)")
    logs = sorted((data_dir / "Logs").glob("Session-*.log"))
    print(f"Game logs   : {logs[-1].name if logs else 'none found - start the game once'}")
    cp = read_settings(data_dir)
    sv = cp.get("custom", "song_videos", fallback="?")
    print(f"In-game song videos setting: {'on' if sv == '1' else 'OFF (Settings > Custom Content > Song Videos)' if sv == '0' else 'unknown'}")
    print(f"yt-dlp      : {'available' if youtube.available() else 'missing'}")
    import shutil
    print(f"ffmpeg      : {shutil.which('ffmpeg') or 'not found (only needed for in-game videos on Linux)'}")
    store = Store(config_dir() / "config.json")
    print(f"Config      : {config_dir()}  (song start delay {store.get('start_lead')}s, "
          f"{store.get('lead_samples')} calibration samples)")
    return 0


def cmd_lyrics(args) -> int:
    from .lyrics import fetch_lrclib
    _, lib = _load(args)
    lib.scan()
    song = lib.find(args.song)
    if not song:
        print("Song not found")
        return 1
    lines, src = song.chart_lyrics(), "chart"
    if not lines:
        lines, src = fetch_lrclib(song.artist, song.name, song.length_s), "lrclib"
    print(f"{song.display}  [{src}, {len(lines)} lines]")
    for l in lines:
        print(f"{l['start']:8.2f}  {l['text']}")
    return 0


def cmd_install(args) -> int:
    data_dir, lib = _load(args)
    lib.scan()
    store = Store(config_dir() / "config.json")
    yt = youtube.YouTube(max_height=args.max_height)
    words = [w.lower() for w in args.filter]
    todo = [s for s in sorted(lib.songs, key=lambda s: s.display.lower())
            if all(w in s.display.lower() for w in words)]
    done = skipped = failed = 0
    for s in todo:
        if s.is_sng:
            skipped += 1
            continue
        if s.local_video() and not args.force:
            skipped += 1
            continue
        if args.limit and done >= args.limit:
            break
        try:
            if args.dry_run:
                c = yt.search(s)
                print(f"{s.display}: {c[0]['title'] if c else 'nothing found'} "
                      f"({'https://youtu.be/' + c[0]['id'] if c else ''})")
                continue
            print(f"{s.display} ...", end=" ", flush=True)
            p = install_video(s, yt, store, force=args.force,
                              progress=lambda m: print(f"\r{s.display} ... {m}   ", end="", flush=True)
                              if isinstance(m, str) else None)
            print(f"\r{s.display} ... saved {p.name}            ")
            done += 1
        except (InstallError, Exception) as e:
            print(f"\r{s.display} ... failed: {e}")
            failed += 1
    print(f"\nDone: {done} installed, {skipped} skipped (.sng or already have a video), {failed} failed.")
    cp = read_settings(data_dir)
    if cp.get("custom", "song_videos", fallback="1") == "0":
        print("Note: turn on Settings > Custom Content > Song Videos in Clone Hero to see them.")
    return 0 if not failed else 1


def _running_instance(port: int) -> str | None:
    import json
    import urllib.request
    url = f"http://127.0.0.1:{port}/"
    try:
        with urllib.request.urlopen(url + "api/state", timeout=1.5) as r:
            if "clock" in json.loads(r.read().decode("utf-8")):
                return url
    except Exception:
        pass
    return None


def cmd_run(args) -> int:
    data_dir, lib = _load(args)
    print(BANNER.format(v=__version__))
    # Already running? Just show the existing page.
    existing = _running_instance(args.port)
    if existing:
        logging.info("Clone Hero Companion is already running at %s", existing)
        if not args.no_browser:
            webbrowser.open(existing)
        return 0
    logging.info("Clone Hero data folder: %s", data_dir)
    lib.scan()
    store = Store(config_dir() / "config.json")
    comp = Companion(data_dir, lib, store, use_youtube=not args.no_youtube,
                     use_lrclib=not args.no_lrclib, auto_install=args.auto_install,
                     max_height=args.max_height)
    if not args.no_youtube and not youtube.available():
        logging.warning("yt-dlp not available - YouTube videos disabled")
    srv = serve(comp, args.host, args.port)
    port = srv.server_address[1]
    comp.start()
    url = f"http://127.0.0.1:{port}/"
    logging.info("Companion page: %s  (open it on any screen, or add it to OBS as a Browser Source)", url)
    if args.host not in ("127.0.0.1", "localhost"):
        logging.info("Listening on %s:%d for other devices on your network", args.host, port)
    logging.info("Waiting for Clone Hero... (Ctrl+C to quit)")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    # periodic rescan so newly added songs are found
    def rescan():
        while True:
            time.sleep(600)
            try:
                lib.scan()
            except Exception:
                pass
    threading.Thread(target=rescan, daemon=True).start()
    stop = comp.quit_requested
    try:
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    except Exception:
        pass
    try:
        while not stop.is_set():
            stop.wait(1)
    except KeyboardInterrupt:
        pass
    logging.info("Bye!")
    return 0


def main(argv=None) -> int:
    ap = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version"):
        argv = ["run"] + argv
    args = ap.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    return {"run": cmd_run, "install-videos": cmd_install, "doctor": cmd_doctor,
            "lyrics": cmd_lyrics}[args.cmd](args)
