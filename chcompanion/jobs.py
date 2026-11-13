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

from .i18n import _, msg_of

log = logging.getLogger(__name__)

PRIO_HOVER, PRIO_USER, PRIO_PLAYED, PRIO_BATCH, PRIO_LIBRARY = 0, 1, 2, 3, 5


from .cancel import Cancelled, bind as _bind_cancel  # noqa: E402,F401  (Cancelled is re-exported here)


def _is_cancel_error(e: BaseException) -> bool:
    """Older code raised RuntimeError(_("job.cancelled")); it is a cancel, not a failure."""
    return getattr(msg_of(e), "key", "") == "job.cancelled"


class Deferred:
    """What a runner may return instead of a result: the rest of the job ("convert the video, then pack it")
    to run on the conversion stage so the job worker can start the next song. run(progress) does the work and
    returns the result string (cleaning up after itself, also when it raises); abort() is for a continuation that
    never runs (app quit) and must clean up the same way."""

    def __init__(self, run, abort=None):
        self.run, self.abort = run, abort or (lambda: None)


class ConversionStage:
    """A bounded, process-wide pool: `slots` threads run the deferred conversions of jobs, at most `slots * 2`
    more wait in line (put() blocks, cancel-aware, when full, so downloaded files cannot pile up). Lines are
    ordered by job priority, then arrival, like the main queue."""

    def __init__(self, slots: int, run_one):
        self.slots = max(1, int(slots))
        self.cap = self.slots * 2
        self.run_one = run_one
        self.cv = threading.Condition()
        self.heap: list = []
        self.seq = itertools.count()
        self.closed = False
        self.threads = [threading.Thread(target=self._loop, daemon=True, name=f"convert-{i}")
                        for i in range(self.slots)]
        for t in self.threads:
            t.start()

    def put(self, job, deferred) -> None:
        """Queue the job's continuation; waits while the line is full. Raises Cancelled if the job is cancelled
        meanwhile (the caller aborts the continuation)."""
        with self.cv:
            while len(self.heap) >= self.cap and not self.closed:
                if job.cancel.is_set():
                    raise Cancelled()
                self.cv.wait(0.2)
            if self.closed:
                raise Cancelled()
            heapq.heappush(self.heap, (job.priority, next(self.seq), job, deferred))
            self.cv.notify_all()

    def waiting(self) -> int:
        with self.cv:
            return len(self.heap)

    def _loop(self):
        while True:
            with self.cv:
                while not self.heap and not self.closed:
                    self.cv.wait()
                if self.closed:
                    return
                _, _, job, deferred = heapq.heappop(self.heap)
                self.cv.notify_all()          # room in the line: a blocked worker may hand off
            self.run_one(job, deferred)

    def shutdown(self) -> list:
        """Stop the threads; returns the continuations that never ran (the caller aborts them)."""
        with self.cv:
            self.closed = True
            left = [(j, d) for _p, _s, j, d in self.heap]
            self.heap.clear()
            self.cv.notify_all()
        return left


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
    video_added: bool = False      # the run put a video into the song (the UI then reminds about Scan Songs)

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title, "songId": self.song_id,
                "status": self.status, "step": self.step, "result": self.result, "error": self.error,
                "priority": self.priority, "created": self.created, "started": self.started,
                "finished": self.finished, "videoAdded": self.video_added}


