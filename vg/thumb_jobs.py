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


_fg_note_seq = 0


def note_frontend_activity(hold_seconds: float = 0.6, source: str = "unknown") -> None:
    """Ask not-yet-started thumbnail jobs to yield for a short quiet window.

    ``source`` identifies which subsystem is keeping the "foreground busy"
    flag alive so we can distinguish, from logs, whether a massive backlog
    was caused by:
      - the polling ticker calling /api/status (cheap, should yield briefly),
      - the list view paging /api/videos (medium),
      - visible thumbnails on /thumb/* (legitimate, must yield), or
      - playback /stream/* (long 4s hold, expected).

    A rate-limited event is emitted on large pushes so we can trace the
    exact chain of `_foreground_until` bumps that ended up delaying a
    batch thumbnail by 30+ seconds.
    """
    global _foreground_until, _fg_note_seq
    hold = max(0.0, float(hold_seconds))
    now = time.monotonic()
    until = now + hold
    bumped = False
    with _jobs_lock:
        if until > _foreground_until:
            previous = _foreground_until
            _foreground_until = until
            bumped = True
    if bumped and hold >= 0.4:
        _fg_note_seq += 1
        from vg.diagnostics import aggregate, emit_rate_limited

        aggregate("foreground_activity_note", hold * 1000.0)
        emit_rate_limited(
            "INFO",
            "foreground_activity_note",
            key=source,
            interval=10.0,
            force=False,
            source=source,
            hold_s=f"{hold:.2f}",
            until_extended_ms=f"{max(0.0, until - previous) * 1000.0:.0f}" if previous else f"{hold * 1000.0:.0f}",
            pending=pending_thumbnail_jobs(),
            seq=_fg_note_seq,
        )


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
    """Yield briefly for visible thumbnail work, but never block a worker.

    The previous implementation sat in a ``while _foreground_until > now``
    loop sleeping up to 0.1s per iteration.  When the browser kept polling
    (status + thumbnail fetches every ~2s), ``note_frontend_activity`` kept
    pushing ``_foreground_until`` forward and every batch worker ended up
    parked here *simultaneously*.  The result was ``thumbnail_job_queue_wait``
    warnings of 35+ seconds and a scan backlog that never drained while
    anyone was browsing the page, even though ``PriorityQueue`` already
    guarantees visible jobs run before batch jobs purely by priority value.

    The new contract is intentionally minimal: do a bounded, one-shot sleep
    so high-priority foreground jobs that were queued right before this batch
    job was dequeued have a chance to be claimed by one of the other
    workers.  If the browser stays active, batch workers still make forward
    progress (slowly) instead of being parked and creating a self-sustaining
    backlog that explodes waited_ms.
    """
    started = time.monotonic()
    with _jobs_lock:
        remaining = _foreground_until - time.monotonic()
    if remaining > 0.0:
        # Bound the yield.  120 ms is long enough for any visible request
        # already on the queue to be picked up by another worker, but short
        # enough that a 1822-video batch still drains in < 4 minutes on a
        # 14-worker pool even while the UI is alive.
        time.sleep(min(0.12, remaining))
    waited = time.monotonic() - started
    if waited >= 0.05:
        from vg.diagnostics import aggregate

        aggregate("thumbnail_batch_frontend_yield", waited * 1000.0)


def _max_batch_started_during_foreground() -> int:
    """How many batch slots may be claimed while the UI hold is active.

    Previously *all* batch slots were deferred while ``_foreground_until``
    was in the future which caused 100% of workers to requeue + sleep 50ms
    over and over again.  With N workers we now allow ``ceil(N/4)`` batch
    workers to actually start, so the backlog drains slowly even if the
    user stays on the page, while the remaining workers remain immediately
    available for visible thumbnail requests.  When no foreground hold is
    active the full ``batch_thumbnail_slots()`` still applies.
    """
    return max(1, (_worker_n + 3) // 4)


def _worker() -> None:
    while True:
        _priority, _seq, job = _queue.get()
        try:
            requeued = False
            with _jobs_lock:
                current = _jobs.get(job.key)
                if current is not job or job.started:
                    continue
                if job.priority >= THUMB_PRIORITY_BATCH:
                    foreground_active = _foreground_until > time.monotonic()
                    started_batch = sum(
                        1
                        for other in _jobs.values()
                        if other.started and other.priority >= THUMB_PRIORITY_BATCH
                    )
                    if foreground_active:
                        # Do not let *every* worker claim a batch item while
                        # a UI hold is active; reserve the rest for visible
                        # work.  But also do not defer every single worker,
                        # which previously caused the whole queue to stall
                        # and produce 35s waited_ms while browsing.
                        cap = _max_batch_started_during_foreground()
                    else:
                        cap = batch_thumbnail_slots()
                    if started_batch >= cap:
                        _queue.put((job.priority, next(_sequence), job))
                        requeued = True
                    else:
                        job.started = True
                else:
                    job.started = True
            if requeued:
                # Reduced from 50 ms to 10 ms.  Combined with the new
                # ``_max_batch_started_during_foreground`` cap, requeues are
                # now far less frequent and do not need a long sleep to
                # avoid busy looping on the PriorityQueue.
                time.sleep(0.01)
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
