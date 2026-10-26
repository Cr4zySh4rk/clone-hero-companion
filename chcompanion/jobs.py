"""Multithreaded job queue for everything slow: video + lyrics for the game,
metadata, chart downloads.

* N worker threads (Settings -> Workers, default = CPU cores capped at 4) pull jobs
  from one priority queue, so a big selection is processed in parallel.
* YouTube calls inside jobs share youtube.GATE (concurrency + spacing + global
  back-off), downloads share youtube.DOWNLOADS, so more workers never means more
  bot challenges.
* A song that Clone Hero is currently playing is never modified: its job waits.
"""
from __future__ import annotations

import heapq
import itertools
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

PRIO_HOVER, PRIO_USER, PRIO_PLAYED, PRIO_BATCH, PRIO_LIBRARY = 0, 1, 2, 3, 5


class Cancelled(Exception):
    pass


@dataclass
class Job:
    kind: str                      # media | metadata | full | download
    title: str
    song_id: str | None = None
    item: dict | None = None       # download source item
    options: dict = field(default_factory=dict)
    priority: int = PRIO_BATCH
    chart: dict | None = field(default=None, repr=False)     # which chart file this job was queued for
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    status: str = "queued"         # queued | running | waiting | done | failed | cancelled
    step: str = ""
    result: str = ""
    error: str = ""
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    not_before: float = 0.0        # a YouTube cool-down ends then
    work_step: str = ""            # the last real step (not a cool-down notice), for error messages
    cooldowns: int = 0             # cool-downs this job has already waited through

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title, "songId": self.song_id,
                "status": self.status, "step": self.step, "result": self.result, "error": self.error,
                "priority": self.priority, "created": self.created, "started": self.started,
                "finished": self.finished}


