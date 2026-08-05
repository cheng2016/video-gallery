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
}
_scan_lock = threading.Lock()
_convert_lock = threading.Lock()
_meta_lock = threading.Lock()
_meta_running = False
_thumb_jpeg_cache: OrderedDict[str, bytes] = OrderedDict()
_thumb_jpeg_lock = threading.Lock()

