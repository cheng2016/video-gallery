# -*- coding: utf-8 -*-
"""Priority scheduler for automatic thumbnail extraction.

All automatic thumbnail work shares a small daemon worker pool.  Visible
thumbnail requests can promote an already queued scan job, while ordinary
page/playback traffic briefly delays the start of the next background job.
"""
from __future__ import annotations

import itertools
import queue
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from vg.util import thumb_worker_count


THUMB_PRIORITY_VISIBLE = 0
THUMB_PRIORITY_BATCH = 10


@dataclass
class _ThumbJob:
    key: str
    work: Callable[[], bool]
    future: Future = field(default_factory=Future)
    priority: int = THUMB_PRIORITY_BATCH
    started: bool = False
    queued_at: float = field(default_factory=time.monotonic)


_queue: queue.PriorityQueue[tuple[int, int, _ThumbJob]] = queue.PriorityQueue()
_jobs: dict[str, _ThumbJob] = {}
_jobs_lock = threading.RLock()
_start_lock = threading.Lock()
_sequence = itertools.count()
_worker_n = 0
_foreground_until = 0.0
_THUMB_FAIL_COOLDOWN_S = 15 * 60
_failed_until: dict[str, float] = {}


def thumbnail_job_key(cache: Path | str, file_id: str) -> str:
    try:
        root = str(Path(cache).resolve())
    except OSError:
        root = str(cache)
    return f"{root.casefold()}::{file_id}"


def note_frontend_activity(hold_seconds: float = 0.6) -> None:
    """Ask not-yet-started thumbnail jobs to yield for a short quiet window."""
    global _foreground_until
    until = time.monotonic() + max(0.0, float(hold_seconds))
    with _jobs_lock:
        if until > _foreground_until:
            _foreground_until = until


def batch_thumbnail_slots() -> int:
    """How many batch ffmpeg jobs may run while the UI is active.

    Visible requests already have the higher queue priority and therefore run
    before waiting batch jobs.  Reducing a large batch to one worker whenever
    the browser polls thumbnails caused a self-sustaining backlog: every page
    refresh extended the foreground window while hundreds of ffmpeg jobs
    waited.  Keep all configured workers available and let priority provide
    the fairness instead of throttling the whole batch.
    """
    return max(1, _worker_n)


def _wait_for_frontend_idle() -> None:
    started = time.monotonic()
    while True:
        with _jobs_lock:
            remaining = _foreground_until - time.monotonic()
        if remaining <= 0:
            waited = time.monotonic() - started
            if waited >= 0.5:
                from vg.diagnostics import emit

                emit(
                    "PERF",
                    "thumbnail_batch_frontend_yield",
                    force=True,
                    waited_ms=f"{waited * 1000.0:.1f}",
                    pending=pending_thumbnail_jobs(),
                )
            return
        time.sleep(min(0.1, remaining))


