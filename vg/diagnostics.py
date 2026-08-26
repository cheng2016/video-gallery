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

# HTTP hang diagnostics: track in-flight Flask requests and lock holders so a
# dead waitress pool still leaves a trail even when new requests never enter.
_inflight_lock = threading.Lock()
_inflight_requests: dict[str, dict] = {}
_last_request_begin = 0.0
_last_request_end = 0.0
_lock_holder_lock = threading.Lock()
_lock_holders: dict[str, dict] = {}
_stall_watchdog_started = False
_STALL_WARN_MS = 3000.0
_STALL_POLL_S = 2.0


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


def info(event: str, *, force: bool = False, **fields) -> None:
    """Emit an INFO-level diagnostic event."""
    emit("INFO", event, force=force or _full_logging, **fields)


def warn(event: str, *, force: bool = True, **fields) -> None:
    """Emit a WARN-level diagnostic event (always printed)."""
    emit("WARN", event, force=force, **fields)


def perf(event: str, elapsed_ms: float, *, force: bool = False, **fields) -> None:
    """Print slow stages by default; full logging prints every instrumented span."""
    emit(
        "PERF",
        event,
        force=force or _full_logging or elapsed_ms >= _slow_ms,
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
        holder = lock_holder_snapshot(event)
        emit(
            "WARN",
            "lock_waiting",
            force=True,
            lock=event,
            waited_ms=f"{waited_ms:.1f}",
            thread=threading.current_thread().name,
            holder=holder.get("holder") or "",
            holder_thread=holder.get("thread") or "",
            holder_held_ms=holder.get("held_ms") or 0,
            **fields,
        )
    waited_ms = (time.perf_counter() - started) * 1000.0
    # Successful uncontended locks are hot-path noise.  Even with full
    # logging enabled, only report an acquisition when the caller actually
    # waited; otherwise large metadata loops can emit thousands of lines and
    # distort the timings they are meant to diagnose.
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
    mark_lock_held(event, thread=threading.current_thread().name, **fields)
    try:
        yield
    finally:
        mark_lock_released(event)
        lock.release()


def mark_lock_held(event: str, *, thread: str = "", **fields) -> None:
    with _lock_holder_lock:
        _lock_holders[str(event)] = {
            "holder": str(event),
            "thread": thread or threading.current_thread().name,
            "since": time.perf_counter(),
            "fields": dict(fields),
        }


def mark_lock_released(event: str) -> None:
    with _lock_holder_lock:
        _lock_holders.pop(str(event), None)


def lock_holder_snapshot(event: str = "") -> dict:
    with _lock_holder_lock:
        if event:
            row = dict(_lock_holders.get(str(event)) or {})
        else:
            # Longest-held lock overall (best-effort hang clue).
            row = {}
            oldest = None
            for item in _lock_holders.values():
                since = float(item.get("since") or 0.0)
                if oldest is None or since < oldest:
                    oldest = since
                    row = dict(item)
    if not row:
        return {"holder": "", "thread": "", "held_ms": 0}
    since = float(row.get("since") or 0.0)
    held_ms = ((time.perf_counter() - since) * 1000.0) if since else 0.0
    return {
        "holder": row.get("holder") or "",
        "thread": row.get("thread") or "",
        "held_ms": round(held_ms, 1),
    }


def lock_holders_summary(limit: int = 8) -> str:
    now = time.perf_counter()
    with _lock_holder_lock:
        rows = sorted(
            (
                (
                    (now - float(item.get("since") or now)) * 1000.0,
                    str(item.get("holder") or ""),
                    str(item.get("thread") or ""),
                )
                for item in _lock_holders.values()
            ),
            reverse=True,
        )
    parts = [
        f"{name}@{thread}:{held_ms:.0f}ms"
        for held_ms, name, thread in rows[: max(1, int(limit))]
        if name
    ]
    return " | ".join(parts)


def begin_request_trace(
    request_id: str,
    *,
    method: str = "",
    path: str = "",
    operation_id: str = "",
) -> None:
    """Record one in-flight HTTP request for the stall watchdog."""
    global _last_request_begin
    rid = str(request_id or "") or uuid.uuid4().hex[:8]
    now = time.perf_counter()
    with _inflight_lock:
        _last_request_begin = now
        _inflight_requests[rid] = {
            "request_id": rid,
            "method": method,
            "path": path,
            "operation_id": operation_id,
            "thread": threading.current_thread().name,
            "started": now,
        }
        inflight_n = len(_inflight_requests)
    # Always leave a begin breadcrumb for UI/API paths.  If the server later
    # wedges, this is the last proof a waitress thread accepted the request.
    hot_skip = path.startswith(("/thumb/", "/stream/", "/hls/")) or path in (
        "/api/status",
        "/api/client-log",
        "/favicon.ico",
        "/robots.txt",
    )
    if not hot_skip:
        emit(
            "INFO",
            "http_request_begin",
            force=True,
            request_id=rid,
            operation_id=operation_id,
            method=method,
            path=path,
            thread=threading.current_thread().name,
            inflight=inflight_n,
        )
    elif path == "/api/status":
        # Status polls every couple seconds — keep inflight tracking, log rarely.
        emit_rate_limited(
            "INFO",
            "http_request_begin",
            key="api_status",
            interval=10.0,
            force=True,
            request_id=rid,
            path=path,
            thread=threading.current_thread().name,
            inflight=inflight_n,
        )


def end_request_trace(request_id: str, *, status: int | None = None) -> None:
    global _last_request_end
    rid = str(request_id or "")
    now = time.perf_counter()
    with _inflight_lock:
        row = _inflight_requests.pop(rid, None)
        _last_request_end = now
        inflight_n = len(_inflight_requests)
    if not row:
        return
    elapsed_ms = (now - float(row.get("started") or now)) * 1000.0
    path = str(row.get("path") or "")
    if elapsed_ms >= _STALL_WARN_MS or path in ("/", "/api/videos", "/api/tree"):
        emit(
            "INFO" if elapsed_ms < _STALL_WARN_MS else "WARN",
            "http_request_end",
            force=True,
            request_id=rid,
            operation_id=row.get("operation_id") or "",
            method=row.get("method") or "",
            path=path,
            status=status if status is not None else "",
            elapsed_ms=f"{elapsed_ms:.1f}",
            thread=row.get("thread") or "",
            inflight=inflight_n,
        )


def inflight_request_snapshot(limit: int = 12) -> list[dict]:
    now = time.perf_counter()
    with _inflight_lock:
        rows = [
            {
                "request_id": row.get("request_id") or "",
                "method": row.get("method") or "",
                "path": row.get("path") or "",
                "thread": row.get("thread") or "",
                "operation_id": row.get("operation_id") or "",
                "elapsed_ms": round((now - float(row.get("started") or now)) * 1000.0, 1),
            }
            for row in _inflight_requests.values()
        ]
    rows.sort(key=lambda item: float(item.get("elapsed_ms") or 0.0), reverse=True)
    return rows[: max(1, int(limit))]


def _stall_watchdog_loop() -> None:
    while True:
        time.sleep(_STALL_POLL_S)
        try:
            now = time.perf_counter()
            with _inflight_lock:
                begin_age_ms = (
                    (now - _last_request_begin) * 1000.0 if _last_request_begin else -1.0
                )
                end_age_ms = (now - _last_request_end) * 1000.0 if _last_request_end else -1.0
                inflight = [
                    {
                        "request_id": row.get("request_id") or "",
                        "method": row.get("method") or "",
                        "path": row.get("path") or "",
                        "thread": row.get("thread") or "",
                        "elapsed_ms": round(
                            (now - float(row.get("started") or now)) * 1000.0, 1
                        ),
                    }
                    for row in _inflight_requests.values()
                ]
            stuck = [
                row
                for row in inflight
                if float(row.get("elapsed_ms") or 0.0) >= _STALL_WARN_MS
            ]
            if not stuck and not (
                inflight and end_age_ms >= _STALL_WARN_MS and begin_age_ms >= _STALL_WARN_MS
            ):
                continue
            scan_bits = {}
            try:
                from vg.state import STATE, scan_lock_status, thumb_bulk_roots

                scan_bits = {
                    "scanning": bool(STATE.get("scanning")),
                    "updating": bool(STATE.get("updating")),
                    "scan_root": STATE.get("scan_root") or STATE.get("root") or "",
                    "lib_gen": STATE.get("lib_gen") or 0,
                    "thumb_bulk": ",".join(thumb_bulk_roots()[:6]),
                    **scan_lock_status(),
                }
            except Exception:
                scan_bits = {}
            sample = " | ".join(
                f"{row.get('method')}:{row.get('path')}@{row.get('thread')}:{row.get('elapsed_ms')}ms"
                for row in (stuck or inflight)[:8]
            )
            emit(
                "WARN",
                "http_server_stall_suspect",
                force=True,
                inflight=len(inflight),
                stuck=len(stuck),
                begin_age_ms=f"{begin_age_ms:.0f}",
                end_age_ms=f"{end_age_ms:.0f}",
                locks=lock_holders_summary(),
                sample=sample,
                **scan_bits,
            )
        except Exception:
            continue


def ensure_stall_watchdog() -> None:
    """Start once: reports wedged waitress/Flask request threads."""
    global _stall_watchdog_started
    if _stall_watchdog_started:
        return
    _stall_watchdog_started = True
    threading.Thread(
        target=_stall_watchdog_loop,
        daemon=True,
        name="http-stall-watchdog",
    ).start()
    emit("INFO", "http_stall_watchdog_started", force=True, poll_s=_STALL_POLL_S)


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
