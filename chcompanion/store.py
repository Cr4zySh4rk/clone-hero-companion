"""Small JSON store for learned/adjusted values (per-song offsets, chosen videos)."""
from __future__ import annotations

import json
import threading
from pathlib import Path

DEFAULTS = {"start_lead": 2.0, "lead_samples": 0, "lyrics_offset": 0.0, "songs": {}}


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data = json.loads(json.dumps(DEFAULTS))
        try:
            loaded = json.loads(path.read_text("utf-8"))
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except FileNotFoundError:
            pass
        except Exception:
            # corrupt file: keep a copy, start fresh
            try:
                path.rename(path.with_suffix(".bad.json"))
            except OSError:
                pass

    def save(self):
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), "utf-8")
            tmp.replace(self.path)

    def get(self, key, default=None):
        with self.lock:
            return self.data.get(key, default)

    def set(self, key, value):
        with self.lock:
            self.data[key] = value
        self.save()

    def song(self, song_key: str) -> dict:
        with self.lock:
            return dict(self.data["songs"].get(song_key, {}))

    def update_song(self, song_key: str, **values):
        with self.lock:
            entry = self.data["songs"].setdefault(song_key, {})
            for k, v in values.items():
                if v is None:
                    entry.pop(k, None)
                else:
                    entry[k] = v
        self.save()

    def learn_lead(self, observed: float):
        with self.lock:
            n = int(self.data.get("lead_samples", 0))
            cur = float(self.data.get("start_lead", DEFAULTS["start_lead"]))
            # running average, weighted towards recent observations after a few samples
            w = 1.0 / min(n + 1, 5)
            self.data["start_lead"] = round(cur * (1 - w) + observed * w, 3)
            self.data["lead_samples"] = n + 1
        self.save()