class JobManager:
    def __init__(self, runner, workers: int | None = None, busy=None, on_change=None):
        """runner(job, progress) does the work and returns a result string.
        busy(job) -> True while the job's song is in use (playing)."""
        self.runner = runner
        self.busy = busy or (lambda job: False)
        self.on_change = on_change or (lambda job: None)
        self.lock = threading.Condition()
        self.heap: list[tuple[int, int, str]] = []
        self.jobs: dict[str, Job] = {}
        self.order = itertools.count()
        self.threads: list[threading.Thread] = []
        self.target_workers = workers or min(4, os.cpu_count() or 2)
        self.active = 0
        self.paused = False

    # ------------------------------------------------------------ control
    def start(self):
        self.set_workers(self.target_workers)

    def set_workers(self, n: int):
        n = max(1, min(int(n), 16))
        with self.lock:
            self.target_workers = n
            alive = [t for t in self.threads if t.is_alive()]
            self.threads = alive
            for _ in range(n - len(alive)):
                t = threading.Thread(target=self._worker, daemon=True, name=f"job-worker-{len(self.threads)}")
                self.threads.append(t)
                t.start()
            self.lock.notify_all()

    def submit(self, job: Job) -> Job:
        with self.lock:
            # one queued job per song+kind: raise priority instead of duplicating
            for j in self.jobs.values():
                if (j.status in ("queued", "waiting") and job.song_id and j.song_id == job.song_id
                        and (j.kind == job.kind or {j.kind, job.kind} == {"media", "full"})):
                    if j.kind == "media" and job.kind == "full":
                        j.kind, j.title = "full", job.title      # "full" covers the video and lyrics too
                    if job.priority < j.priority:
                        j.priority = job.priority
                        heapq.heappush(self.heap, (j.priority, next(self.order), j.id))
                    j.options.update(job.options)
                    self.lock.notify()
                    return j
            self.jobs[job.id] = job
            heapq.heappush(self.heap, (job.priority, next(self.order), job.id))
            self.lock.notify()
        self.on_change(job)
        return job

    def cancel(self, job_id: str | None = None):
        with self.lock:
            targets = [self.jobs[job_id]] if job_id in self.jobs else (
                list(self.jobs.values()) if job_id is None else [])
            for j in targets:
                if j.status in ("queued", "waiting", "running"):
                    j.cancel.set()
                    if j.status != "running":
                        j.status, j.finished = "cancelled", time.time()
            self.lock.notify_all()

    def retry_failed(self):
        with self.lock:
            for j in list(self.jobs.values()):
                if j.status in ("failed", "cancelled"):
                    nj = Job(j.kind, j.title, j.song_id, j.item, dict(j.options), j.priority)
                    nj.chart = j.chart
                    del self.jobs[j.id]
                    self.jobs[nj.id] = nj
                    heapq.heappush(self.heap, (nj.priority, next(self.order), nj.id))
            self.lock.notify_all()

    def clear_finished(self):
        with self.lock:
            for k in [k for k, j in self.jobs.items() if j.status in ("done", "failed", "cancelled")]:
                del self.jobs[k]

    def snapshot(self, limit: int = 400) -> dict:
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda j: (
                {"running": 0, "waiting": 1, "queued": 2}.get(j.status, 3), j.priority, -j.finished, j.created))
            counts: dict[str, int] = {}
            for j in self.jobs.values():
                counts[j.status] = counts.get(j.status, 0) + 1
            done_times = [j.finished - j.started for j in self.jobs.values()
                          if j.status == "done" and j.started and j.finished]
        return {"jobs": [j.public() for j in jobs[:limit]], "counts": counts,
                "workers": self.target_workers, "active": self.active,
                "avgSeconds": round(sum(done_times) / len(done_times), 1) if done_times else None}

    # ------------------------------------------------------------ workers
    def _next(self) -> Job | None:
        with self.lock:
            while True:
                if threading.current_thread() not in self.threads[: self.target_workers]:
                    return None   # worker count was reduced
                deferred = []
                job = None
                while self.heap:
                    prio, order, jid = heapq.heappop(self.heap)
                    j = self.jobs.get(jid)
                    if not j or j.status not in ("queued", "waiting") or j.priority != prio:
                        continue
                    if j.cancel.is_set():
                        j.status = "cancelled"
                        continue
                    if j.not_before > time.time():
                        deferred.append((prio, order, jid))
                        left = int(j.not_before - time.time() + 0.999)
                        j.status, j.step = "waiting", f"waiting for YouTube cool-down {left // 60}:{left % 60:02d}"
                        continue
                    if self.busy(j):
                        deferred.append((prio, order, jid))
                        if j.status != "waiting":
                            j.status, j.step = "waiting", "waiting until the song isn't playing"
                        continue
                    job = j
                    break
                for d in deferred:
                    heapq.heappush(self.heap, d)
                if job:
                    job.status, job.started, job.step = "running", time.time(), "starting"
                    self.active += 1
                    return job
                self.lock.wait(timeout=2.0)

    def _worker(self):
        while True:
            job = self._next()
            if job is None:
                return
            self.on_change(job)

            def progress(msg: str, job=job):
                if job.cancel.is_set():
                    raise Cancelled()
                job.step = msg
                if not msg.startswith("waiting for YouTube"):
                    job.work_step = msg
                self.on_change(job)
            try:
                job.result = self.runner(job, progress) or "done"
                job.status = "done"
                job.step = ""
            except Cancelled:
                job.status, job.step = "cancelled", ""
            except Exception as e:
                if type(e).__name__ == "BotDetected":
                    # YouTube really is limiting us (429 / "not a bot"): the gate pauses everyone. Wait
                    # out the cool-down and retry; fail only once every cool-down has been used.
                    from . import youtube
                    youtube.GATE.throttled()
                    job.cooldowns += 1
                    if job.cooldowns <= youtube.GATE.max_waits():
                        left = max(youtube.GATE.remaining(), 1)
                        with self.lock:
                            job.not_before = time.time() + left
                            job.status = "waiting"
                            job.step = f"waiting for YouTube cool-down {left // 60}:{left % 60:02d}"
                            heapq.heappush(self.heap, (job.priority, next(self.order), job.id))
                            self.active -= 1
                        self.on_change(job)
                        continue
                    why = f"YouTube kept limiting requests after {job.cooldowns - 1} cool-down(s); try again later"
                    log.warning("Job %s (%s) failed for %s: %s", job.id, job.kind, job.title, why)
                    job.status = "failed"
                    job.error = f"{job.work_step or 'searching YouTube'}: {why}"
                    job.not_before = 0.0
                    detail = None
                else:
                    detail = str(e) or type(e).__name__
                # Keep the operation and traceback: Windows' missing-executable
                # errors often have no filename and otherwise look identical.
                if detail is not None:
                    log.exception("Job %s (%s) failed for %s during %s: %s",
                                  job.id, job.kind, job.title, job.step or "starting", detail)
                    job.status = "failed"
                    job.error = f"{job.step}: {detail}" if job.step else detail
            finally:
                if job.status != "waiting":
                    job.finished = time.time()
            with self.lock:
                if job.status != "waiting":
                    self.active -= 1
                self.lock.notify_all()
            self.on_change(job)
