# -*- coding: utf-8 -*-
"""Shared runtime state and locks."""
from __future__ import annotations

import threading
from collections import OrderedDict

STATE: dict = {
    "root": None,
    "cache_dir": None,
    "tree": {"name": "全部", "path": "", "children": [], "videos": []},
    "videos": [],  # flat list
    "by_category": {},  # cat -> list[video]
    "by_id": {},
    "by_thumb_id": {},
    "facets": None,  # 预计算的 types/genres/categories
    "scanning": False,
    "scan_progress": "",
    "thumb_progress": "",
    "meta_progress": "",
    "ffmpeg": None,
    "updating": False,  # 后台增量中
    "exporting": False,
    "export_ok": None,
    "export_msg": "",
    "export_path": "",
    "convert_jobs": {},  # job_id -> job dict
    "bind_host": "127.0.0.1",
    "bind_port": 8765,
    "lan_share": False,
    "disk_libs": {},  # root_key -> {root, cache_dir, by_id, updated} 跨盘历史/续播
    "mounted_roots": [],  # 多根目录：统一片库挂载列表
    "convert_parallel": 1,  # 转码并发上限
    "lib_gen": 0,  # 片库世代：扫描中途/合并后递增，供轻量轮询
    "scan_root": "",  # 正在扫描的盘根路径
    "scan_live": None,  # 扫描中途已发现条目（不覆盖其它盘 STATE）
}
_scan_lock = threading.Lock()
_convert_lock = threading.Lock()
_meta_lock = threading.Lock()
_meta_running = False
_thumb_jpeg_cache: OrderedDict[str, bytes] = OrderedDict()
_thumb_jpeg_lock = threading.Lock()
_thumb_jpeg_cache_bytes = 0

# Filtered/faceted/sorted /api/videos results; offset/limit are deliberately
# excluded from the key so infinite-scroll pages share one computed list.
_video_query_cache: OrderedDict[tuple, tuple] = OrderedDict()
_video_query_cache_lock = threading.RLock()
VIDEO_QUERY_CACHE_MAX = 32
VIDEO_QUERY_CACHE_MAX_ITEMS = 100_000


def video_query_cache_get(key: tuple):
    with _video_query_cache_lock:
        value = _video_query_cache.get(key)
        if value is not None:
            _video_query_cache.move_to_end(key)
        return value


def video_query_cache_put(key: tuple, value: tuple) -> None:
    try:
        result_rows = value[0]
        if len(result_rows) > VIDEO_QUERY_CACHE_MAX_ITEMS:
            return
    except (TypeError, IndexError):
        pass
    with _video_query_cache_lock:
        _video_query_cache[key] = value
        _video_query_cache.move_to_end(key)
        while len(_video_query_cache) > VIDEO_QUERY_CACHE_MAX:
            _video_query_cache.popitem(last=False)


def invalidate_query_caches() -> None:
    with _video_query_cache_lock:
        _video_query_cache.clear()

# Expensive /api/videos query results are keyed by catalog generation.  Keep
# the cache beside STATE so catalog writers can invalidate it without importing
# the web module (which would create an import cycle).
