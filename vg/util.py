# -*- coding: utf-8 -*-
"""Logging and path/size helpers."""
from __future__ import annotations

import sys


import hashlib
import os
import re
from pathlib import Path

from vg.config import (
    MIN_SEGMENT_FILE_BYTES,
    MIN_VIDEO_FILE_BYTES,
    PLAYLIST_EXTS,
    SEGMENT_EXTS,
    SKIP_DIR_NAMES,
    THUMB_WORKERS_MAX,
)
from vg.state import STATE

def log(msg: str) -> None:
    """CMD 窗口可见日志（立即刷新），并写入 startup.log。"""
    try:
        print(msg, flush=True)
    except Exception:
        pass
    try:
        from vg import bootlog
        bootlog.write(msg)
    except Exception:
        pass


def thumb_worker_count(total: int = 0) -> int:
    cpus = os.cpu_count() or 4
    n = max(2, min(THUMB_WORKERS_MAX, cpus))
    if total > 0:
        n = max(1, min(n, total))
    return n

def _fmt_bytes(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024:
            return f"{x:.1f} {unit}" if unit != "B" else f"{int(x)} B"
        x /= 1024
    return f"{x:.1f} PB"


def safe_rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def resolve_under_root(rel: str, root: Path | None = None) -> Path | None:
    """解析 root 下任意文件（含 m3u8 分片），防止路径穿越。"""
    from vg.disk_libs import resolve_under_root_path

    use_root = root if root is not None else STATE["root"]
    if use_root is None:
        return None
    return resolve_under_root_path(Path(use_root) if not isinstance(use_root, Path) else use_root, rel)


def resolve_video_path(rel: str, root: Path | None = None) -> Path | None:
    """把相对路径解析到 root 下，防止路径穿越。"""
    return resolve_under_root(rel, root=root)


def video_id(rel: str) -> str:
    return hashlib.md5(rel.encode("utf-8")).hexdigest()[:16]


def natural_sort_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s or "")]


def should_skip_dir(name: str) -> bool:
    return name.startswith(".") or name.lower() in SKIP_DIR_NAMES


def is_too_small_video(ext: str, size: int) -> bool:
    """过小的假/损坏视频文件（m3u8 除外）。"""
    ext = (ext or "").lower()
    if ext in PLAYLIST_EXTS:
        return False
    min_bytes = MIN_SEGMENT_FILE_BYTES if ext in SEGMENT_EXTS else MIN_VIDEO_FILE_BYTES
    return int(size or 0) < min_bytes


def _clear_path_attrs_windows(path: Path) -> None:
    """去掉 Hidden/System，否则 Windows 上覆盖写入常报 PermissionError (errno 13)。"""
    if sys.platform != "win32":
        return
    try:
        if not path.exists():
            return
        import ctypes
        # FILE_ATTRIBUTE_NORMAL
        ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x80)
    except Exception:
        pass


def _hide_path_windows(path: Path) -> None:
    """仅标记隐藏，不再加 System（System 会导致无法覆盖写入）。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x2)  # FILE_ATTRIBUTE_HIDDEN
    except Exception:
        pass

def format_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


def format_duration(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return ""
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"

