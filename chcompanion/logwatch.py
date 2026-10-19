"""Follows Clone Hero's session log (<data>/Logs/Session-*.log) and turns it into
a song clock.

Observed log behaviour (Clone Hero v1.1):
  [Log] Loading Song: <Artist> - <Name>        song chosen, gameplay loading
  [Log] Seeking to song time:<X>               while playing -> paused at X
  [Log] Seeking to song time:<X - 1.5>         while paused  -> resumed (1.5 s rewind)
  [Log] Pausing session: <id> at time: <X>     (online only) paused at X
  [Log] Replay input count: <n>                song finished (results screen)
  [Log] Completing session / Cancelling session (online only) finished / quit
Menu song previews also log "Seeking to song time", so seeks are interpreted
relative to the current state.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

RE_LOAD = re.compile(r"Loading Song:\s*(.+?)\s*$")
RE_SEEK = re.compile(r"Seeking to song time:\s*(-?[0-9.]+)")
RE_PAUSE_SESSION = re.compile(r"Pausing session: .* at time:\s*(-?[0-9.]+)")
RE_SPEED = re.compile(r"[?&]speed=(\d+)")
RESUME_REWIND = 1.5


@dataclass
class ClockState:
    status: str = "idle"          # idle | playing | paused | ended
    song: str | None = None       # "Artist - Name" as logged
    anchor_time: float = 0.0      # song time at anchor
    anchor_wall: float = 0.0      # time.time() at anchor
    rate: float = 0.0             # 0 when paused, else song speed
    speed: float = 1.0
    loaded_wall: float = 0.0
    calibrated: bool = False      # clock has been confirmed by a pause
    seq: int = 0                  # increments on every change
    pause_x: float | None = field(default=None, repr=False)

    def now(self) -> float:
        return self.anchor_time + self.rate * (time.time() - self.anchor_wall)

    def public(self) -> dict:
        return {"status": self.status, "song": self.song, "anchorTime": self.anchor_time,
                "anchorWall": self.anchor_wall, "rate": self.rate, "speed": self.speed,
                "calibrated": self.calibrated, "seq": self.seq}


class LogWatcher(threading.Thread):
    def __init__(self, data_dir: Path, on_change: Callable[[ClockState, str], None],
                 start_lead: Callable[[], float], learn_lead: Callable[[float], None],
                 poll: float = 0.03):
        super().__init__(daemon=True, name="logwatch")
        self.logs_dir = data_dir / "Logs"
        self.on_change = on_change
        self.start_lead = start_lead
        self.learn_lead = learn_lead
        self.poll = poll
        self.state = ClockState()
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self.current_file: str | None = None
        self.debug_events: list[str] = []

    # ------------------------------------------------------------ helpers
    def _latest_log(self) -> str | None:
        files = glob.glob(str(self.logs_dir / "Session-*.log"))
        if not files:
            files = glob.glob(str(self.logs_dir / "*.log"))
        # Names are Session-YYYY-MM-DD_HH-MM-SS.log -> lexical order == time order.
        return max(files, key=lambda p: (os.path.basename(p), os.path.getsize(p))) if files else None

    def _emit(self, why: str):
        self.state.seq += 1
        line = f"{time.time():.3f} {why} -> {self.state.status} t={self.state.now():.2f}"
        self.debug_events.append(line)
        del self.debug_events[:-200]
        log.debug(line)
        try:
            self.on_change(self.state, why)
        except Exception:
            log.exception("on_change failed")

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------ parsing
    def handle_line(self, line: str, now: float | None = None):
        now = time.time() if now is None else now
        s = self.state
        with self.lock:
            m = RE_LOAD.search(line)
            if m:
                s.song = m.group(1)
                s.status = "playing"
                s.loaded_wall = now
                s.speed = 1.0
                s.anchor_time = -self.start_lead()
                s.anchor_wall = now
                s.rate = s.speed
                s.calibrated = False
                s.pause_x = None
                self._emit("load")
                return
            if s.status == "idle":
                return
            m = RE_SPEED.search(line)
            if m and "scores" in line and s.status in ("playing", "paused"):
                sp = int(m.group(1)) / 100.0
                if 0.05 < sp < 5 and sp != s.speed:
                    t = s.now()
                    s.speed = sp
                    if s.rate:
                        s.rate = sp
                    s.anchor_time, s.anchor_wall = t, now
                    self._emit("speed")
                return
            m = RE_PAUSE_SESSION.search(line)
            if m and s.status == "playing":
                self._pause(float(m.group(1)), now)
                return
            m = RE_SEEK.search(line)
            if m:
                x = float(m.group(1))
                if s.status == "playing":
                    self._pause(x, now)
                elif s.status == "paused":
                    px = s.pause_x if s.pause_x is not None else x
                    if abs(x - px) < 0.05:  # duplicate pause seek (e.g. after session pause)
                        return
                    if px - 4.0 <= x <= px + 0.05:  # resume (rewind)
                        s.status = "playing"
                        s.rate = s.speed
                        s.anchor_time, s.anchor_wall = x, now
                        s.pause_x = None
                        self._emit("resume")
                    else:  # a menu preview -> the player quit to the menu
                        self._idle("quit")
                return
            if "Replay input count" in line or "Completing session" in line:
                if s.status in ("playing", "paused"):
                    t = s.now()
                    s.status, s.rate = "ended", 0.0
                    s.anchor_time, s.anchor_wall = t, now
                    self._emit("end")
                return
            if "Cancelling session" in line and s.status in ("playing", "paused"):
                self._idle("cancel")
                return
            if "OnCountdownEnd" in line and s.status == "paused":
                # countdown after a resume without an observed rewind seek
                s.status, s.rate = "playing", s.speed
                s.anchor_wall = now
                self._emit("resume-countdown")

    def _pause(self, x: float, now: float):
        s = self.state
        if not s.calibrated and s.speed > 0:
            # wall-clock moment the song time was 0, compared with the load line
            actual_zero = now - x / s.speed
            lead = actual_zero - s.loaded_wall
            if 0.0 <= lead <= 15.0 and s.pause_x is None:
                self.learn_lead(lead)
            s.calibrated = True
        s.status, s.rate = "paused", 0.0
        s.anchor_time, s.anchor_wall = x, now
        s.pause_x = x
        self._emit("pause")

    def _idle(self, why: str):
        s = self.state
        s.status, s.rate, s.song = "idle", 0.0, None
        s.pause_x = None
        self._emit(why)

    # ------------------------------------------------------------ main loop
    def run(self):
        f = None
        buf = ""
        last_check = 0.0
        while not self._stop.is_set():
            now = time.time()
            if now - last_check > 1.5 or f is None:
                last_check = now
                latest = self._latest_log()
                if latest and latest != self.current_file:
                    if f:
                        f.close()
                    first = self.current_file is None
                    self.current_file = latest
                    try:
                        f = open(latest, "r", encoding="utf-8", errors="replace")
                    except OSError as e:
                        log.warning("Cannot open %s: %s", latest, e)
                        f = None
                        self.current_file = None
                        time.sleep(1)
                        continue
                    if first:
                        f.seek(0, os.SEEK_END)  # only react to what happens from now on
                    buf = ""
                    log.info("Following %s", latest)
                    if not first and self.state.status != "idle":
                        self._idle("game-restarted")
                elif f is not None:
                    # Detect truncation/rotation of the same file name
                    try:
                        if os.path.getsize(self.current_file) < f.tell():
                            f.seek(0)
                    except OSError:
                        pass
            if f is None:
                time.sleep(0.5)
                continue
            chunk = f.read()
            if chunk:
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    if line.startswith("["):
                        self.handle_line(line.rstrip("\r"))
            else:
                time.sleep(self.poll)