class JobManager:
    def __init__(self, runner, workers: int | None = None, busy=None, on_change=None,
                 conversion_slots: int | None = None):
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
        self.active = 0          # jobs running, including those in the conversion stage
        self.paused = False
        self._slots = conversion_slots
        self._stage: ConversionStage | None = None
        self._closed = False

    def stage(self) -> ConversionStage:
        with self.lock:
            if self._closed:
                raise Cancelled()
            if self._stage is None:
                n = self._slots
                if n is None:
                    from .hwvideo import encode_slots
                    n = encode_slots()
                self._stage = ConversionStage(n, self._convert_one)
            return self._stage

    def shutdown(self):
        """App quit: stop the workers and the conversion stage; queued conversions clean up and end cancelled."""
        with self.lock:
            self._closed = True
            self.target_workers = 0
            stage = self._stage
            self.lock.notify_all()
        left = stage.shutdown() if stage else []
        for job, d in left:
            try:
                d.abort()
            except Exception as e:
                log.warning("Could not clean up the queued conversion of %s: %s", job.title, e)
            with self.lock:
                if job.status == "running":
                    job.status, job.step = "cancelled", ""
                job.finished = job.finished or time.time()
                self.active -= 1
            self.on_change(job)

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

    def cancel(self, job_id: str | None = None, kind: str | None = None):
        with self.lock:
            targets = [self.jobs[job_id]] if job_id in self.jobs else (
                list(self.jobs.values()) if job_id is None else [])
            if kind:
                targets = [j for j in targets if j.kind == kind]
            queued = []
            for j in targets:
                if j.status in ("queued", "waiting", "running"):
                    j.cancel.set()
                    if j.status != "running":      # never started: finished right now
                        j.status, j.step, j.finished = "cancelled", "", time.time()
                        queued.append(j)
                    else:                          # shown cancelled at once; the worker stops the work
                        j.status, j.step = "cancelled", ""
            self.lock.notify_all()
        for j in queued:
            self.on_change(j)

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
                        j.status, j.step = "waiting", _("job.wait_cooldown", time=f"{left // 60}:{left % 60:02d}")
                        continue
                    if self.busy(j):
                        deferred.append((prio, order, jid))
                        if j.status != "waiting":
                            j.status, j.step = "waiting", _("job.wait_playing")
                        continue
                    job = j
                    break
                for d in deferred:
                    heapq.heappush(self.heap, d)
                if job:
                    job.status, job.started, job.step = "running", time.time(), _("job.starting")
                    self.active += 1
                    return job
                self.lock.wait(timeout=2.0)

    def _worker(self):
        while True:
            job = self._next()
            if job is None:
                return
            self.on_change(job)
            self._process(job, lambda progress: self.runner(job, progress), may_defer=True)

    def _convert_one(self, job: Job, deferred: Deferred):
        """A conversion-stage thread: finish the job exactly as a worker would have."""
        self._process(job, deferred.run, may_defer=False)

    def _process(self, job: Job, fn, may_defer: bool):
        """Run fn(progress) for the job and settle its final state. A runner may return a Deferred: the job then
        stays "running" (visible as converting) and moves to the conversion stage, freeing this worker."""
        if True:
            def progress(msg, job=job):
                if job.cancel.is_set():
                    raise Cancelled()
                if job.status == "cancelled":
                    return
                job.step = msg
                if getattr(msg, "key", "") not in ("job.wait_cooldown", "yt.pacing"):
                    job.work_step = msg
                self.on_change(job)
            handed = False
            try:
                if may_defer and job.cancel.is_set():
                    raise Cancelled()
                with _bind_cancel(job.cancel):
                    res = fn(progress)
                    if may_defer and isinstance(res, Deferred):
                        job.step = _("ingame.waiting_encoder")
                        try:
                            self.stage().put(job, res)
                        except BaseException:
                            res.abort()
                            raise
                        handed = True       # the stage finishes the job; active stays counted
                        self.on_change(job)
                        return
                    job.result = res or _("job.done")
                job.status = "cancelled" if job.cancel.is_set() else "done"
                job.step = ""
            except Cancelled:
                job.status, job.step = "cancelled", ""
            except Exception as e:
                detail = None
                if job.cancel.is_set() or _is_cancel_error(e):
                    job.status, job.step = "cancelled", ""
                elif type(e).__name__ == "BotDetected":
                    # YouTube really is limiting us (429 / "not a bot"): the gate pauses everyone. Wait
                    # out the cool-down and retry; fail only once every cool-down has been used.
                    from . import youtube
                    youtube.GATE.throttled(e, getattr(e, "video_id", None))
                    job.cooldowns += 1
                    if job.cooldowns <= youtube.GATE.max_waits():
                        left = max(youtube.GATE.remaining(), 1)
                        with self.lock:
                            job.not_before = time.time() + left
                            job.status = "waiting"
                            job.step = _("job.wait_cooldown", time=f"{left // 60}:{left % 60:02d}")
                            heapq.heappush(self.heap, (job.priority, next(self.order), job.id))
                            self.active -= 1
                        self.on_change(job)
                        return
                    sig = youtube.SIGNAL_LABELS.get((youtube.GATE.last_limit or {}).get("signal"), "")
                    why = (_("job.yt_limited_sig", count=job.cooldowns - 1, signal=sig) if sig
                           else _("job.yt_limited", count=job.cooldowns - 1))
                    log.warning("Job %s (%s) failed for %s: %s", job.id, job.kind, job.title, str(why))
                    job.status = "failed"
                    job.error = _("job.error_in_step", step=job.work_step or _("job.searching_youtube"), detail=why)
                    job.not_before = 0.0
                else:
                    detail = msg_of(e)
                # Keep the operation and traceback: Windows' missing-executable
                # errors often have no filename and otherwise look identical.
                if detail is not None:
                    log.exception("Job %s (%s) failed for %s during %s: %s",
                                  job.id, job.kind, job.title, job.step or "starting", detail)
                    job.status = "failed"
                    job.error = _("job.error_in_step", step=job.step, detail=detail) if job.step else detail
            finally:
                if not handed and job.status != "waiting":
                    job.finished = job.finished if job.status == "cancelled" and job.finished else time.time()
            with self.lock:
                if job.status != "waiting":
                    self.active -= 1
                self.lock.notify_all()
            self.on_change(job)
