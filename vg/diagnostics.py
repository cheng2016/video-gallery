# -*- coding: utf-8 -*-
"""Low-overhead structured diagnostics for large libraries.

Default: errors, warnings, and slow/key stages.
Full logging: also print key method calls and parameters in time order.
Loop/hot-path successes must use aggregate(), never per-item emit().
"""
from __future__ import annotations

import atexit
import contextlib
import threading
import time
import traceback
import uuid
from collections import defaultdict
from typing import Iterator

_full_logging = False
_slow_ms = 200.0
_agg_lock = threading.Lock()
_aggregates: dict[str, dict] = defaultdict(
    lambda: {
        "count": 0,
        "errors": 0,
        "total_ms": 0.0,
        "max_ms": 0.0,
        "samples": [],
    }
)
_last_aggregate_flush = time.monotonic()
_rate_lock = threading.Lock()
_last_events: dict[str, float] = {}


def set_full_logging(enabled: bool) -> None:
    global _full_logging
    _full_logging = bool(enabled)


def full_logging_enabled() -> bool:
    return bool(_full_logging)


def request_id() -> str:
    return uuid.uuid4().hex[:8]


def _fields_text(fields: dict) -> str:
    parts: list[str] = []
    for key, value in fields.items():
        if value is None or value == "":
            continue
        text = str(value).replace("\r", " ").replace("\n", " ")
        parts.append(f"{key}={text}")
    return " ".join(parts)


def emit(
    level: str,
    event: str,
    *,
    force: bool = False,
    detail: str = "",
    **fields,
) -> None:
    """Print one structured line.

    ERROR/WARN always print. Other levels need force=True (key/slow stages)
    or must go through call() for full-logging method traces.
    Full logging does not dump loop successes.
    """
    level_n = (level or "INFO").upper()
    if level_n not in {"ERROR", "WARN"} and not force:
        return
    suffix = _fields_text(fields)
    line = f"[{level_n}] {event}"
    if suffix:
        line += " " + suffix
    if detail:
        line += " detail=" + str(detail).replace("\r", " ").replace("\n", " | ")
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        from vg import bootlog

        bootlog.write(line, urgent=level_n == "ERROR")
    except Exception:
        pass


def error(event: str, exc: BaseException | None = None, **fields) -> None:
    """Always print an actionable error and traceback."""
    detail = ""
    if exc is not None:
        detail = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ).strip()
    emit("ERROR", event, force=True, detail=detail, **fields)


def call(method: str, **params) -> None:
    """Full-logging only: one timeline line for a key method and its arguments."""
    if not _full_logging:
        return
    emit("CALL", method, force=True, **params)


def emit_rate_limited(
    level: str,
    event: str,
    *,
    key: str = "",
    interval: float = 30.0,
    force: bool = False,
    **fields,
) -> bool:
    """Emit at most once per event/key window. Returns whether it emitted."""
    identity = f"{event}|{key}"
    now = time.monotonic()
    with _rate_lock:
        previous = _last_events.get(identity, 0.0)
        if now - previous < max(0.0, interval):
            return False
        _last_events[identity] = now
    emit(level, event, force=force, **fields)
    return True


def perf(event: str, elapsed_ms: float, *, force: bool = False, **fields) -> None:
    """Print slow stages by default; force=True for key pipeline summaries."""
    emit(
        "PERF",
        event,
        force=force or elapsed_ms >= _slow_ms,
        elapsed_ms=f"{elapsed_ms:.1f}",
        **fields,
    )


@contextlib.contextmanager
def span(event: str, *, force: bool = False, **fields) -> Iterator[dict]:
    """Measure a stage; callers may add result fields to the yielded dict."""
    started = time.perf_counter()
    result: dict = {}
    try:
        yield result
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000.0
        error(event, exc, elapsed_ms=f"{elapsed:.1f}", **fields, **result)
        raise
    else:
        elapsed = (time.perf_counter() - started) * 1000.0
        perf(event, elapsed, force=force, **fields, **result)


@contextlib.contextmanager
def timed_lock(
    lock,
    event: str,
    *,
    warn_after: float = 1.0,
    **fields,
) -> Iterator[None]:
    """Acquire a lock; only log when the wait is long enough to look stuck."""
    started = time.perf_counter()
    warned = False
    while True:
        acquired = lock.acquire(timeout=max(0.05, float(warn_after)))
        if acquired:
            break
        waited_ms = (time.perf_counter() - started) * 1000.0
        warned = True
        emit(
            "WARN",
            "lock_waiting",
            force=True,
            lock=event,
            waited_ms=f"{waited_ms:.1f}",
            thread=threading.current_thread().name,
            **fields,
        )
    waited_ms = (time.perf_counter() - started) * 1000.0
    if warned:
        emit(
            "PERF",
            "lock_acquired",
            force=True,
            lock=event,
            waited_ms=f"{waited_ms:.1f}",
            thread=threading.current_thread().name,
            **fields,
        )
    try:
        yield
    finally:
        lock.release()


def aggregate(event: str, elapsed_ms: float = 0.0, *, failed: bool = False) -> None:
    """Aggregate high-QPS success paths; errors must also call error()."""
    global _last_aggregate_flush
    now = time.monotonic()
    with _agg_lock:
        row = _aggregates[event]
        row["count"] += 1
        row["errors"] += int(failed)
        row["total_ms"] += max(0.0, elapsed_ms)
        row["max_ms"] = max(row["max_ms"], elapsed_ms)
        samples = row["samples"]
        if len(samples) < 512:
            samples.append(max(0.0, elapsed_ms))
        if now - _last_aggregate_flush < 5.0:
            return
        snapshot = dict(_aggregates)
        _aggregates.clear()
        _last_aggregate_flush = now
    for name, values in snapshot.items():
        _emit_aggregate(name, values)


def _emit_aggregate(name: str, values: dict) -> None:
        count = int(values["count"])
        avg = values["total_ms"] / count if count else 0.0
        samples = sorted(values["samples"])
        p50 = samples[int((len(samples) - 1) * 0.50)] if samples else 0.0
        p95 = samples[int((len(samples) - 1) * 0.95)] if samples else 0.0
        emit(
            "PERF",
            name,
            force=True,
            count=count,
            errors=int(values["errors"]),
            avg_ms=f"{avg:.1f}",
            p50_ms=f"{p50:.1f}",
            p95_ms=f"{p95:.1f}",
            max_ms=f"{values['max_ms']:.1f}",
        )


def flush_aggregates() -> None:
    global _last_aggregate_flush
    with _agg_lock:
        snapshot = dict(_aggregates)
        _aggregates.clear()
        _last_aggregate_flush = time.monotonic()
    for name, values in snapshot.items():
        _emit_aggregate(name, values)


atexit.register(flush_aggregates)
