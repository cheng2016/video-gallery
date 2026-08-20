# -*- coding: utf-8 -*-
"""Persistent startup / runtime log for diagnosing flash-exit on frozen builds."""
from __future__ import annotations

import os
import sys
import atexit
import threading
import traceback
from datetime import datetime
from pathlib import Path

_LOG_PATH: Path | None = None
_INIT = False
_WRITE_LOCK = threading.RLock()
_PENDING: list[str] = []
_FLUSH_EVENT = threading.Event()
_FLUSH_THREAD: threading.Thread | None = None
_STOP = False
_MAX_LOG_BYTES = 4 * 1024 * 1024
_KEEP_LOG_BYTES = 2 * 1024 * 1024


def log_path() -> Path:
    """exe 旁 startup.log（frozen）；源码模式写到项目根。"""
    global _LOG_PATH
    if _LOG_PATH is not None:
        return _LOG_PATH
    if getattr(sys, "frozen", False):
        _LOG_PATH = Path(sys.executable).resolve().parent / "startup.log"
    else:
        _LOG_PATH = Path(__file__).resolve().parent.parent / "startup.log"
    return _LOG_PATH


def init(reset: bool = False) -> Path:
    """初始化日志。默认追加新会话（不抹掉上次闪退记录）；reset=True 才截断。"""
    global _INIT
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if reset:
            path.write_text("", encoding="utf-8")
        elif not path.exists():
            path.write_text("", encoding="utf-8")
        else:
            # 体积过大时只保留尾部，避免无限涨
            try:
                if path.stat().st_size > _MAX_LOG_BYTES:
                    tail = path.read_text(encoding="utf-8", errors="replace")[-_KEEP_LOG_BYTES:]
                    path.write_text("…(truncated)…\n" + tail, encoding="utf-8")
            except Exception:
                pass
        _INIT = True
        _ensure_flush_thread()
        write("", urgent=True)
        write("======== session ========", urgent=True)
        write(f"time={datetime.now().isoformat(timespec='seconds')}", urgent=True)
        write(f"frozen={bool(getattr(sys, 'frozen', False))}", urgent=True)
        write(f"exe={sys.executable!r}", urgent=True)
        write(f"argv={sys.argv!r}", urgent=True)
        write(f"cwd={os.getcwd()!r}", urgent=True)
        write(f"pid={os.getpid()}", urgent=True)
        write(f"python={sys.version.split()[0]} platform={sys.platform}", urgent=True)
        if getattr(sys, "frozen", False):
            meipass = getattr(sys, "_MEIPASS", None)
            write(f"_MEIPASS={meipass!r}", urgent=True)
            internal = Path(sys.executable).resolve().parent / "_internal"
            write(f"_internal_exists={internal.is_dir()}", urgent=True)
    except Exception:
        pass
    return path


def _append_lines(lines: list[str], *, sync: bool) -> None:
    if not lines:
        return
    with _WRITE_LOCK:
        with log_path().open("a", encoding="utf-8", errors="replace") as f:
            f.write("".join(lines))
            f.flush()
            if sync:
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass


def flush(*, sync: bool = False) -> None:
    with _WRITE_LOCK:
        if not _PENDING:
            return
        lines = list(_PENDING)
        _PENDING.clear()
    try:
        _append_lines(lines, sync=sync)
    except Exception:
        pass


def _flush_worker() -> None:
    while not _STOP:
        _FLUSH_EVENT.wait(0.5)
        _FLUSH_EVENT.clear()
        flush(sync=False)
    flush(sync=True)


def _ensure_flush_thread() -> None:
    global _FLUSH_THREAD
    if _FLUSH_THREAD and _FLUSH_THREAD.is_alive():
        return
    with _WRITE_LOCK:
        if _FLUSH_THREAD and _FLUSH_THREAD.is_alive():
            return
        _FLUSH_THREAD = threading.Thread(
            target=_flush_worker,
            daemon=True,
            name="runtime-log-writer",
        )
        _FLUSH_THREAD.start()


def write(msg: str, *, urgent: bool = False) -> None:
    """Queue normal lines; errors/startup markers are durably flushed now."""
    try:
        if not _INIT:
            init(reset=False)
        line = str(msg).rstrip("\n")
        ts = datetime.now().strftime("%H:%M:%S")
        formatted = f"[{ts}] {line}\n"
        if urgent:
            flush(sync=False)
            _append_lines([formatted], sync=True)
            return
        with _WRITE_LOCK:
            _PENDING.append(formatted)
            should_flush = len(_PENDING) >= 64
        _ensure_flush_thread()
        if should_flush:
            _FLUSH_EVENT.set()
    except Exception:
        pass


def shutdown() -> None:
    global _STOP
    _STOP = True
    _FLUSH_EVENT.set()
    flush(sync=True)


atexit.register(shutdown)


def step(name: str, detail: str = "") -> None:
    if detail:
        write(f"STEP {name}: {detail}")
    else:
        write(f"STEP {name}")


def fail(msg: str, detail: str = "") -> None:
    write(f"FAIL: {msg}", urgent=True)
    if detail:
        for line in str(detail).splitlines() or [detail]:
            write(f"  {line}", urgent=True)


def exception(prefix: str = "EXCEPTION") -> None:
    write(f"{prefix}:", urgent=True)
    for line in traceback.format_exc().splitlines():
        write(f"  {line}", urgent=True)
