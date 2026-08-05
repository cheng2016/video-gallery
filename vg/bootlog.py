# -*- coding: utf-8 -*-
"""Persistent startup / runtime log for diagnosing flash-exit on frozen builds."""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

_LOG_PATH: Path | None = None
_INIT = False


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
                if path.stat().st_size > 512_000:
                    tail = path.read_text(encoding="utf-8", errors="replace")[-200_000:]
                    path.write_text("…(truncated)…\n" + tail, encoding="utf-8")
            except Exception:
                pass
        _INIT = True
        write("")
        write("======== session ========")
        write(f"time={datetime.now().isoformat(timespec='seconds')}")
        write(f"frozen={bool(getattr(sys, 'frozen', False))}")
        write(f"exe={sys.executable!r}")
        write(f"argv={sys.argv!r}")
        write(f"cwd={os.getcwd()!r}")
        write(f"pid={os.getpid()}")
        write(f"python={sys.version.split()[0]} platform={sys.platform}")
        if getattr(sys, "frozen", False):
            meipass = getattr(sys, "_MEIPASS", None)
            write(f"_MEIPASS={meipass!r}")
            internal = Path(sys.executable).resolve().parent / "_internal"
            write(f"_internal_exists={internal.is_dir()}")
    except Exception:
        pass
    return path


def write(msg: str) -> None:
    """追加一行并立即 flush（崩溃前尽量落盘）。"""
    try:
        if not _INIT:
            init(reset=False)
        line = str(msg).rstrip("\n")
        ts = datetime.now().strftime("%H:%M:%S")
        with log_path().open("a", encoding="utf-8", errors="replace") as f:
            f.write(f"[{ts}] {line}\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
    except Exception:
        pass


def step(name: str, detail: str = "") -> None:
    if detail:
        write(f"STEP {name}: {detail}")
    else:
        write(f"STEP {name}")


def fail(msg: str, detail: str = "") -> None:
    write(f"FAIL: {msg}")
    if detail:
        for line in str(detail).splitlines() or [detail]:
            write(f"  {line}")


def exception(prefix: str = "EXCEPTION") -> None:
    write(f"{prefix}:")
    for line in traceback.format_exc().splitlines():
        write(f"  {line}")
