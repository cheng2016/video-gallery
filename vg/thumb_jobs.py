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


_queue: queue.PriorityQueue[tuple[int, int, _ThumbJob]] = queue.PriorityQueue()
_jobs: dict[str, _ThumbJob] = {}
_jobs_lock = threading.Lock()
_start_lock = threading.Lock()
_sequence = itertools.count()
_workers_started = False
_foreground_until = 0.0


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


def _wait_for_frontend_idle() -> None:
    while True:
        with _jobs_lock:
            remaining = _foreground_until - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))


def _worker() -> None:
    while True:
        _priority, _seq, job = _queue.get()
        try:
            with _jobs_lock:
                current = _jobs.get(job.key)
                if current is not job or job.started:
                    continue
                job.started = True
            _wait_for_frontend_idle()
            try:
                result = bool(job.work())
            except BaseException as exc:
                job.future.set_exception(exc)
            else:
                job.future.set_result(result)
            finally:
                with _jobs_lock:
                    if _jobs.get(job.key) is job:
                        _jobs.pop(job.key, None)
        finally:
            _queue.task_done()


def _ensure_workers() -> None:
    global _workers_started
    if _workers_started:
        return
    with _start_lock:
        if _workers_started:
            return
        for index in range(thumb_worker_count()):
            threading.Thread(
                target=_worker,
                daemon=True,
                name=f"thumb-worker-{index + 1}",
            ).start()
        _workers_started = True


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
        existing = _jobs.get(key)
        if existing is not None:
            if not existing.started and priority < existing.priority:
                existing.priority = priority
                _queue.put((priority, next(_sequence), existing))
            return existing.future
        job = _ThumbJob(key=key, work=work, priority=priority)
        _jobs[key] = job
        _queue.put((priority, next(_sequence), job))
        return job.future


def pending_thumbnail_jobs() -> int:
    with _jobs_lock:
        return len(_jobs)