def _worker() -> None:
    while True:
        _priority, _seq, job = _queue.get()
        try:
            requeued = False
            with _jobs_lock:
                current = _jobs.get(job.key)
                if current is not job or job.started:
                    continue
                # Batch jobs yield harder when the page is actively loading thumbs.
                if job.priority >= THUMB_PRIORITY_BATCH:
                    # Do not claim a batch slot while the foreground hold is
                    # active.  Otherwise every worker can enter
                    # _wait_for_frontend_idle after claiming a batch item and
                    # visible requests wait behind the scan batch.
                    if _foreground_until > time.monotonic():
                        _queue.put((job.priority, next(_sequence), job))
                        requeued = True
                    else:
                        started_batch = sum(
                            1
                            for other in _jobs.values()
                            if other.started and other.priority >= THUMB_PRIORITY_BATCH
                        )
                        if started_batch >= batch_thumbnail_slots():
                            _queue.put((job.priority, next(_sequence), job))
                            requeued = True
                        else:
                            job.started = True
                else:
                    job.started = True
            if requeued:
                time.sleep(0.05)
                continue
            if job.priority >= THUMB_PRIORITY_BATCH:
                _wait_for_frontend_idle()
            queue_wait_ms = (time.monotonic() - job.queued_at) * 1000.0
            if queue_wait_ms >= 1000.0:
                from vg.diagnostics import aggregate, emit_rate_limited

                aggregate("thumbnail_job_queue_wait", queue_wait_ms)
                if queue_wait_ms >= 5000.0:
                    emit_rate_limited(
                        "WARN",
                        "thumbnail_job_queue_wait",
                        key="backlog",
                        interval=30.0,
                        force=True,
                        waited_ms=f"{queue_wait_ms:.1f}",
                        pending=pending_thumbnail_jobs(),
                        worker=threading.current_thread().name,
                    )
            work_started = time.monotonic()
            try:
                result = bool(job.work())
            except BaseException as exc:
                from vg.diagnostics import error

                error(
                    "thumbnail_job_exception",
                    exc,
                    key=job.key,
                    priority=job.priority,
                    queue_wait_ms=f"{queue_wait_ms:.1f}",
                )
                with _jobs_lock:
                    _failed_until[job.key] = time.monotonic() + _THUMB_FAIL_COOLDOWN_S
                job.future.set_exception(exc)
            else:
                from vg.diagnostics import aggregate

                if result:
                    with _jobs_lock:
                        _failed_until.pop(job.key, None)
                    aggregate(
                        "thumbnail_job",
                        (time.monotonic() - work_started) * 1000.0,
                    )
                else:
                    with _jobs_lock:
                        _failed_until[job.key] = time.monotonic() + _THUMB_FAIL_COOLDOWN_S
                    from vg.diagnostics import emit

                    emit(
                        "WARN",
                        "thumbnail_job_failed",
                        force=True,
                        key=job.key,
                        priority=job.priority,
                        queue_wait_ms=f"{queue_wait_ms:.1f}",
                    )
                job.future.set_result(result)
            finally:
                with _jobs_lock:
                    if _jobs.get(job.key) is job:
                        _jobs.pop(job.key, None)
        finally:
            _queue.task_done()


def ensure_thumbnail_workers(n: int | None = None) -> None:
    """Start daemon workers; later burst scans may expand the pool, never shrink it."""
    global _worker_n
    want = max(1, int(n) if n is not None else thumb_worker_count())
    with _start_lock:
        before = _worker_n
        while _worker_n < want:
            _worker_n += 1
            threading.Thread(
                target=_worker,
                daemon=True,
                name=f"thumb-worker-{_worker_n}",
            ).start()
        if _worker_n != before:
            from vg.diagnostics import emit

            emit(
                "INFO",
                "thumbnail_workers_started",
                force=True,
                before=before,
                workers=_worker_n,
                requested=want,
            )


def _ensure_workers() -> None:
    ensure_thumbnail_workers()


def thumbnail_recently_failed(key: str) -> bool:
    until = _failed_until.get(key)
    return bool(until and until > time.monotonic())


def submit_thumbnail_job(
    key: str,
    work: Callable[[], bool],
    *,
    priority: int = THUMB_PRIORITY_BATCH,
) -> Future:
    """Queue one deduplicated job; lower priority numbers run first.

    A visible request may promote a batch job that is still waiting.  The old
    queue entry is harmless: workers ignore it after the promoted entry starts.
    """
    _ensure_workers()
    with _jobs_lock:
        until = _failed_until.get(key)
        if until and until > time.monotonic():
            from vg.diagnostics import aggregate

            aggregate("thumbnail_generation_skipped_recent_fail")
            done = Future()
            done.set_result(False)
            return done
        if len(_failed_until) > 2000:
            now = time.monotonic()
            for stale in [k for k, exp in _failed_until.items() if exp <= now]:
                _failed_until.pop(stale, None)
        existing = _jobs.get(key)
        if existing is not None:
            if not existing.started and priority < existing.priority:
                existing.priority = priority
                _queue.put((priority, next(_sequence), existing))
            return existing.future
        job = _ThumbJob(key=key, work=work, priority=priority)
        _jobs[key] = job
        _queue.put((priority, next(_sequence), job))
        depth = len(_jobs)
        if depth in (50, 100) or (depth >= 200 and depth % 100 == 0):
            from vg.diagnostics import emit

            emit(
                "WARN",
                "thumbnail_queue_backlog",
                force=True,
                pending=depth,
                workers=_worker_n,
                priority=priority,
            )
        return job.future


def pending_thumbnail_jobs() -> int:
    with _jobs_lock:
        return len(_jobs)
