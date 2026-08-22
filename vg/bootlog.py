# -*- coding: utf-8 -*-
"""Persistent startup / runtime log for diagnosing flash-exit on frozen builds.

每次启动在专用 ``logs/`` 目录下创建新文件，文件名形如
``startup-YYYYMMDD-HHMMSS-PID.log``，便于按时间归档与对比。

为兼容仍然按固定路径读取最新日志的脚本/工具，``init()`` 还会写一个
``logs/latest.txt``，第一行是当前会话日志文件的绝对路径。
"""
from __future__ import annotations

import os
import sys
import atexit
import threading
import traceback
from datetime import datetime
from pathlib import Path

_LOG_PATH: Path | None = None
_LOG_DIR: Path | None = None
_INIT = False
_WRITE_LOCK = threading.RLock()
_PENDING: list[str] = []
_FLUSH_EVENT = threading.Event()
_FLUSH_THREAD: threading.Thread | None = None
_STOP = False
# 保留最近 N 个历史日志文件，超出按 mtime 淘汰最旧。
_KEEP_LOG_FILES = 20
# 单文件大小上限（超出后尾部截断保留，避免无限涨）。
_MAX_LOG_BYTES = 4 * 1024 * 1024
_KEEP_LOG_BYTES = 2 * 1024 * 1024


def log_dir() -> Path:
    """返回日志目录（exe 旁或项目根下的 ``logs/``）。"""
    global _LOG_DIR
    if _LOG_DIR is not None:
        return _LOG_DIR
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent.parent
    _LOG_DIR = base / "logs"
    return _LOG_DIR


def log_path() -> Path:
    """当前会话日志文件路径。未 ``init`` 前返回目录下预生成的占位路径。"""
    if _LOG_PATH is not None:
        return _LOG_PATH
    # 占位：返回按时间戳构造的路径，但不会自动创建文件，
    # 真正创建发生在 ``init()`` 中。
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return log_dir() / f"startup-{stamp}-{os.getpid()}.log"


def _migrate_legacy_log() -> None:
    """把项目根 / exe 旁旧的 ``startup.log`` 搬进 ``logs/`` 目录。

    升级到分文件日志后，老的固定路径文件不再写入；如果它仍然存在，
    就改名归档到 ``logs/`` 下，避免用户去老路径找不到新日志。
    """
    if getattr(sys, "frozen", False):
        legacy = Path(sys.executable).resolve().parent / "startup.log"
    else:
        legacy = Path(__file__).resolve().parent.parent / "startup.log"
    try:
        if not legacy.exists() or legacy.stat().st_size == 0:
            return
    except OSError:
        return
    target_dir = log_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"startup-legacy-{stamp}.log"
    # 避免极小概率重名
    i = 1
    while target.exists():
        i += 1
        target = target_dir / f"startup-legacy-{stamp}-{i}.log"
    try:
        legacy.replace(target)
    except OSError:
        # 跨卷或权限问题：尝试读后写
        try:
            target.write_bytes(legacy.read_bytes())
            legacy.unlink()
        except Exception:
            pass


def _prune_old_logs() -> None:
    """保留最近 ``_KEEP_LOG_FILES`` 个会话日志，按 mtime 淘汰最旧。"""
    try:
        d = log_dir()
        if not d.is_dir():
            return
        files = [p for p in d.glob("startup-*.log") if p.is_file()]
        if len(files) <= _KEEP_LOG_FILES:
            return
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[_KEEP_LOG_FILES:]:
            try:
                p.unlink()
            except OSError:
                pass
    except Exception:
        pass


def _write_latest_pointer() -> None:
    """在 ``logs/latest.txt`` 写当前会话路径，便于脚本定位最新日志。"""
    try:
        ptr = log_dir() / "latest.txt"
        ptr.write_text(f"{_LOG_PATH}\n", encoding="utf-8")
    except Exception:
        pass


def init(reset: bool = False) -> Path:
    """初始化日志：每次启动在 ``logs/`` 下创建新文件。"""
    global _INIT, _LOG_PATH
    if _INIT and _LOG_PATH is not None:
        return _LOG_PATH
    try:
        d = log_dir()
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        d = Path(os.getcwd()) / "logs"
        d.mkdir(parents=True, exist_ok=True)
    # 迁移老 startup.log（如果存在）
    _migrate_legacy_log()
    # 为本会话创建新文件
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    _LOG_PATH = d / f"startup-{stamp}-{os.getpid()}.log"
    # 极小概率重名（同秒+同pid）—— 加序号
    i = 1
    while _LOG_PATH.exists():
        i += 1
        _LOG_PATH = d / f"startup-{stamp}-{os.getpid()}-{i}.log"
    try:
        _LOG_PATH.write_text("", encoding="utf-8")
    except Exception:
        pass
    _write_latest_pointer()
    _prune_old_logs()
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
    write(f"log_file={_LOG_PATH}", urgent=True)
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        write(f"_MEIPASS={meipass!r}", urgent=True)
        internal = Path(sys.executable).resolve().parent / "_internal"
        write(f"_internal_exists={internal.is_dir()}", urgent=True)
    return _LOG_PATH


def _append_lines(lines: list[str], *, sync: bool) -> None:
    if not lines:
        return
    if _LOG_PATH is None:
        init(reset=False)
    with _WRITE_LOCK:
        with _LOG_PATH.open("a", encoding="utf-8", errors="replace") as f:
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
