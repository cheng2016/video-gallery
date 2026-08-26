# -*- coding: utf-8 -*-
"""Flask application and HTTP routes."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from collections import OrderedDict


import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from functools import wraps
from pathlib import Path
from urllib.parse import quote

try:
    from flask import Flask, Response, abort, g, jsonify, render_template, request, send_file
    from werkzeug.exceptions import HTTPException
except ImportError:
    print("=" * 50)
    print("【错误】未安装依赖 Flask")
    print("请在本目录运行:")
    if sys.platform == "win32":
        print(r"  .venv\Scripts\pip.exe install -r requirements.txt")
        print("或重新双击 start.bat")
    else:
        print("  .venv/bin/pip install -r requirements.txt")
        print("或重新运行 ./start.sh")
    print("=" * 50)
    input("按回车键退出…")
    sys.exit(1)

from vg.cache import (
    attach_thumb_meta,
    ensure_cache_dir,
    read_thumb_jpeg,
    thumb_cache_invalidate,
    thumb_path,
    thumb_version,
)
from vg.catalog import (
    build_category_facets,
    rebuild_indexes,
    video_category as _video_category,
    video_search_text as _video_search_text,
)
from vg.catalog_repository import find_video_by_id
from vg.duplicates import video_identity
from vg.config import (
    APP_DIR,
    BROWSER_FRIENDLY_EXTS,
    BROWSER_HARD_EXTS,
    GENRE_DEFS,
    PROBE_META_VER,
)
from vg.disk_libs import (
    cache_dir_for_item,
    ensure_library,
    offline_roots,
    read_root_library,
    resolve_item_rel,
    save_library_item,
    save_root_library,
)
from vg.drives import list_drives_info
from vg.genres import ensure_video_genres
from vg.http_helpers import filter_videos_by_scope, resolve_local_path
from vg.lan_service import lan_urls
from vg.privacy import (
    privacy_snapshot,
    probe_audio_enabled,
    probe_duration_enabled,
)
from vg.media import (
    _apply_probe_to_item,
    _item_probe_path,
    _needs_metadata_probe,
    _video_file_for_thumb,
    make_thumbnail,
    probe_media_info,
    save_thumbnail_jpeg,
)
from vg.roots import (
    filter_videos_by_lib,
    get_mounted_roots,
    publish_unified_library,
    root_label,
    roots_summary,
    thumb_id_for_item,
    tree_for_scope,
    videos_for_scope,
)
from vg.scan import start_scan
from vg.search import parse_search_query, video_matches_query
from vg.series import collapse_to_series_cards, series_episodes
from vg.state import STATE, video_query_cache_get, video_query_cache_put
from vg.taxonomy import ensure_video_taxonomy, taxonomy_facets, taxonomy_facets_pair
from vg.thumb_jobs import (
    THUMB_PRIORITY_VISIBLE,
    note_frontend_activity,
    submit_thumbnail_job,
    thumbnail_job_key,
)
from vg.thumbs import (
    clear_thumbnail_failure,
    mark_thumbnail_failure,
    thumbnail_failure_is_current,
)
from vg.streaming import _stream_file, rewrite_m3u8_for_proxy
from vg.trash import move_to_trash
from vg.util import (
    _clear_path_attrs_windows,
    format_size,
    log,
    resolve_under_root,
    resolve_video_path,
)
from vg.diagnostics import (
    aggregate as diagnostic_aggregate,
    begin_request_trace as diagnostic_begin_request_trace,
    call as diagnostic_call,
    emit as diagnostic_emit,
    emit_rate_limited as diagnostic_emit_rate_limited,
    end_request_trace as diagnostic_end_request_trace,
    ensure_stall_watchdog as diagnostic_ensure_stall_watchdog,
    error as diagnostic_error,
    full_logging_enabled as diagnostic_full_logging_enabled,
    perf as diagnostic_perf,
    request_id as diagnostic_request_id,
    timed_lock as diagnostic_timed_lock,
)

app = Flask(__name__, template_folder=str(APP_DIR / "templates"))
_delete_lock = threading.RLock()
_video_response_cache: OrderedDict[tuple, tuple[bytes, int, str]] = OrderedDict()
_video_response_cache_lock = threading.RLock()
_VIDEO_RESPONSE_CACHE_MAX = 32
_VIDEO_RESPONSE_CACHE_MAX_BYTES = 32 * 1024 * 1024
_video_response_cache_bytes = 0
_tree_payload_cache: OrderedDict[tuple, dict] = OrderedDict()
_tree_payload_cache_lock = threading.RLock()
_TREE_PAYLOAD_CACHE_MAX = 16
# Keep the stable-state browser cache long enough to cover rapid tag
# switching, while remaining short enough that an external catalog change is
# not hidden for long.  Scanning/updating and generation mismatches remain
# no-store regardless of this value.
_API_BROWSER_CACHE_MAX_AGE = 6


def _set_request_cache_layer(layer: str, **fields) -> None:
    """Record the server-side cache layer selected for the current request.

    The browser may satisfy an image request without contacting Flask at all;
    that case cannot be observed here.  For requests that do reach the app,
    this marker lets the after-request logger and ``X-VG-Cache-Layer`` header
    identify the exact server-side path (memory, disk, or rebuild/query).
    """
    try:
        g._vg_cache_layer = str(layer or "unknown")
        g._vg_cache_fields = dict(fields)
    except (AttributeError, RuntimeError):
        # Called from a test/helper without an active Flask request.
        return

# Duplicate badges are derived runtime fields and are intentionally removed
# before a row is persisted to SQLite. The SQL paging path therefore needs a
# small, generation-scoped lookup to restore those fields without scanning or
# re-running duplicate detection for every page request.
_runtime_duplicate_index_lock = threading.RLock()
_runtime_duplicate_index_key: tuple[int, int, int] | None = None
_runtime_duplicate_index: dict[str, dict[str, object]] = {}
_runtime_duplicate_video_count = 0


def _video_location_identity(video: dict) -> str:
    """Return a root/relative-path key for persisted-vs-runtime ID changes."""
    root = (video.get("_lib_root") or video.get("root") or "").strip().replace("/", "\\").rstrip("\\").casefold()
    rel = (video.get("rel") or "").replace("\\", "/").strip("/").casefold()
    if not root or not rel:
        return ""
    return f"{root}|{rel}"


def _runtime_duplicate_fields_index() -> tuple[dict[str, dict[str, object]], int, int]:
    """Return ``identity -> duplicate fields`` for the current catalog."""
    global _runtime_duplicate_index_key
    global _runtime_duplicate_index
    global _runtime_duplicate_video_count

    videos = STATE.get("videos") or []
    key = (id(videos), int(STATE.get("lib_gen") or 0), len(videos))
    with _runtime_duplicate_index_lock:
        if key != _runtime_duplicate_index_key:
            index: dict[str, dict[str, object]] = {}
            marked_count = 0
            for video in videos:
                if not video.get("dup"):
                    continue
                identity = video_identity(video)
                if not identity:
                    continue
                fields = {
                    "dup": True,
                    "dup_n": int(video.get("dup_n") or 0),
                    "dup_reason": str(video.get("dup_reason") or "重复"),
                }
                index[identity] = fields
                location_identity = _video_location_identity(video)
                if location_identity:
                    # A unified multi-disk catalog may intentionally rewrite a
                    # colliding runtime id while SQLite retains the source id.
                    # The owning root + relative path remains stable.
                    index.setdefault(location_identity, fields)
                marked_count += 1
            _runtime_duplicate_index_key = key
            _runtime_duplicate_index = index
            _runtime_duplicate_video_count = marked_count
        return (
            _runtime_duplicate_index,
            len(videos),
            _runtime_duplicate_video_count,
        )


@app.errorhandler(Exception)
def _log_unhandled_exception(exc):
    if isinstance(exc, HTTPException):
        return exc
    diagnostic_error(
        "http_unhandled_exception",
        exc,
        request_id=getattr(g, "_diag_request_id", ""),
        method=request.method,
        path=request.path,
    )
    if (request.path or "").startswith("/api/"):
        return jsonify({"ok": False, "msg": "服务器内部错误"}), 500
    return Response("服务器内部错误", status=500, mimetype="text/plain")


def _stable_request_args() -> tuple:
    """Query args that identify a page; ignore cache-buster `_`."""
    return tuple(
        sorted(
            (key, tuple(values))
            for key, values in request.args.lists()
            if key not in ("_", "op")
        )
    )


def _video_response_cache_key() -> tuple:
    """Identify one paged query within one immutable catalog generation."""
    existing = getattr(g, "_video_response_cache_key", None)
    if existing is not None:
        return existing
    try:
        _off = max(0, int(request.args.get("offset", 0) or 0))
    except ValueError:
        _off = 0
    try:
        _lim_raw = request.args.get("limit", 60) or 60
        _lim = max(1, min(200, int(_lim_raw)))
    except ValueError:
        _lim = 60
    return (
        int(STATE.get("lib_gen") or 0),
        id(STATE.get("videos")),
        _stable_request_args(),
        # 分页参数必须单独入 key；否则 loadMore offset=20 的结果会覆盖
        # 首屏 offset=0 缓存，导致筛选栏（types/genres 等）被意外清空。
        _off,
        _lim,
    )


def _cached_video_response(key: tuple):
    with diagnostic_timed_lock(
        _video_response_cache_lock,
        "video_response_cache_read",
    ):
        value = _video_response_cache.get(key)
        if value is not None:
            _video_response_cache.move_to_end(key)
        return value


def _store_video_response(key: tuple, response: Response) -> None:
    global _video_response_cache_bytes
    if response.status_code != 200:
        return
    value = (response.get_data(), response.status_code, response.mimetype)
    size = len(value[0])
    if size > _VIDEO_RESPONSE_CACHE_MAX_BYTES // 2:
        diagnostic_emit(
            "WARN",
            "api_videos_response_cache_skip",
            force=True,
            reason="single_response_too_large",
            bytes=size,
            limit=_VIDEO_RESPONSE_CACHE_MAX_BYTES // 2,
        )
        return
    _off = getattr(g, "_video_response_cache_off", None)
    _lim = getattr(g, "_video_response_cache_lim", None)
    try:
        sample = json.loads(value[0]) if value[0] else {}
    except Exception:
        sample = {}
    with diagnostic_timed_lock(
        _video_response_cache_lock,
        "video_response_cache_write",
    ):
        if not _video_response_cache:
            _video_response_cache_bytes = 0
        old = _video_response_cache.pop(key, None)
        if old:
            _video_response_cache_bytes -= len(old[0])
        _video_response_cache[key] = value
        _video_response_cache_bytes += size
        _video_response_cache.move_to_end(key)
        while (
            len(_video_response_cache) > _VIDEO_RESPONSE_CACHE_MAX
            or _video_response_cache_bytes > _VIDEO_RESPONSE_CACHE_MAX_BYTES
        ):
            _, removed = _video_response_cache.popitem(last=False)
            _video_response_cache_bytes -= len(removed[0])
    # 每次新写入都留痕：方便下次排查「响应缓存返回了错误内容」时直接对比
    # key 里的 offset/limit 与实际响应体是否一致、以及 facets 数量是否合理。
    # offset=0 首屏写入强制打日志；offset>0 的翻页写入通常 types_len=0，
    # 为避免刷屏仅 force=False；任何 mismatch 都升 WARN。
    res_offset = sample.get("offset")
    res_types_len = (
        len(sample.get("types") or [])
        if isinstance(sample.get("types"), list)
        else -1
    )
    mismatch = (
        (res_offset is not None and res_offset != _off)
        or (_off == 0 and res_types_len == 0)
    )
    emit_force = bool(mismatch or _off == 0 or _lim >= 60)
    emit_level = "WARN" if mismatch else "INFO"
    diagnostic_emit(
        emit_level,
        "api_videos_response_cache_write_detail",
        force=emit_force,
        request_id=getattr(g, "_diag_request_id", ""),
        offset=_off,
        limit=_lim,
        res_offset=res_offset,
        res_rows=len(sample.get("videos") or []) if isinstance(sample.get("videos"), list) else -1,
        res_count=sample.get("count"),
        res_types_len=res_types_len,
        res_genres_len=len(sample.get("genres") or []) if isinstance(sample.get("genres"), list) else -1,
        res_subfolders_len=len(sample.get("subfolders") or []) if isinstance(sample.get("subfolders"), list) else -1,
        offset_match=(res_offset is None or res_offset == _off),
        zero_types_ok=(not _off == 0 or res_types_len > 0),
        cached_body_bytes=size,
        cache_entries=len(_video_response_cache),
        cache_bytes_total=_video_response_cache_bytes,
    )


def invalidate_response_caches() -> None:
    global _video_response_cache_bytes
    with diagnostic_timed_lock(
        _video_response_cache_lock,
        "video_response_cache_invalidate",
    ):
        _video_response_cache.clear()
        _video_response_cache_bytes = 0
    with diagnostic_timed_lock(
        _tree_payload_cache_lock,
        "tree_payload_cache_invalidate",
    ):
        _tree_payload_cache.clear()
    global _warm_generation
    _warm_generation = None


_warm_generation = None
_warm_lock = threading.Lock()
_warming = False
_warm_enabled = False


# L1 warmup stays off in production: real UI requests often carry prefs
# (e.g. ext=.mp4) that warm URLs never match, so it only steals startup
# CPU/disk. Tests may still enable/call warm_response_caches() directly.
_WARM_DELAY_MS = 1200


def enable_response_cache_warm(*, schedule: bool = True) -> None:
    """Opt-in L1 warmup for tests/experiments. Production never enables this."""
    global _warm_enabled
    _warm_enabled = True
    diagnostic_emit(
        "INFO",
        "response_cache_warm_enabled",
        force=True,
        lib_gen=int(STATE.get("lib_gen") or 0),
        videos=len(STATE.get("videos") or []),
        schedule=schedule,
    )
    if schedule:
        schedule_response_cache_warm()


def schedule_response_cache_warm() -> None:
    """No-op unless enable_response_cache_warm() was called (tests only)."""
    if not _warm_enabled:
        return
    gen = int(STATE.get("lib_gen") or 0)
    videos_id = id(STATE.get("videos"))
    delay_ms = int(_WARM_DELAY_MS)
    diagnostic_emit(
        "INFO",
        "response_cache_warm_scheduled",
        force=True,
        lib_gen=gen,
        videos=len(STATE.get("videos") or []),
        delay_ms=delay_ms,
    )

    def run() -> None:
        try:
            time.sleep(max(0.0, delay_ms / 1000.0))
            if STATE.get("scanning"):
                diagnostic_emit(
                    "INFO",
                    "response_cache_warm_skipped",
                    force=True,
                    reason="scanning",
                    scheduled_gen=gen,
                    current_gen=int(STATE.get("lib_gen") or 0),
                )
                return
            current_gen = int(STATE.get("lib_gen") or 0)
            if current_gen != gen or id(STATE.get("videos")) != videos_id:
                diagnostic_emit(
                    "INFO",
                    "response_cache_warm_skipped",
                    force=True,
                    reason="stale_snapshot",
                    scheduled_gen=gen,
                    current_gen=current_gen,
                    videos_identity_changed=id(STATE.get("videos")) != videos_id,
                )
                return
            warm_response_caches()
        except Exception as exc:
            diagnostic_error(
                "response_cache_warm_thread_failed",
                exc,
                scheduled_gen=gen,
            )

    try:
        threading.Thread(target=run, daemon=True, name="vg-warm-l1").start()
    except Exception as exc:
        diagnostic_error("response_cache_warm_thread_start_failed", exc, lib_gen=gen)


def warm_response_caches() -> None:
    """Pre-fill L1 list/tree responses for the default first page and category tags."""
    global _warm_generation, _warming
    gen = int(STATE.get("lib_gen") or 0)
    if gen <= 0 or STATE.get("scanning"):
        diagnostic_emit(
            "INFO",
            "response_cache_warm_skipped",
            force=diagnostic_full_logging_enabled(),
            reason="invalid_generation" if gen <= 0 else "scanning",
            lib_gen=gen,
        )
        return
    with _warm_lock:
        if _warming or _warm_generation == (gen, id(STATE.get("videos"))):
            diagnostic_emit(
                "INFO",
                "response_cache_warm_skipped",
                force=True,
                reason="already_running" if _warming else "already_warm",
                lib_gen=gen,
            )
            return
        _warming = True
    started = time.perf_counter()
    warmed = 0
    try:
        categories = []
        facets = STATE.get("facets") or {}
        for row in facets.get("categories") or []:
            name = str(row.get("id") or row.get("name") or "").strip()
            if name and name not in ("__root__", "__all__"):
                categories.append(name)
        for key in (STATE.get("by_category") or {}):
            name = str(key or "").strip()
            if name and name not in categories and name not in ("__root__", "__all__"):
                categories.append(name)
        queries = [
            f"/api/tree?gen={gen}",
            f"/api/videos?sort=mtime_desc&view=flat&gen={gen}&offset=0&limit=20",
        ]
        from urllib.parse import quote
        for name in categories[:12]:
            queries.append(
                f"/api/videos?sort=mtime_desc&view=flat&gen={gen}&offset=0&limit=20"
                f"&category={quote(name)}"
            )
        diagnostic_emit(
            "INFO",
            "response_cache_warm_begin",
            force=True,
            lib_gen=gen,
            queries=len(queries),
            categories=len(categories[:12]),
        )
        client = app.test_client()
        for path in queries:
            try:
                response = client.get(path)
                if response.status_code != 200:
                    diagnostic_emit(
                        "WARN",
                        "response_cache_warm_request_bad_status",
                        force=True,
                        path=path,
                        status=response.status_code,
                        lib_gen=gen,
                    )
                    continue
                warmed += 1
            except Exception as exc:
                diagnostic_error("response_cache_warm_request_failed", exc, path=path)
        _warm_generation = (gen, id(STATE.get("videos")))
        diagnostic_perf(
            "response_cache_warm",
            (time.perf_counter() - started) * 1000.0,
            force=True,
            queries=warmed,
            requested_queries=len(queries),
            failed_queries=len(queries) - warmed,
            categories=len(categories[:12]),
            lib_gen=gen,
        )
    except Exception as exc:
        diagnostic_error("response_cache_warm_failed", exc)
    finally:
        with _warm_lock:
            _warming = False


def _serialized(lock: threading.RLock):
    def decorate(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            with diagnostic_timed_lock(
                lock,
                f"http_serialized_{func.__name__}",
                path=request.path,
            ):
                return func(*args, **kwargs)

        return wrapped

    return decorate


def _client_ip() -> str:
    # 本机直连，不信任伪造的 X-Forwarded-For
    return (request.remote_addr or "").strip()


def _operation_id() -> str:
    raw = (
        request.headers.get("X-VG-Operation-ID")
        or request.args.get("op")
        or ""
    ).strip()
    return raw[:64] if re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", raw) else ""


@app.before_request
def _start_request_diagnostics():
    g._diag_started = time.perf_counter()
    g._diag_request_id = diagnostic_request_id()
    g._diag_operation_id = _operation_id()
    diagnostic_ensure_stall_watchdog()
    diagnostic_begin_request_trace(
        g._diag_request_id,
        method=request.method,
        path=request.path or "",
        operation_id=g._diag_operation_id,
    )
    return None


@app.teardown_request
def _end_request_diagnostics(exc):
    rid = getattr(g, "_diag_request_id", "")
    if not rid:
        return
    # after_request already ended most traces; this catches aborts/exceptions.
    if getattr(g, "_diag_trace_ended", False):
        return
    diagnostic_end_request_trace(rid, status=None)
    g._diag_trace_ended = True


@app.before_request
def _enforce_lan_share():
    """未开局域网时，拒绝非本机访问（服务始终监听 0.0.0.0，开关即时生效）。"""
    if STATE.get("lan_share"):
        return None
    from vg.lan import is_loopback_addr
    if is_loopback_addr(_client_ip()):
        return None
    msg = "当前仅允许本机访问。请在跑服务的电脑上打开网页，点顶栏「局域网」开启分享。"
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "msg": msg}), 403
    return Response(
        "<!doctype html><meta charset=utf-8><title>仅本机</title>"
        f"<body style='font-family:sans-serif;padding:2rem'><h1>仅本机可访问</h1><p>{msg}</p></body>",
        status=403,
        mimetype="text/html; charset=utf-8",
    )


@app.before_request
def _serve_cached_videos_response():
    if request.method != "GET" or request.path != "/api/videos":
        return None
    if not hasattr(g, "_diag_started"):
        g._diag_started = time.perf_counter()
        g._diag_request_id = diagnostic_request_id()
    # This hook can return before later before_request handlers run.
    try:
        _off = max(0, int(request.args.get("offset", 0) or 0))
    except ValueError:
        _off = 0
    try:
        _lim_raw = request.args.get("limit", 60) or 60
        _lim = max(1, min(200, int(_lim_raw)))
    except ValueError:
        _lim = 60
    note_frontend_activity(
        0.45,
        source=f"api_videos_cache_hook:offset={_off}",
    )
    key = (
        int(STATE.get("lib_gen") or 0),
        id(STATE.get("videos")),
        _stable_request_args(),
        # 分页参数必须单独入 key；否则 loadMore offset=20 的结果（facets/genres/types 空）
        # 会覆盖 offset=0 首屏缓存，下次 polling refresh 命中后把筛选栏清空，
        # 视觉上就是「滑动一会儿标签突然变了/没了」。
        _off,
        _lim,
    )
    g._video_response_cache_key = key
    g._video_response_cache_off = _off
    g._video_response_cache_lim = _lim
    cached = _cached_video_response(key)
    if cached is None:
        diagnostic_aggregate("api_videos_response_cache_miss")
        _set_request_cache_layer(
            "L3_server_query",
            cache="response_miss",
            offset=_off,
            limit=_lim,
        )
        diagnostic_emit(
            "INFO",
            "api_videos_cache_resolution",
            force=diagnostic_full_logging_enabled(),
            request_id=getattr(g, "_diag_request_id", ""),
            layer="L3_server_query",
            offset=_off,
            limit=_lim,
            lib_gen=int(STATE.get("lib_gen") or 0),
            response_cache_entries=len(_video_response_cache),
            response_cache_bytes=_video_response_cache_bytes,
        )
        return None
    diagnostic_aggregate("api_videos_response_cache_hit")
    _set_request_cache_layer(
        "L1_server_response_memory",
        cache="response_hit",
        offset=_off,
        limit=_lim,
    )
    body, status, mimetype = cached
    try:
        sample = json.loads(body) if body else {}
    except Exception:
        sample = {}
    res_offset = sample.get("offset")
    res_types_len = (
        len(sample.get("types") or [])
        if isinstance(sample.get("types"), list)
        else -1
    )
    # 正常命中不刷屏（force=False INFO）；但 offset=0 的首屏命中必须强制留痕，
    # 以及"请求的 offset/limit 与响应体落盘时记录的不一致"也是 cache key
    # 错乱的硬证据，打 WARN 级别告警。
    mismatch = (
        (res_offset is not None and res_offset != _off)
        or (_off == 0 and res_types_len == 0)
    )
    emit_force = bool(mismatch or diagnostic_full_logging_enabled())
    emit_level = "WARN" if mismatch else "INFO"
    diagnostic_emit(
        emit_level,
        "api_videos_response_cache_hit_detail",
        force=emit_force,
        request_id=getattr(g, "_diag_request_id", ""),
        offset=_off,
        limit=_lim,
        res_offset=res_offset,
        res_rows=len(sample.get("videos") or []) if isinstance(sample.get("videos"), list) else -1,
        res_count=sample.get("count"),
        res_types_len=res_types_len,
        res_genres_len=len(sample.get("genres") or []) if isinstance(sample.get("genres"), list) else -1,
        res_subfolders_len=len(sample.get("subfolders") or []) if isinstance(sample.get("subfolders"), list) else -1,
        offset_match=(res_offset is None or res_offset == _off),
        zero_types_ok=(not _off == 0 or res_types_len > 0),
        cached_body_bytes=len(body),
    )
    diagnostic_emit(
        "INFO",
        "api_videos_cache_resolution",
        force=diagnostic_full_logging_enabled(),
        request_id=getattr(g, "_diag_request_id", ""),
        layer="L1_server_response_memory",
        offset=_off,
        limit=_lim,
        response_rows=len(sample.get("videos") or []) if isinstance(sample.get("videos"), list) else -1,
    )
    return Response(body, status=status, mimetype=mimetype)


@app.before_request
def _let_foreground_requests_preempt_thumbnails():
    """Give list rendering and playback a quiet window between ffmpeg jobs."""
    if request.method != "GET":
        return None
    path = request.path or ""
    if path.startswith(("/stream/", "/playlist/", "/hls/")):
        note_frontend_activity(4.0, source=f"playback:{path.split('/',2)[-1][:40]}")
    elif path == "/":
        note_frontend_activity(0.45, source="home_page")
    elif path == "/api/tree":
        note_frontend_activity(0.45, source="api_tree")
    elif path == "/api/videos":
        try:
            _off = max(0, int(request.args.get("offset", 0) or 0))
        except ValueError:
            _off = 0
        note_frontend_activity(0.45, source=f"api_videos:offset={_off}")
    elif path == "/api/videos-by-ids":
        note_frontend_activity(0.45, source="api_videos_by_ids")
    elif path.startswith("/thumb/"):
        note_frontend_activity(0.45, source=f"thumb:{path.split('/',2)[-1][:40]}")
    elif path.startswith("/api/series/"):
        note_frontend_activity(0.45, source=f"api_series:{path.split('/',3)[-1][:40]}")
    return None


@app.after_request
def _log_request_diagnostics(resp):
    started = getattr(g, "_diag_started", None)
    if started is None:
        return resp
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    path = request.path or ""
    rid = getattr(g, "_diag_request_id", "")
    operation_id = getattr(g, "_diag_operation_id", "")
    diagnostic_end_request_trace(rid, status=resp.status_code)
    g._diag_trace_ended = True
    resp.headers["X-VG-Request-ID"] = rid
    if operation_id:
        resp.headers["X-VG-Operation-ID"] = operation_id
    cache_layer = getattr(g, "_vg_cache_layer", "")
    cache_fields = getattr(g, "_vg_cache_fields", {}) or {}
    if cache_layer:
        resp.headers["X-VG-Cache-Layer"] = str(cache_layer)
        expected_deferred_thumb = resp.status_code == 503 and path.startswith("/thumb/")
        diagnostic_emit(
            "INFO",
            "http_cache_resolution",
            force=(
                diagnostic_full_logging_enabled()
                or elapsed_ms >= 200.0
                or (resp.status_code >= 400 and not expected_deferred_thumb)
            ),
            request_id=rid,
            operation_id=operation_id,
            method=request.method,
            path=path,
            status=resp.status_code,
            layer=cache_layer,
            cache_control=resp.headers.get("Cache-Control", ""),
            elapsed_ms=f"{elapsed_ms:.1f}",
            **cache_fields,
        )
    deferred_thumb = resp.status_code == 503 and path.startswith("/thumb/")
    quiet_404 = resp.status_code == 404 and path in ("/favicon.ico", "/robots.txt")
    hot_path = (
        path.startswith(("/thumb/", "/stream/", "/hls/"))
        or path in ("/api/status", "/api/client-log", "/favicon.ico", "/robots.txt")
    )
    if resp.status_code >= 400 and not deferred_thumb and not quiet_404:
        diagnostic_emit(
            "ERROR",
            "http_request_failed",
            force=True,
            request_id=rid,
            operation_id=operation_id,
            method=request.method,
            path=path,
            status=resp.status_code,
            elapsed_ms=f"{elapsed_ms:.1f}",
        )
    elif quiet_404:
        pass
    elif hot_path:
        diagnostic_aggregate(
            "http_hot_path",
            elapsed_ms,
            failed=False,
        )
        # The aggregate keeps hot-path volume cheap, but used to hide which
        # endpoint was responsible for multi-second stalls.  Keep normal
        # requests aggregated and emit a detailed line only for slow ones.
        if elapsed_ms >= 1000.0:
            diagnostic_perf(
                "http_hot_path_slow",
                elapsed_ms,
                force=True,
                request_id=rid,
                operation_id=operation_id,
                method=request.method,
                path=path,
                status=resp.status_code,
            )
    elif elapsed_ms >= 200.0:
        diagnostic_perf(
            "http_request",
            elapsed_ms,
            force=True,
            request_id=rid,
            operation_id=operation_id,
            method=request.method,
            path=path,
            status=resp.status_code,
        )
    else:
        diagnostic_call(
            "http_request",
            request_id=rid,
            operation_id=operation_id,
            verb=request.method,
            path=path,
            status=resp.status_code,
            elapsed_ms=f"{elapsed_ms:.1f}",
        )
    return resp


@app.after_request
def _api_cache_policy(resp):
    """Apply a short, generation-aware browser cache to stable list/tree data.

    The frontend adds ``gen=<lib_gen>`` to these GET URLs.  A matching
    generation is safe to cache briefly while the library is stable; scans and
    updates remain ``no-store``.  The generation is also included in the ETag
    so an expired browser entry can be revalidated without sending the body.
    """
    path = request.path or ""
    if not path.startswith("/api/"):
        return resp

    cacheable_path = path in ("/api/videos", "/api/tree") and request.method == "GET"
    current_gen = int(STATE.get("lib_gen") or 0)
    requested_gen = request.args.get("gen", "").strip()
    try:
        generation_match = bool(requested_gen) and int(requested_gen) == current_gen
    except ValueError:
        generation_match = False
    stable = not bool(STATE.get("scanning")) and not bool(STATE.get("updating"))
    if cacheable_path and stable and generation_match and resp.status_code == 304:
        resp.headers["Cache-Control"] = (
            f"private, max-age={_API_BROWSER_CACHE_MAX_AGE}, must-revalidate"
        )
        resp.headers["X-VG-Cache-Policy"] = "private-short-generation"
        resp.headers.pop("Pragma", None)
        resp.headers.pop("Expires", None)
        browser_cache_enabled = True
    else:
        browser_cache_enabled = cacheable_path and stable and generation_match and resp.status_code == 200

        if browser_cache_enabled:
            body = resp.get_data()
            digest = hashlib.sha1(body).hexdigest()[:16]
            resp.set_etag(f"vg-{current_gen}-{digest}")
            resp.headers["Cache-Control"] = (
                f"private, max-age={_API_BROWSER_CACHE_MAX_AGE}, must-revalidate"
            )
            resp.headers["X-VG-Cache-Policy"] = "private-short-generation"
            resp.headers.pop("Pragma", None)
            resp.headers.pop("Expires", None)
            if request.if_none_match and request.if_none_match.contains(resp.get_etag()[0]):
                resp.status_code = 304
                resp.set_data(b"")
        else:
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
            if cacheable_path:
                resp.headers["X-VG-Cache-Policy"] = "no-store-generation-mismatch-or-busy"

    # Include the policy in the existing request-level cache-resolution line.
    try:
        cache_fields = getattr(g, "_vg_cache_fields", {}) or {}
        cache_fields.update(
            {
                "browser_cache": "enabled" if browser_cache_enabled else "disabled",
                "cache_generation": current_gen,
                "requested_generation": requested_gen or "missing",
                "library_stable": stable,
                "etag_revalidated": resp.status_code == 304,
            }
        )
        g._vg_cache_fields = cache_fields
    except (AttributeError, RuntimeError):
        pass
    return resp


@app.after_request
def _cache_videos_response(resp):
    if request.method == "GET" and request.path == "/api/videos":
        _store_video_response(_video_response_cache_key(), resp)
    return resp


@app.route("/")
def index():
    """返回页面；用字符串注入盘符 JSON，不依赖 Jinja，避免 {{ }} 原样展示。"""
    page_served = time.perf_counter()
    html_path = APP_DIR / "templates" / "index.html"
    html = html_path.read_text(encoding="utf-8")
    html_size = len(html)
    try:
        drives = list_drives_info()
        root = STATE["root"]
        current = str(root) if root else ""
        for d in drives:
            try:
                d["active"] = bool(current) and os.path.normcase(
                    os.path.abspath(d["path"])
                ) == os.path.normcase(os.path.abspath(current))
            except OSError:
                d["active"] = False
        payload = json.dumps(
            {
                "drives": drives,
                "current": current,
                "scanning": STATE["scanning"],
                "full_logging": diagnostic_full_logging_enabled(),
            },
            ensure_ascii=False,
        )
    except Exception as e:
        log(f"[错误] 页面注入盘符失败: {e}")
        payload = '{"drives":[],"current":"","scanning":false}'
    boot = f"<script>window.__BOOT_DRIVES__ = {payload};</script>"
    if "</head>" in html:
        html = html.replace("</head>", boot + "\n</head>", 1)
    else:
        html = boot + html
    _page_serve_ms = (time.perf_counter() - page_served) * 1000.0
    log(
        f"[页面] index.html 已发送: {html_size} 字节, "
        f"视频={len(STATE.get('videos', []))}, "
        f"扫描中={STATE.get('scanning')}, 耗时={_page_serve_ms:.1f}ms"
    )
    resp = Response(html, mimetype="text/html; charset=utf-8")
    _set_request_cache_layer(
        "L3_template_html",
        cache="no_store",
        source="templates/index.html",
    )
    # Prevent browser from caching the HTML — ensures JS changes are
    # picked up immediately on refresh without Ctrl+Shift+R.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


_CLIENT_LOG_CORE_EVENTS = {
    "refresh_render_completed",
    "load_more_done",
}


def _ingest_client_action(data: object) -> tuple[str, int] | None:
    """Validate and emit one browser event; return an HTTP error when rejected."""
    if not isinstance(data, dict):
        diagnostic_emit(
            "WARN",
            "client_log_rejected",
            force=True,
            reason="invalid_json_object",
        )
        return "日志格式错误", 400
    event = str(data.get("event") or "").strip()[:80]
    if not event or not re.fullmatch(r"[A-Za-z0-9_.:-]+", event):
        diagnostic_emit(
            "WARN",
            "client_log_rejected",
            force=True,
            reason="invalid_event_name",
            client_event=event,
        )
        return "事件名错误", 400
    level = str(data.get("level") or "INFO").upper()
    if level not in {"INFO", "WARN", "ERROR"}:
        level = "INFO"
    full_logging = diagnostic_full_logging_enabled()
    if level == "INFO" and not full_logging and event not in _CLIENT_LOG_CORE_EVENTS:
        diagnostic_aggregate("client_log_suppressed")
        return None
    raw_fields = data.get("fields")
    fields: dict[str, object] = {}
    # 布局诊断需要保留多行控件的完整矩形数据；普通客户端日志仍保持较小上限。
    field_value_limit = 4000 if event == "filter_layout" else 500
    if isinstance(raw_fields, dict):
        for key, value in list(raw_fields.items())[:24]:
            safe_key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(key))[:40]
            if not safe_key:
                continue
            if isinstance(value, bool) or value is None:
                fields[safe_key] = value
            elif isinstance(value, (int, float)):
                fields[safe_key] = value
            else:
                fields[safe_key] = str(value).replace("\r", " ").replace("\n", " ")[:field_value_limit]
    operation_id = str(data.get("operation_id") or _operation_id()).strip()[:64]
    if event.startswith("player_") and event not in {
        "player_error",
        "player_play_rejected",
        "player_opened",
        "player_closed",
        "player_source_set",
    }:
        diagnostic_aggregate("client_player_noise")
        return None
    # Client fields may reuse names already passed as kwargs (request_id, action).
    renamed = {
        "request_id": "related_request_id",
        "action": "target_action",
        "operation_id": "client_operation_id",
        "page": "client_page",
        "force": "client_force",
        "detail": "client_detail",
        "event": "client_event",
        "level": "client_level",
        "client_ip": "reported_client_ip",
    }
    for key, alias in renamed.items():
        if key in fields:
            fields[alias] = fields.pop(key)
    diagnostic_emit(
        level,
        "client_action",
        force=full_logging or level in {"WARN", "ERROR"} or event in _CLIENT_LOG_CORE_EVENTS,
        action=event,
        operation_id=operation_id,
        request_id=getattr(g, "_diag_request_id", ""),
        client_ip=_client_ip(),
        page=str(data.get("page") or "")[:200],
        **fields,
    )
    return None


@app.route("/api/client-log", methods=["POST"])
def api_client_log():
    """Receive browser diagnostics; INFO events may arrive in bounded batches."""
    ingest_started = time.perf_counter()
    data = request.get_json(silent=True)
    if isinstance(data, dict) and "events" in data:
        raw_events = data.get("events")
        if not isinstance(raw_events, list) or not raw_events or len(raw_events) > 32:
            diagnostic_emit(
                "WARN",
                "client_log_batch_rejected",
                force=True,
                reason="invalid_batch",
                batch_count=len(raw_events) if isinstance(raw_events, list) else -1,
            )
            return jsonify({"ok": False, "msg": "日志批次格式错误"}), 400
        events = raw_events
    else:
        events = [data]

    for item in events:
        rejected = _ingest_client_action(item)
        if rejected is not None:
            msg, status = rejected
            return jsonify({"ok": False, "msg": msg}), status
    diagnostic_perf(
        "client_log_ingest",
        (time.perf_counter() - ingest_started) * 1000.0,
        batch_count=len(events),
        client_event=(
            str(events[0].get("event") or "")[:80]
            if len(events) == 1 and isinstance(events[0], dict)
            else "batch"
        ),
        full_logging=diagnostic_full_logging_enabled(),
    )
    return jsonify({"ok": True})


def _tree_cache_key(lib: str) -> tuple:
    # Scanning/updating only overlay status fields at serialize time; the
    # heavy tree payload is keyed by catalog identity so a background
    # count (updating True→False) does not rebuild the tree.
    return (
        int(STATE.get("lib_gen") or 0),
        id(STATE.get("videos")),
        (lib or "").strip().casefold(),
    )


def _cached_tree_payload(key: tuple) -> dict | None:
    with diagnostic_timed_lock(
        _tree_payload_cache_lock,
        "tree_payload_cache_read",
    ):
        value = _tree_payload_cache.get(key)
        if value is not None:
            _tree_payload_cache.move_to_end(key)
        return value


def _store_tree_payload(key: tuple, payload: dict) -> None:
    with diagnostic_timed_lock(
        _tree_payload_cache_lock,
        "tree_payload_cache_write",
    ):
        _tree_payload_cache[key] = payload
        _tree_payload_cache.move_to_end(key)
        while len(_tree_payload_cache) > _TREE_PAYLOAD_CACHE_MAX:
            _tree_payload_cache.popitem(last=False)


def _build_tree_payload(lib: str) -> dict:
    started = time.perf_counter()
    root = STATE["root"]
    all_videos = STATE["videos"] or []
    scanning = bool(STATE.get("scanning"))
    scope_started = time.perf_counter()
    videos = videos_for_scope(lib or None)
    scope_ms = (time.perf_counter() - scope_started) * 1000.0

    # ------------------------------------------------------------------
    # Disk-cache read attempt (tree + facets for the unified case).
    # Skip during a scan: STATE["lib_gen"] bumps every publish and the
    # live catalog is only partially built, so reads would almost always
    # signature-mismatch and waste I/O.
    # ------------------------------------------------------------------
    tree_cache_stats: dict | None = None
    facets_disk_stats: dict | None = None
    tree_cache_used = False
    facets_disk_used = False

    if not scanning and len(all_videos) > 0 and len(videos) > 0:
        try:
            from vg.catalog_cache import (
                emit_load_log,
                load_facets_disk_cache,
                load_tree_disk_cache,
            )
        except Exception:
            emit_load_log = None  # type: ignore
            load_facets_disk_cache = None  # type: ignore
            load_tree_disk_cache = None  # type: ignore
    else:
        emit_load_log = None  # type: ignore
        load_facets_disk_cache = None  # type: ignore
        load_tree_disk_cache = None  # type: ignore

    # Tree read attempt
    tree_started = time.perf_counter()
    tree = None
    if load_tree_disk_cache is not None:
        loaded_tree, ts = load_tree_disk_cache(lib, len(videos))
        tree_cache_stats = ts
        if loaded_tree is not None:
            tree = loaded_tree
            tree_cache_used = True
    if tree is None:
        tree = tree_for_scope(lib or None)
    tree_ms = (time.perf_counter() - tree_started) * 1000.0
    if emit_load_log is not None and tree_cache_stats is not None:
        ev = tree_cache_stats.pop("event", "tree_disk_cache_load")
        if tree_cache_stats.get("hit"):
            tree_cache_stats["tree_ms_if_recompute"] = (
                f"{tree_ms:.1f}"  # total actually spent reading the cache
            )
            emit_load_log("PERF", ev, force=True, from_build_tree=True, **tree_cache_stats)
        elif tree_cache_stats.get("miss_reason"):
            emit_load_log("PERF", ev, force=True, from_build_tree=True, **tree_cache_stats)

    if tree_cache_used:
        _set_request_cache_layer(
            "L2_disk_tree",
            cache="tree_disk_hit",
            lib=lib or "all",
            cache_path=(tree_cache_stats or {}).get("cache_path", ""),
        )

    # Prefer precomputed facets for the unified catalog; scoped views recompute.
    # Also reject cache when it disagrees with the folder tree (same video source).
    facets = STATE.get("facets") or {}
    tree_count = int((tree or {}).get("count") or 0)
    use_cached = (
        not lib
        and facets
        and int(facets.get("count") or -1) == len(videos)
        and int(facets.get("count") or -1) == tree_count
        and not scanning
    )
    # If STATE["facets"] is not usable but we have a valid disk cache for the
    # whole-library facets (lib="" matches disk cache's unified scope), fill
    # it from disk so we skip the per-video counting loop.  Scoped libs always
    # recompute — the disk facets file only describes the union.
    if not use_cached and not lib and not scanning and load_facets_disk_cache is not None:
        loaded_facets, fs = load_facets_disk_cache(len(videos))
        facets_disk_stats = fs
        if (
            loaded_facets is not None
            and int(loaded_facets.get("count") or -1) == len(videos)
            and int(loaded_facets.get("count") or -1) == tree_count
        ):
            facets = loaded_facets
            STATE["facets"] = facets  # next callers benefit from the in-memory form
            use_cached = True
            facets_disk_used = True
        if fs:
            ev = fs.pop("event", "facets_disk_cache_load")
            if fs.get("hit") or fs.get("miss_reason"):
                emit_load_log(
                    "PERF",
                    ev,
                    force=True,
                    from_build_tree=True,
                    injected=facets_disk_used,
                    **fs,
                )
    if facets_disk_used and not tree_cache_used:
        _set_request_cache_layer(
            "L2_disk_facets",
            cache="facets_disk_hit",
            lib=lib or "all",
            cache_path=(facets_disk_stats or {}).get("cache_path", ""),
        )
    elif not tree_cache_used and not facets_disk_used:
        _set_request_cache_layer(
            "L3_tree_rebuild",
            cache="disk_miss_or_skipped",
            lib=lib or "all",
        )
    facets_started = time.perf_counter()
    if use_cached:
        types = facets.get("types") or []
        genres = facets.get("genres") or []
        themes = facets.get("themes") or []
        backgrounds = facets.get("backgrounds") or []
        categories = facets.get("categories") or []
        count = int(facets.get("count") or len(videos))
    else:
        # Per-segment timing inside the facets recomputation branch.
        # Previous logs showed ``facets_ms=882`` but ``taxonomy_facets_pair``
        # only accounted for ~3.5ms — the gap is in this loop (type/cat/genre
        # counting) and the subsequent list builds.  We split:
        #   loop_ms        : the for-loop over all videos
        #     genre_ms     : cumulative ensure_video_genres() time
        #     cat_ms       : cumulative _video_category() time
        #   genre_hits/misses : whether ensure_video_genres found a cached
        #                       non-empty list or re-ran detect_genres
        #                       (empty genres [] is falsy → always misses)
        #   build_types_ms / build_genres_ms / build_cat_ms : list construction
        #   taxonomy_ms    : taxonomy_facets_pair (also has its own detail line)
        loop_t0 = time.perf_counter()
        type_counts: dict[str, int] = {}
        cat_counts: dict[str, int] = {}
        genre_counts: dict[str, int] = {}
        genre_ms = 0.0
        cat_ms = 0.0
        genre_hits = 0
        genre_misses = 0
        for v in videos:
            ext = (v.get("ext") or "").lower() or "unknown"
            type_counts[ext] = type_counts.get(ext, 0) + 1
            tc = time.perf_counter()
            cat = _video_category(v)
            cat_ms += (time.perf_counter() - tc) * 1000.0
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
            # Pre-check genre cache hit (non-empty list) to attribute time
            # to "actually running detect_genres" vs "reading cached field".
            cached_genres = v.get("genres")
            if isinstance(cached_genres, list) and cached_genres:
                genre_hits += 1
            else:
                genre_misses += 1
            tg = time.perf_counter()
            for g in ensure_video_genres(v):
                genre_counts[g] = genre_counts.get(g, 0) + 1
            genre_ms += (time.perf_counter() - tg) * 1000.0
        loop_ms = (time.perf_counter() - loop_t0) * 1000.0

        bt_t0 = time.perf_counter()
        types = [
            {"ext": ext, "count": cnt, "label": ext.lstrip(".").upper() or "未知"}
            for ext, cnt in sorted(type_counts.items(), key=lambda x: (-x[1], x[0]))
        ]
        build_types_ms = (time.perf_counter() - bt_t0) * 1000.0

        bg_t0 = time.perf_counter()
        genre_order = {name: i for i, (name, _) in enumerate(GENRE_DEFS)}
        genres = [
            {"id": name, "name": name, "count": cnt}
            for name, cnt in sorted(
                genre_counts.items(),
                key=lambda x: (genre_order.get(x[0], 999), -x[1], x[0]),
            )
            if cnt > 0
        ]
        build_genres_ms = (time.perf_counter() - bg_t0) * 1000.0

        bc_t0 = time.perf_counter()
        categories = build_category_facets(cat_counts)
        build_cat_ms = (time.perf_counter() - bc_t0) * 1000.0

        # Single-pass taxonomy facets (themes + backgrounds together) instead
        # of two separate full-list scans.  For a 2785-video catalog this was
        # the dominant cost in tree_build (facets_ms ~ 721 ms out of 907 ms).
        tx_t0 = time.perf_counter()
        themes, backgrounds = taxonomy_facets_pair(videos)
        taxonomy_ms = (time.perf_counter() - tx_t0) * 1000.0

        count = len(videos)

        diagnostic_emit(
            "PERF",
            "tree_build_facets_breakdown",
            force=True,
            videos=len(videos),
            loop_ms=f"{loop_ms:.1f}",
            genre_ms=f"{genre_ms:.1f}",
            cat_ms=f"{cat_ms:.1f}",
            genre_hits=genre_hits,
            genre_misses=genre_misses,
            build_types_ms=f"{build_types_ms:.1f}",
            build_genres_ms=f"{build_genres_ms:.1f}",
            build_cat_ms=f"{build_cat_ms:.1f}",
            taxonomy_ms=f"{taxonomy_ms:.1f}",
        )
    facets_ms = (time.perf_counter() - facets_started) * 1000.0
    mounts_started = time.perf_counter()
    # Pass the scoped ``videos`` (already materialised from SQLite/memory)
    # as the snapshot source, not ``all_videos = STATE["videos"] or []``.
    # During a scan STATE["videos"] may still be empty (publish happens at
    # the end), which made ``use_snapshot`` flip to False and forced
    # ``roots_summary`` to reopen every per-disk catalog for each root:
    #     roots_summary_slow 206ms  D:\:56.5ms/0 | E:\:43.0ms/0 | G:\:77.5ms/0
    # The scoped ``videos`` list is a valid snapshot of the current catalog
    # state and lets the O(N) bucketisation path run instead.
    mounts = roots_summary(videos)
    mounts_ms = (time.perf_counter() - mounts_started) * 1000.0

    # Warm on-disk caches lazily (only_if_missing=True) for this payload if
    # we were forced to recompute any of it.  This is the secondary write
    # path; the primary one is ``apply_catalog_to_state`` +
    # ``publish_unified_library``.  Here we cover the edge cases where the
    # cache file did not exist at state-publish time (e.g. first run on a
    # new ``STATE["cache_dir"]`` after switching privacy settings, or a
    # manual deletion of the cache dir while the app runs).
    #
    # All write calls are fully wrapped in try/except and self-log via
    # ``emit_save_log``/diagnostic_error; any exception must not break the
    # tree response because the in-memory payload is still valid.
    warm_facets_result: dict = {}
    warm_tree_result: dict = {}
    if not scanning and len(videos) > 0:
        try:
            from vg.catalog_cache import (
                emit_save_log as _bs_emit_save,
                save_facets_disk_cache as _bs_save_facets,
                save_tree_disk_cache as _bs_save_tree,
            )

            # Facets warm: only for lib="" (unified scope matches the disk
            # facets schema count field exactly).
            if not lib:
                composed_facets = {
                    "types": types,
                    "genres": genres,
                    "themes": themes,
                    "backgrounds": backgrounds,
                    "categories": categories,
                    "count": count,
                }
                warm_facets_result = _bs_save_facets(
                    composed_facets, len(videos), only_if_missing=True
                )
                ev = warm_facets_result.pop("event", "facets_disk_cache_save")
                if warm_facets_result.get("bytes_written"):
                    _bs_emit_save(
                        "PERF",
                        ev,
                        force=True,
                        from_build_tree=True,
                        lib=lib or "all",
                        **warm_facets_result,
                    )
                elif warm_facets_result.get("skip_reason") and warm_facets_result.get("skip_reason") != "already_valid":
                    _bs_emit_save(
                        "WARN",
                        ev,
                        force=True,
                        from_build_tree=True,
                        lib=lib or "all",
                        **warm_facets_result,
                    )

            # Tree warm: for any lib (we compute per-scope trees).
            warm_tree_result = _bs_save_tree(
                lib, tree, len(videos), only_if_missing=True
            )
            ev = warm_tree_result.pop("event", "tree_disk_cache_save")
            if warm_tree_result.get("bytes_written"):
                _bs_emit_save(
                    "PERF",
                    ev,
                    force=True,
                    from_build_tree=True,
                    **warm_tree_result,
                )
            elif warm_tree_result.get("skip_reason") and warm_tree_result.get("skip_reason") != "already_valid":
                _bs_emit_save(
                    "WARN",
                    ev,
                    force=True,
                    from_build_tree=True,
                    **warm_tree_result,
                )
        except Exception as exc:
            from vg.diagnostics import error

            error("build_tree_payload_cache_warm_unexpected_exception", exc)

    payload = {
        "tree": tree,
        "types": types,
        "genres": genres,
        "themes": themes,
        "backgrounds": backgrounds,
        "categories": categories,
        "count": count,
        "root": str(root) if root else "",
        "lib": lib,
        "roots": mounts,
        "multi": len(mounts) > 1,
    }
    diagnostic_perf(
        "tree_build",
        (time.perf_counter() - started) * 1000.0,
        lib=lib or "all",
        videos=len(videos),
        categories=len(categories),
        roots=len(mounts),
        facets_cache="hit" if use_cached else "miss",
        facets_disk_cache="hit" if facets_disk_used else ("miss" if facets_disk_stats is not None else "skip"),
        tree_disk_cache="hit" if tree_cache_used else ("miss" if tree_cache_stats is not None else "skip"),
        scope_ms=f"{scope_ms:.1f}",
        tree_ms=f"{tree_ms:.1f}",
        facets_ms=f"{facets_ms:.1f}",
        mounts_ms=f"{mounts_ms:.1f}",
        scanning=bool(STATE.get("scanning")),
    )
    return payload


@app.route("/api/tree")
def api_tree():
    started = time.perf_counter()
    lib = (request.args.get("lib") or "").strip()
    diagnostic_call("api_tree", lib=lib or "all")
    cache_key = _tree_cache_key(lib)
    try:
        heavy = _cached_tree_payload(cache_key)
    except Exception as exc:
        diagnostic_error(
            "tree_payload_cache_read_failed",
            exc,
            request_id=getattr(g, "_diag_request_id", ""),
            lib=lib or "all",
            lib_gen=int(STATE.get("lib_gen") or 0),
        )
        raise
    cache_result = "hit" if heavy is not None else "miss"
    diagnostic_emit(
        "INFO",
        "tree_payload_cache_resolution",
        force=diagnostic_full_logging_enabled(),
        request_id=getattr(g, "_diag_request_id", ""),
        cache=cache_result,
        lib=lib or "all",
        lib_gen=int(STATE.get("lib_gen") or 0),
        scanning=bool(STATE.get("scanning")),
        updating=bool(STATE.get("updating")),
        etag_supplied=bool(request.if_none_match),
    )
    if heavy is not None:
        _set_request_cache_layer(
            "L1_server_tree_memory",
            cache="tree_payload_hit",
            lib=lib or "all",
        )
    if heavy is None:
        try:
            heavy = _build_tree_payload(lib)
            # Catalog identity is in the cache key; scanning/updating are overlaid
            # at serialize time so a background count does not rebuild the tree.
            _store_tree_payload(cache_key, heavy)
        except Exception as exc:
            diagnostic_error(
                "tree_payload_build_or_store_failed",
                exc,
                request_id=getattr(g, "_diag_request_id", ""),
                lib=lib or "all",
                lib_gen=int(STATE.get("lib_gen") or 0),
                scanning=bool(STATE.get("scanning")),
                updating=bool(STATE.get("updating")),
            )
            raise

    if not isinstance(heavy, dict) or "tree" not in heavy or "count" not in heavy:
        diagnostic_emit(
            "ERROR",
            "tree_payload_invalid",
            force=True,
            request_id=getattr(g, "_diag_request_id", ""),
            payload_type=type(heavy).__name__,
            has_tree=isinstance(heavy, dict) and "tree" in heavy,
            has_count=isinstance(heavy, dict) and "count" in heavy,
            cache=cache_result,
            lib=lib or "all",
        )

    serialize_started = time.perf_counter()
    response = jsonify({
        **heavy,
        "scanning": STATE["scanning"],
        "updating": bool(STATE.get("updating")),
        "exporting": bool(STATE.get("exporting")),
        "export_msg": STATE.get("export_msg") or "",
        "export_path": STATE.get("export_path") or "",
        "export_ok": STATE.get("export_ok"),
        "scan_progress": STATE["scan_progress"],
        "thumb_progress": STATE["thumb_progress"],
        "meta_progress": STATE.get("meta_progress") or "",
        "lib_gen": int(STATE.get("lib_gen") or 0),
        "scan_found": len(STATE.get("scan_live") or []) if isinstance(STATE.get("scan_live"), list) else 0,
        "has_ffmpeg": bool(STATE["ffmpeg"]),
        "bind_host": STATE.get("bind_host") or "127.0.0.1",
        "bind_port": int(STATE.get("bind_port") or 8765),
        "lan_share": bool(STATE.get("lan_share")),
        "lan_urls": lan_urls(),
        "privacy": privacy_snapshot(),
    })
    serialize_ms = (time.perf_counter() - serialize_started) * 1000.0
    diagnostic_perf(
        "api_tree",
        (time.perf_counter() - started) * 1000.0,
        force=cache_result == "miss",
        request_id=getattr(g, "_diag_request_id", ""),
        lib=lib or "all",
        cache=cache_result,
        count=heavy.get("count"),
        roots=len(heavy.get("roots") or []),
        serialize_ms=f"{serialize_ms:.1f}",
        response_bytes=response.calculate_content_length(),
        scanning=bool(STATE.get("scanning")),
    )
    return response


@app.route("/api/videos-by-ids", methods=["POST"])
def api_videos_by_ids():
    """按 id 批量取条目（继续看 / 历史）。支持 hints.root 跨盘解析。"""
    data = request.get_json(silent=True) or {}
    ids = data.get("ids") or []
    hints = data.get("hints") or {}
    if not isinstance(ids, list):
        return jsonify({"ok": False, "msg": "ids 无效"}), 400
    if not isinstance(hints, dict):
        hints = {}

    # 先按历史里的 root 预加载索引（不切换当前盘）
    roots_needed = []
    seen_roots: set[str] = set()
    for vid in ids[:100]:
        h = hints.get(str(vid)) or hints.get(vid) or {}
        if isinstance(h, dict):
            r = (h.get("root") or "").strip()
            if r and r.casefold() not in seen_roots:
                seen_roots.add(r.casefold())
                roots_needed.append(r)
                ensure_library(r)

    out = []
    missing = []
    offline = set(offline_roots(roots_needed))
    for vid in ids[:100]:
        vid = str(vid or "")
        if not vid:
            continue
        h = hints.get(vid) or {}
        prefer = (h.get("root") or "").strip() if isinstance(h, dict) else ""
        v = find_video_by_id(vid, prefer_root=prefer or None)
        if not v:
            miss = {"id": vid}
            if prefer:
                miss["root"] = prefer
                if prefer in offline:
                    miss["offline"] = True
            missing.append(miss)
            continue
        enriched = dict(v)
        attach_thumb_meta(enriched)
        row = {
            k: enriched[k]
            for k in enriched
            if k not in ("segments", "_q", "_lib_root", "_lib_cache", "_thumb_id")
        }
        # 给前端存盘符用
        if v.get("_lib_root"):
            row["root"] = v["_lib_root"]
        elif STATE.get("root"):
            row["root"] = str(STATE["root"])
        out.append(row)
    return jsonify({
        "ok": True,
        "videos": out,
        "count": len(out),
        "missing": missing,
        "offline_roots": sorted(offline),
    })


def _genre_facets(videos: list[dict]) -> list[dict]:
    """统计类型；只返回有片的。含子目录路径里识别到的类型。"""
    genre_counts: dict[str, int] = {}
    for v in videos:
        for g in ensure_video_genres(v):
            genre_counts[g] = genre_counts.get(g, 0) + 1
    genre_order = {name: i for i, (name, _) in enumerate(GENRE_DEFS)}
    return [
        {"id": name, "name": name, "count": cnt}
        for name, cnt in sorted(
            genre_counts.items(),
            key=lambda x: (genre_order.get(x[0], 999), -x[1], x[0]),
        )
        if cnt > 0
    ]


def _subfolder_facets(videos: list[dict], category: str, folder: str) -> list[dict]:
    """
    当前频道/子目录下的「下一级」文件夹列表。
    例如选了 电影 → 列出 华语/欧美；再选 电影/华语 → 列出 动作/爱情。
    仅统计真正的子目录（路径更深），本层直接放文件的不算子类。
    """
    prefix = (folder or category or "").strip("/")
    counts: dict[str, int] = {}
    for v in videos:
        f = (v.get("folder") or "").strip("/")
        if not f:
            continue
        if prefix:
            if f == prefix:
                continue  # 就在本层文件，没有更深子目录
            if not f.startswith(prefix + "/"):
                continue
            rest = f[len(prefix) + 1 :]
        else:
            rest = f
        nxt = rest.split("/")[0]
        if not nxt:
            continue
        full = f"{prefix}/{nxt}" if prefix else nxt
        if f == full or f.startswith(full + "/"):
            counts[nxt] = counts.get(nxt, 0) + 1
    return [
        {
            "id": (f"{prefix}/{name}" if prefix else name),
            "name": name,
            "count": cnt,
        }
        for name, cnt in sorted(counts.items(), key=lambda x: (-x[1], x[0].lower()))
        if cnt > 0
    ]


def _subfolder_levels(cat_videos: list[dict], category: str, folder: str) -> list[dict]:
    """
    多层子类行：
    - 第 1 行：频道下的直接子目录
    - 选中某一项且其下还有子目录时，再追加一行
    - folder 始终为完整相对路径（含频道名）
    """
    cat = (category or "").strip("/")
    if not cat or cat == "__root__":
        return []

    folder_norm = (folder or "").strip("/").replace("\\", "/")
    if folder_norm == cat:
        folder_norm = ""
    # 纠正：子路径必须挂在当前频道下
    if folder_norm and folder_norm != cat and not folder_norm.startswith(cat + "/"):
        folder_norm = f"{cat}/{folder_norm}"

    # 从频道到当前选中路径的前缀链
    prefixes: list[str] = [cat]
    if folder_norm.startswith(cat + "/"):
        acc = cat
        for part in folder_norm[len(cat) + 1 :].split("/"):
            if not part:
                continue
            acc = f"{acc}/{part}"
            prefixes.append(acc)

    levels: list[dict] = []
    for i, prefix in enumerate(prefixes):
        items = _subfolder_facets(cat_videos, "", prefix)
        if not items:
            break  # 该层没有子目录（只有文件）→ 不再追加行
        selected = ""
        if folder_norm.startswith(prefix + "/"):
            selected = prefix + "/" + folder_norm[len(prefix) + 1 :].split("/")[0]
        label = "子类" if i == 0 else prefix.split("/")[-1]
        # 第 1 行「全部」清空；更深行「全部」= 停在本层（勾选=含子目录全部，取消=只看根目录）
        all_id = "" if prefix == cat else prefix
        levels.append({
            "label": label,
            "prefix": prefix,
            "all_id": all_id,
            "selected": selected,
            "items": items,
        })
    return levels


def _extension_facets(videos: list[dict]) -> list[dict]:
    """Count formats for the current scope, independent of selected ext."""
    started = time.perf_counter()
    counts: dict[str, int] = {}
    for item in videos:
        ext = str(item.get("ext") or "").strip().lower()
        if ext:
            counts[ext] = counts.get(ext, 0) + 1
    result = [
        {"id": ext, "ext": ext, "label": ext.lstrip("."), "name": ext.lstrip("."), "count": count}
        for ext, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ]
    diagnostic_perf(
        "extension_facets_built",
        (time.perf_counter() - started) * 1000.0,
        source_rows=len(videos),
        type_facets=len(result),
    )
    return result


def _prepare_video_query(
    lib: str,
    category: str,
    folder: str,
    folder_all: bool,
    genre: str,
    theme: str,
    background: str,
    q_raw: str,
    parsed: dict,
    ext: str,
    view: str,
    sort: str,
) -> tuple:
    """Build one complete filtered/faceted/sorted result for /api/videos."""
    query_started = time.perf_counter()
    scope_started = time.perf_counter()
    videos = list(videos_for_scope(lib or None))
    videos = filter_videos_by_scope(videos, category=category)
    scope_ms = (time.perf_counter() - scope_started) * 1000.0

    cat_videos = videos
    if ext or q_raw:
        cat_videos = list(videos)
        if ext:
            ext_n = ext if ext.startswith(".") else "." + ext
            cat_videos = [v for v in cat_videos if (v.get("ext") or "").lower() == ext_n]
        if q_raw:
            cat_videos = [
                v for v in cat_videos
                if video_matches_query(v, parsed, _video_search_text)
            ]

    subfolder_levels = _subfolder_levels(cat_videos, category, folder)

    folder_started = time.perf_counter()
    if folder:
        has_children = bool(_subfolder_facets(videos, "", folder))
        videos = filter_videos_by_scope(
            videos,
            folder=folder,
            include_descendants=not (has_children and not folder_all),
        )
    folder_ms = (time.perf_counter() - folder_started) * 1000.0
    type_videos = list(videos)
    if q_raw:
        type_videos = [
            v for v in type_videos
            if video_matches_query(v, parsed, _video_search_text)
        ]
    scoped_types = _extension_facets(type_videos)
    if ext:
        ext_n = ext if ext.startswith(".") else "." + ext
        videos = [v for v in videos if (v.get("ext") or "").lower() == ext_n]
    if q_raw:
        videos = [v for v in videos if video_matches_query(v, parsed, _video_search_text)]

    taxonomy_started = time.perf_counter()
    scoped_genres = _genre_facets(videos)
    scoped_themes = taxonomy_facets(videos, "themes")
    scoped_backgrounds = taxonomy_facets(videos, "backgrounds")
    scoped_subs = _subfolder_facets(videos, category, folder)
    taxonomy_ms = (time.perf_counter() - taxonomy_started) * 1000.0

    selected_filter_started = time.perf_counter()
    if genre:
        videos = [v for v in videos if genre in ensure_video_genres(v)]
    if theme:
        videos = [v for v in videos if theme in ensure_video_taxonomy(v)[0]]
    if background:
        videos = [v for v in videos if background in ensure_video_taxonomy(v)[1]]
    selected_filter_ms = (time.perf_counter() - selected_filter_started) * 1000.0

    raw_count = len(videos)
    if view == "series":
        videos = collapse_to_series_cards(videos)

    reverse = True
    key_fn = lambda v: v.get("mtime") or 0
    if sort == "mtime_asc":
        reverse = False
    elif sort == "name":
        key_fn = lambda v: (v.get("name") or "").lower()
        reverse = False
    elif sort == "size_desc":
        key_fn = lambda v: v.get("size") or 0
    elif sort == "size_asc":
        key_fn = lambda v: v.get("size") or 0
        reverse = False
    elif sort == "duration_desc":
        key_fn = lambda v: v.get("duration") or 0
    elif sort == "duration_asc":
        key_fn = lambda v: v.get("duration") or 0
        reverse = False

    sort_started = time.perf_counter()
    videos = sorted(videos, key=key_fn, reverse=reverse)
    sort_ms = (time.perf_counter() - sort_started) * 1000.0
    diagnostic_perf(
        "python_video_query",
        (time.perf_counter() - query_started) * 1000.0,
        source_rows=len(videos),
        raw_count=raw_count,
        scope_ms=f"{scope_ms:.1f}",
        folder_ms=f"{folder_ms:.1f}",
        taxonomy_ms=f"{taxonomy_ms:.1f}",
        selected_filter_ms=f"{selected_filter_ms:.1f}",
        sort_ms=f"{sort_ms:.1f}",
        category=category or "all",
        folder=folder,
        ext=ext,
        genre=bool(genre),
        theme=bool(theme),
        background=bool(background),
        view=view,
    )
    return (
        videos,
        raw_count,
        scoped_genres,
        scoped_themes,
        scoped_backgrounds,
        scoped_subs,
        subfolder_levels,
        scoped_types,
    )


@app.route("/api/videos")
def api_videos():
    api_started = time.perf_counter()
    query_ms = 0.0
    page_ms = 0.0
    folder = request.args.get("folder", "").strip().strip("/")
    category = request.args.get("category", "").strip().strip("/")
    genre = request.args.get("genre", "").strip()
    theme = request.args.get("theme", "").strip()
    background = request.args.get("background", "").strip()
    q_raw = request.args.get("q", "").strip()
    parsed = parse_search_query(q_raw)
    # 搜索语法里的 field 覆盖独立筛选项
    if parsed.get("ext"):
        ext = parsed["ext"]
    else:
        ext = request.args.get("ext", "").strip().lower()
    if parsed.get("genre") and not genre:
        genre = parsed["genre"]
    if parsed.get("theme") and not theme:
        theme = parsed["theme"]
    if parsed.get("background") and not background:
        background = parsed["background"]
    if parsed.get("category") and not category:
        category = parsed["category"]
    sort = request.args.get("sort", "mtime_desc").strip().lower()
    lib = (request.args.get("lib") or "").strip()

    folder = folder.replace("\\", "/")
    folder_all = request.args.get("folder_all", "").strip() in ("1", "true", "yes")
    view = (request.args.get("view") or "flat").strip().lower()
    view = view if view in ("series", "flat") else "flat"
    try:
        offset = max(0, int(request.args.get("offset", 0) or 0))
    except ValueError:
        offset = 0
    try:
        limit = int(request.args.get("limit", 60) or 60)
    except ValueError:
        limit = 60
    limit = max(1, min(limit, 200))
    diagnostic_call(
        "api_videos",
        lib=lib or "all",
        category=category or "all",
        folder=folder,
        folder_all=folder_all,
        ext=ext,
        view=view,
        sort=sort,
        offset=offset,
        limit=limit,
        query_len=len(q_raw),
    )

    mounted = [str(root) for root in (STATE.get("mounted_roots") or []) if root]
    sql_roots = [lib] if lib else mounted
    if not sql_roots and STATE.get("root"):
        sql_roots = [str(STATE["root"])]
    sql_skip_reason = ""
    if view != "flat":
        sql_skip_reason = f"view={view}"
    elif ":" in q_raw:
        sql_skip_reason = "complex_search"
    elif not sql_roots:
        sql_skip_reason = "no_roots"
    sql_eligible = not sql_skip_reason
    # Time the segments that happen BEFORE ``sql_started``: arg parsing,
    # parse_search_query, mounted-roots lookup, ``_catalog_caches_for_roots``
    # and the include_descendants probe.  The existing api_videos_sql PERF
    # line only reports sql_ms + facets_ms + thumb_meta_ms + serialize_ms,
    # which previously summed to ~94ms while the request took 1312ms — the
    # ~1.2s gap lived entirely in this pre-sql setup region.
    setup_t0 = time.perf_counter()
    if sql_eligible:
        from vg.catalog_db import (
            facets_from_rows,
            load_catalog_facet_rows,
            merge_catalog_facets,
            query_catalog_page,
            query_catalogs_page,
        )

        _caches_t0 = time.perf_counter()
        sql_caches = _catalog_caches_for_roots(sql_roots)
        caches_ms = (time.perf_counter() - _caches_t0) * 1000.0
        if not sql_caches:
            sql_skip_reason = "no_catalog_cache"
            sql_eligible = False
        else:
            # Match Python scope: exact folder only when user unchecked「全部」
            # on a folder that still has children; otherwise include descendants.
            include_descendants = True
            probe_ms = 0.0
            probe_count = 0
            has_children = False
            facet_rowsets: list[list[dict]] = []
            need_facet_rows = offset == 0 or bool(folder and not folder_all)
            facet_load_started = time.perf_counter()
            if need_facet_rows:
                facet_rowsets = [
                    load_catalog_facet_rows(cache, category=category, search=q_raw)
                    for cache in sql_caches
                ]
                probe_count = len(facet_rowsets)
            if folder and not folder_all:
                folder_n = folder.strip("/").replace("\\", "/")
                prefix = folder_n + "/"
                for rows in facet_rowsets:
                    if any(
                        (row.get("folder") or "").startswith(prefix)
                        for row in rows
                    ):
                        has_children = True
                        break
                include_descendants = not has_children
            probe_ms = (time.perf_counter() - facet_load_started) * 1000.0
            if probe_ms >= 100.0:
                diagnostic_emit(
                    "WARN",
                    "api_videos_sql_include_descendants_probe_slow",
                    force=True,
                    request_id=getattr(g, "_diag_request_id", ""),
                    probe_ms=f"{probe_ms:.1f}",
                    probes=probe_count,
                    caches=len(sql_caches),
                    folder=folder,
                    category=category or "all",
                    ext=ext,
                    has_children=has_children,
                    include_descendants=include_descendants,
                )

            # Total pre-sql setup time (parse + caches lookup + probe).
            setup_ms = (time.perf_counter() - setup_t0) * 1000.0
            sql_started = time.perf_counter()
            query_kwargs = dict(
                category=category,
                folder=folder,
                include_descendants=include_descendants,
                ext=ext,
                search=q_raw,
                genre=genre,
                theme=theme,
                background=background,
                sort=sort,
            )
            if len(sql_caches) == 1:
                page, total = query_catalog_page(
                    sql_caches[0],
                    offset=offset,
                    limit=limit,
                    **query_kwargs,
                )
            else:
                page, total = query_catalogs_page(
                    sql_caches,
                    offset=offset,
                    limit=limit,
                    **query_kwargs,
                )
            sql_ms = (time.perf_counter() - sql_started) * 1000.0
            thumb_started = time.perf_counter()
            duplicate_index, runtime_video_count, runtime_duplicate_count = (
                _runtime_duplicate_fields_index()
            )
            sql_runtime_field_rows = 0
            matched_runtime_duplicate_rows = 0
            slim = []
            for video in page:
                enriched = dict(video)
                if not enriched.get("root"):
                    enriched["root"] = lib or enriched.get("_lib_root") or ""
                if lib and not enriched.get("root"):
                    enriched["root"] = lib
                if any(key in enriched for key in ("dup", "dup_n", "dup_reason")):
                    sql_runtime_field_rows += 1
                duplicate_fields = duplicate_index.get(video_identity(enriched))
                if duplicate_fields is None:
                    duplicate_fields = duplicate_index.get(_video_location_identity(enriched))
                if duplicate_fields:
                    enriched.update(duplicate_fields)
                    matched_runtime_duplicate_rows += 1
                attach_thumb_meta(enriched)
                excluded = {
                    "_q", "_lib_root", "_lib_cache", "_folder_raw", "_thumb_id", "segments"
                }
                row = {key: enriched[key] for key in enriched if key not in excluded}
                if not row.get("root"):
                    row["root"] = lib or ""
                slim.append(row)
            thumb_meta_ms = (time.perf_counter() - thumb_started) * 1000.0
            facet_started = time.perf_counter()
            base_facet_ms = 0.0
            type_facet_ms = 0.0
            if offset == 0:
                def _merge_from_rows(*, folder_value: str, ext_value: str, descendants: bool) -> dict:
                    try:
                        built = [
                            facets_from_rows(
                                rows,
                                category=category,
                                folder=folder_value,
                                include_descendants=descendants,
                                ext=ext_value,
                            )
                            for rows in facet_rowsets
                        ]
                    except Exception as exc:
                        diagnostic_error(
                            "api_videos_facets_derive_failed",
                            exc,
                            request_id=getattr(g, "_diag_request_id", ""),
                            category=category or "all",
                            folder=folder_value,
                            ext=ext_value,
                            caches=len(sql_caches),
                            loaded_rows=sum(len(rows) for rows in facet_rowsets),
                        )
                        raise
                    if not built:
                        return {
                            "genres": [],
                            "themes": [],
                            "backgrounds": [],
                            "subfolders": [],
                            "types": [],
                        }
                    return built[0] if len(built) == 1 else merge_catalog_facets(built)

                base_facet_started = time.perf_counter()
                facets = _merge_from_rows(
                    folder_value=folder,
                    ext_value=ext,
                    descendants=include_descendants,
                )
                base_facet_ms = (time.perf_counter() - base_facet_started) * 1000.0
                # 格式选项必须按当前频道/子类统计，不能沿用整盘 types；
                # 查询格式选项时刻意不带当前 ext，保证仍可切换到其它格式。
                type_facet_started = time.perf_counter()
                type_facets = _merge_from_rows(
                    folder_value=folder,
                    ext_value="",
                    descendants=include_descendants,
                )
                facets["types"] = type_facets.get("types") or []
                type_facet_ms = (time.perf_counter() - type_facet_started) * 1000.0
            else:
                facets = {"genres": [], "themes": [], "backgrounds": [], "subfolders": [], "types": []}
            facets_ms = (time.perf_counter() - facet_started) * 1000.0
            levels = []
            level_facet_ms = 0.0
            if offset == 0 and category and category != "__root__":
                level_facet_started = time.perf_counter()
                # SQL 路径也要返回完整的子类层级。此前只使用当前 folder 的
                # facets：选到叶子目录后没有更深子目录，subfolders 为空，
                # 前端就把整个子类区域清空了。
                category_n = category.strip("/").replace("\\", "/")
                folder_n = folder.strip("/").replace("\\", "/")
                if folder_n == category_n:
                    folder_n = ""
                prefixes = [category_n]
                if folder_n.startswith(category_n + "/"):
                    acc = category_n
                    for part in folder_n[len(category_n) + 1 :].split("/"):
                        if part:
                            acc = f"{acc}/{part}"
                            prefixes.append(acc)
                for level_index, prefix in enumerate(prefixes):
                    level_merged = _merge_from_rows(
                        folder_value=prefix,
                        ext_value="",
                        descendants=True,
                    )
                    items = level_merged.get("subfolders") or []
                    if not items:
                        break
                    selected = ""
                    if folder_n.startswith(prefix + "/"):
                        selected = prefix + "/" + folder_n[len(prefix) + 1 :].split("/")[0]
                    levels.append({
                        "label": "子类" if level_index == 0 else prefix.rsplit("/", 1)[-1],
                        "prefix": prefix,
                        "all_id": "" if prefix == category_n else prefix,
                        "selected": selected,
                        "items": items,
                    })
                level_facet_ms = (time.perf_counter() - level_facet_started) * 1000.0
            if offset == 0:
                loaded_facet_rows = sum(len(rows) for rows in facet_rowsets)
                diagnostic_perf(
                    "api_videos_facets_single_pass",
                    probe_ms + facets_ms + level_facet_ms,
                    force=False,
                    request_id=getattr(g, "_diag_request_id", ""),
                    caches=len(sql_caches),
                    sqlite_queries=len(facet_rowsets),
                    loaded_rows=loaded_facet_rows,
                    result_rows=total,
                    load_ms=f"{probe_ms:.1f}",
                    derive_ms=f"{(facets_ms + level_facet_ms):.1f}",
                    category=category or "all",
                    folder=folder,
                    ext=ext,
                    levels=len(levels),
                )
                if total > 0 and loaded_facet_rows == 0:
                    diagnostic_emit(
                        "WARN",
                        "api_videos_facets_empty_for_nonempty_result",
                        force=True,
                        request_id=getattr(g, "_diag_request_id", ""),
                        result_rows=total,
                        caches=len(sql_caches),
                        category=category or "all",
                        folder=folder,
                        ext=ext,
                    )
            serialize_started = time.perf_counter()
            response = jsonify({
                "videos": slim,
                "count": total,
                "raw_count": total,
                "offset": offset,
                "limit": limit,
                "has_more": offset + len(slim) < total,
                "genres": facets["genres"],
                "themes": facets["themes"],
                "backgrounds": facets["backgrounds"],
                "subfolders": facets["subfolders"],
                "types": facets.get("types") or [],
                "subfolder_levels": levels,
                "view": "flat",
                "lib": lib,
                "facets_included": offset == 0,
            })
            serialize_ms = (time.perf_counter() - serialize_started) * 1000.0
            total_ms = (time.perf_counter() - api_started) * 1000.0
            # Verify the per-segment times account for the whole request.
            # Expected: setup_ms + sql_ms + thumb_meta_ms + facets_ms
            #           + level_facet_ms + serialize_ms ≈ total_ms
            # If the gap is large, the missing time is in an untimed region
            # (e.g. between segments, or in a path we didn't instrument).
            attributed_ms = (
                setup_ms + sql_ms + thumb_meta_ms + facets_ms
                + level_facet_ms + serialize_ms
            )
            gap_ms = total_ms - attributed_ms
            diagnostic_perf(
                "api_videos_sql",
                total_ms,
                force=True,
                rows=len(slim),
                total_rows=total,
                offset=offset,
                caches=len(sql_caches),
                setup_ms=f"{setup_ms:.1f}",
                caches_ms=f"{caches_ms:.1f}",
                probe_ms=f"{probe_ms:.1f}",
                probe_count=probe_count,
                sql_ms=f"{sql_ms:.1f}",
                runtime_video_rows=runtime_video_count,
                runtime_duplicate_rows=runtime_duplicate_count,
                sql_runtime_field_rows=sql_runtime_field_rows,
                matched_runtime_duplicate_rows=matched_runtime_duplicate_rows,
                facets_ms=f"{facets_ms:.1f}",
                base_facet_ms=f"{base_facet_ms:.1f}",
                type_facet_ms=f"{type_facet_ms:.1f}",
                level_facet_ms=f"{level_facet_ms:.1f}",
                thumb_meta_ms=f"{thumb_meta_ms:.1f}",
                serialize_ms=f"{serialize_ms:.1f}",
                gap_ms=f"{gap_ms:.1f}",
                response_bytes=response.calculate_content_length(),
                category=category or "all",
                folder=folder,
                ext=ext,
                subfolder_facets=len(facets.get("subfolders") or []),
                subfolder_levels=len(levels),
                type_facets=len(facets.get("types") or []),
                sort=sort,
            )
            if gap_ms >= 100.0:
                # Surface unattributed time so we know there's still a
                # hidden hot spot we haven't instrumented.
                diagnostic_emit(
                    "WARN",
                    "api_videos_sql_unattributed_time",
                    force=True,
                    request_id=getattr(g, "_diag_request_id", ""),
                    total_ms=f"{total_ms:.1f}",
                    attributed_ms=f"{attributed_ms:.1f}",
                    gap_ms=f"{gap_ms:.1f}",
                    setup_ms=f"{setup_ms:.1f}",
                    sql_ms=f"{sql_ms:.1f}",
                    facets_ms=f"{facets_ms:.1f}",
                    thumb_meta_ms=f"{thumb_meta_ms:.1f}",
                    level_facet_ms=f"{level_facet_ms:.1f}",
                    serialize_ms=f"{serialize_ms:.1f}",
                    offset=offset,
                    folder=folder,
                )
            return response

    if sql_skip_reason:
        diagnostic_emit_rate_limited(
            "WARN",
            "api_videos_sql_fallback",
            key=f"{sql_skip_reason}|{view}|{lib or 'all'}",
            interval=30.0,
            force=True,
            reason=sql_skip_reason,
            view=view,
            lib=lib or "all",
            roots=len(sql_roots),
        )

    query_key = (
        int(STATE.get("lib_gen") or 0),
        lib,
        category,
        folder,
        folder_all,
        genre,
        theme,
        background,
        q_raw,
        ext,
        view,
        sort,
    )
    cached_query = video_query_cache_get(query_key)
    query_cache = "L1" if cached_query is not None else "miss"
    if cached_query is None:
        query_started = time.perf_counter()
        cached_query = _prepare_video_query(
            lib,
            category,
            folder,
            folder_all,
            genre,
            theme,
            background,
            q_raw,
            parsed,
            ext,
            view,
            sort,
        )
        query_ms = (time.perf_counter() - query_started) * 1000.0
        video_query_cache_put(query_key, cached_query)
    (
        videos,
        raw_count,
        scoped_genres,
        scoped_themes,
        scoped_backgrounds,
        scoped_subs,
        subfolder_levels,
        scoped_types,
    ) = cached_query

    total = len(videos)
    page_started = time.perf_counter()
    page = videos[offset: offset + limit]
    slim = []
    for v in page:
        if v.get("kind") == "series":
            row = {k: v[k] for k in v if k not in ("_q", "_lib_root", "_lib_cache", "_folder_raw", "_thumb_id")}
            cover_id = v.get("cover_id") or ""
            if cover_id and not row.get("thumb_id"):
                row["thumb_id"] = cover_id
            # Collapse already copied cover thumb flags. Re-resolving each card
            # during scan reloads the whole disk catalog and stalls the list.
            if cover_id and not v.get("has_thumb"):
                cover = find_video_by_id(
                    cover_id,
                    prefer_root=v.get("_lib_root") or v.get("root") or None,
                )
                if cover:
                    attach_thumb_meta(cover)
                    row["has_thumb"] = cover.get("has_thumb")
                    row["thumb_v"] = cover.get("thumb_v")
                    row["thumb_id"] = cover.get("thumb_id") or thumb_id_for_item(cover) or cover_id
            # 剧集来源盘
            eps = v.get("episodes") or []
            if eps and eps[0].get("lib_label"):
                row["lib_label"] = eps[0].get("lib_label")
            elif eps and eps[0].get("_lib_label"):
                row["lib_label"] = eps[0].get("_lib_label")
            slim.append(row)
            continue
        enriched = dict(v)
        attach_thumb_meta(enriched)
        excluded = {"_q", "_lib_root", "_lib_cache", "_folder_raw", "_thumb_id"}
        if v.get("kind") == "ts_set" and v.get("segments"):
            excluded.add("segments")
        row = {k: enriched[k] for k in enriched if k not in excluded}
        if v.get("_lib_root") and not row.get("root"):
            row["root"] = v["_lib_root"]
        if v.get("_lib_label") and not row.get("lib_label"):
            row["lib_label"] = v["_lib_label"]
        if v.get("actors"):
            row["actors"] = list(v["actors"])
        slim.append(row)

    page_ms = (time.perf_counter() - page_started) * 1000.0
    has_more = offset + len(slim) < total
    payload = {
        "videos": slim,
        "count": total,
        "raw_count": raw_count,
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
        "genres": scoped_genres,
        "themes": scoped_themes,
        "backgrounds": scoped_backgrounds,
        "subfolders": scoped_subs,
        "types": scoped_types,
        "subfolder_levels": subfolder_levels,
        "view": view if view in ("series", "flat") else "flat",
        "lib": lib,
    }
    serialize_started = time.perf_counter()
    response = jsonify(payload)
    serialize_ms = (time.perf_counter() - serialize_started) * 1000.0
    total_ms = (time.perf_counter() - api_started) * 1000.0

    scrolling = offset > 0
    # Diagnostics for scroll/pagination jitter:
    #   - overshoot: client asked for an offset beyond the current result set.
    #     This is the #1 backend-side cause of "suddenly has_more becomes
    #     false on a still-loading page and the browser stops paginating
    #     even though the user knows more videos exist".
    #   - empty_page_on_scroll: scrolling request that returned zero rows is
    #     a UI bug candidate (the page will silently stall instead of loading
    #     more).
    overshoot = offset > 0 and offset >= total
    empty_page_on_scroll = scrolling and len(slim) == 0
    jitter_warn = overshoot or empty_page_on_scroll or (scrolling and total_ms >= 800.0)
    if jitter_warn:
        from vg.diagnostics import emit as _diag_emit

        _diag_emit(
            "WARN",
            "api_videos_scroll_jitter",
            force=True,
            request_id=getattr(g, "_diag_request_id", ""),
            scrolling=scrolling,
            offset=offset,
            limit=limit,
            total_rows=total,
            returned_rows=len(slim),
            has_more=has_more,
            raw_count=raw_count,
            overshoot=overshoot,
            empty_page_on_scroll=empty_page_on_scroll,
            total_ms=f"{total_ms:.1f}",
            query_ms=f"{query_ms:.1f}",
            page_ms=f"{page_ms:.1f}",
            lib=lib or "all",
            category=category or "all",
            folder=folder,
            folder_all=folder_all,
            ext=ext,
            sort=sort,
            view=view,
        )
    diagnostic_perf(
        "api_videos",
        total_ms,
        force=True,
        request_id=getattr(g, "_diag_request_id", ""),
        query_ms=f"{query_ms:.1f}",
        page_ms=f"{page_ms:.1f}",
        serialize_ms=f"{serialize_ms:.1f}",
        rows=len(slim),
        total_rows=total,
        offset=offset,
        limit=limit,
        has_more=has_more,
        scrolling=scrolling,
        overshoot=overshoot,
        empty_page=empty_page_on_scroll,
        cache=query_cache,
        response_bytes=response.calculate_content_length(),
        category=category or "all",
        folder=folder,
        folder_all=folder_all,
        ext=ext,
        type_facets=len(scoped_types),
        sort=sort,
        view=view,
    )
    return response


@app.route("/api/drives")
def api_drives():
    root = STATE["root"]
    current = str(root) if root else ""
    try:
        drives = list_drives_info()
    except Exception as e:
        print(f"【错误】列出盘符失败: {e}")
        drives = []
    for d in drives:
        try:
            d["active"] = bool(current) and os.path.normcase(
                os.path.abspath(d["path"])
            ) == os.path.normcase(os.path.abspath(current))
        except OSError:
            d["active"] = False
    return jsonify({
        "drives": drives,
        "current": current,
        "scanning": STATE["scanning"],
        "count": len(drives),
    })


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """扫描所选盘；新盘自动加入片库，已加入的盘则更新其索引。"""
    started = time.perf_counter()
    data = request.get_json(silent=True) or {}
    path = (data.get("path") or data.get("drive") or "").strip().strip('"')
    if not path:
        return jsonify({"ok": False, "msg": "请选择盘符"}), 400
    # 允许 E: / E:\ / E:/
    if len(path) == 2 and path[1] == ":":
        path = path + "\\"
    do_thumbs = data.get("thumbs", True)
    force = data.get("force", False)
    trigger = str(data.get("trigger") or ("rescan_button" if force else "scan_button"))
    try:
        root = Path(path).expanduser().resolve()
    except OSError as e:
        return jsonify({"ok": False, "msg": f"路径无效: {e}"}), 400
    if not root.is_dir():
        return jsonify({"ok": False, "msg": f"目录不存在: {root}"}), 400

    diagnostic_call(
        "scan_button_click",
        trigger=trigger,
        root=root,
        force_scan=bool(force),
    )

    # start_scan's background worker mounts the root before loading/scanning it.
    # Doing add_mount here first archived and republished the entire catalog on
    # the request thread, making this supposedly asynchronous endpoint block
    # for 5–6 seconds on a multi-disk library.
    scan_started = time.perf_counter()
    ok, scan_msg = start_scan(
        root,
        do_thumbs=bool(do_thumbs),
        force=bool(force),
        replace_mounts=False,
    )
    scan_start_ms = (time.perf_counter() - scan_started) * 1000.0
    if not ok:
        return jsonify({"ok": False, "msg": scan_msg, "roots": roots_summary()}), 409
    roots_started = time.perf_counter()
    roots = roots_summary()
    roots_ms = (time.perf_counter() - roots_started) * 1000.0
    diagnostic_perf(
        "api_scan_start",
        (time.perf_counter() - started) * 1000.0,
        root=root,
        force_scan=bool(force),
        scan_start_ms=f"{scan_start_ms:.1f}",
        roots_ms=f"{roots_ms:.1f}",
        roots=len(roots),
    )
    return jsonify({
        "ok": True,
        "msg": f"{root_label(root)} 已自动加入片库，{scan_msg}",
        "root": str(root),
        "roots": roots,
    })


@app.route("/api/rescan", methods=["POST"])
def api_rescan():
    if not STATE["root"]:
        return jsonify({"ok": False, "msg": "尚未选择盘符"}), 400
    data = request.get_json(silent=True) or {}
    do_thumbs = data.get("thumbs", True)
    diagnostic_call("rescan_button_click", root=STATE["root"], force_scan=True)
    ok, msg = start_scan(
        STATE["root"],
        do_thumbs=bool(do_thumbs),
        force=True,
        replace_mounts=False,
    )
    if not ok:
        return jsonify({"ok": False, "msg": msg}), 409
    return jsonify({"ok": True, "msg": msg})


def _cache_dir_from_root_hint(root_hint: str | None) -> Path | None:
    """Resolve a scan-root hint to its thumb cache without catalog lookup.

    Used by /thumb hot path so already-generated .vgt files do not wait on
    find_video_by_id over multi-thousand catalogs (which starved waitress).
    Skips ensure_cache_dir's legacy cleanup — that belongs on scan, not every
    image request.
    """
    raw = (root_hint or "").strip()
    if not raw:
        return None
    try:
        from vg.disk_libs import _norm_root_str

        key = _norm_root_str(raw)
        lib = (STATE.get("disk_libs") or {}).get(key)
        if isinstance(lib, dict):
            cached = (lib.get("cache_dir") or "").strip()
            if cached:
                path = Path(cached)
                if path.is_dir():
                    return path
    except OSError:
        pass
    try:
        from vg.privacy import resolve_cache_dir_for_root

        return resolve_cache_dir_for_root(Path(raw))
    except OSError:
        return None


def _catalog_caches_for_roots(roots: list[str]) -> list[Path]:
    """Resolve only roots that already have a catalog.sqlite."""
    from vg.catalog_db import catalog_exists

    out: list[Path] = []
    seen: set[str] = set()
    for root_hint in roots:
        cache = _cache_dir_from_root_hint(root_hint)
        if cache is None or not catalog_exists(cache):
            continue
        try:
            key = str(cache.resolve()).casefold()
        except OSError:
            key = str(cache).casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(Path(cache))
    return out


def _serve_thumb_jpeg(cache: Path | None, file_id: str):
    if not cache or not file_id:
        return None
    raw = read_thumb_jpeg(cache, file_id)
    if not raw:
        return None
    return Response(
        raw,
        mimetype="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.route("/thumb/<vid>")
def thumb(vid: str):
    if not re.fullmatch(r"[a-f0-9]{16}", vid):
        diagnostic_emit(
            "WARN",
            "thumbnail_request_invalid_id",
            force=True,
            video_id=vid,
            operation_id=getattr(g, "_diag_operation_id", ""),
        )
        abort(404)
    prefer_root = (request.args.get("root") or "").strip() or None
    defer = request.args.get("defer", "").strip().lower() in ("1", "true", "yes")
    placeholder = '''<svg xmlns="http://www.w3.org/2000/svg" width="480" height="270" viewBox="0 0 480 270">
      <rect fill="#1a1d24" width="480" height="270"/>
      <polygon points="210,100 210,170 280,135" fill="#4a5568"/>
    </svg>'''
    placeholder_headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
    }

    def _deferred_placeholder():
        _set_request_cache_layer(
            "L3_thumb_placeholder",
            cache="missing_or_generation_queued",
            video_id=vid,
        )
        return Response(
            placeholder,
            status=503,
            mimetype="image/svg+xml",
            headers={**placeholder_headers, "Retry-After": "1"},
        )

    # Fast path: cards already pass owning root + thumb_id. Existing .vgt
    # must be served without scanning the in-memory catalog.
    hint_cache = _cache_dir_from_root_hint(prefer_root)
    served = _serve_thumb_jpeg(hint_cache, vid)
    if served is not None:
        return served
    if not prefer_root:
        active = STATE.get("cache_dir")
        served = _serve_thumb_jpeg(Path(active) if active else None, vid)
        if served is not None:
            return served
        # Try every known disk cache before any catalog lookup.
        for lib in (STATE.get("disk_libs") or {}).values():
            if not isinstance(lib, dict):
                continue
            cache_s = (lib.get("cache_dir") or "").strip()
            if not cache_s:
                continue
            served = _serve_thumb_jpeg(Path(cache_s), vid)
            if served is not None:
                return served

    # Root hint was given but file is not ready yet: do not block on
    # ensure_library / full-catalog search for deferred card loads.
    if prefer_root and defer:
        note_frontend_activity(0.8, source=f"thumb_deferred:{prefer_root[:40]}")
        item = None
        if STATE.get("scanning") or STATE.get("updating"):
            # The scan owns the catalog/cache transition.  Loading the whole
            # root here only delays the 503 placeholder and contends with the
            # writer; the browser will retry after the scan publishes a new
            # generation.
            diagnostic_emit_rate_limited(
                "INFO",
                "thumbnail_deferred_lookup_skipped",
                key=prefer_root,
                interval=5.0,
                force=True,
                video_id=vid,
                root=prefer_root,
                reason="catalog_transition",
                scanning=bool(STATE.get("scanning")),
                updating=bool(STATE.get("updating")),
                operation_id=getattr(g, "_diag_operation_id", ""),
            )
            diagnostic_aggregate("thumbnail_placeholder")
            return _deferred_placeholder()
        else:
            item = find_video_by_id(vid, prefer_root=prefer_root)
        cache = cache_dir_for_item(item) or hint_cache or STATE.get("cache_dir")
        if cache is not None:
            cache = Path(cache)
        file_id = thumb_id_for_item(item) if item else vid
        ffmpeg = STATE.get("ffmpeg")
        if item and ffmpeg and cache:
            src = _video_file_for_thumb(item)
            out = thumb_path(cache, file_id)
            if thumbnail_failure_is_current(item):
                diagnostic_aggregate("thumbnail_generation_skipped_persisted_fail")
                diagnostic_emit_rate_limited(
                    "INFO",
                    "thumbnail_generation_skipped_persisted_fail",
                    key=f"{prefer_root}|{file_id}",
                    interval=30.0,
                    force=True,
                    video_id=vid,
                    root=prefer_root,
                    reason=item.get("thumb_failed_reason") or "persisted_failure",
                )
                diagnostic_aggregate("thumbnail_placeholder")
                return _deferred_placeholder()
            # Metadata probing marks audio-only/corrupt containers as ``bad``.
            # They cannot produce a video frame, so queueing ffmpeg here only
            # repeats a 300-500ms failure for every thumbnail retry (and floods
            # the log with ``thumbnail_source_no_video_stream`` warnings).
            bad_reason = str(item.get("bad_reason") or "").casefold()
            if item.get("bad") and ("无视频流" in bad_reason or "no video" in bad_reason):
                diagnostic_aggregate("thumbnail_generation_skipped_no_video_stream")
                diagnostic_emit_rate_limited(
                    "INFO",
                    "thumbnail_generation_skipped_no_video_stream",
                    key=f"{prefer_root}|{vid}",
                    interval=30.0,
                    force=True,
                    video_id=vid,
                    root=prefer_root,
                    reason=item.get("bad_reason") or "no_video_stream",
                )
            elif src:
                def generate_requested_thumb() -> bool:
                    ok = make_thumbnail(ffmpeg, src, out, background=True)
                    if not ok:
                        mark_thumbnail_failure(
                            item,
                            reason=item.get("bad_reason") or "thumbnail_generation_failed",
                        )
                        try:
                            save_library_item(item)
                            diagnostic_emit_rate_limited(
                                "INFO",
                                "thumbnail_failure_marker_saved",
                                key=f"{cache}|{file_id}",
                                interval=30.0,
                                force=True,
                                video_id=file_id,
                                cache=cache,
                                reason=item.get("thumb_failed_reason"),
                            )
                        except Exception as exc:
                            diagnostic_error(
                                "thumbnail_failure_marker_save_failed",
                                exc,
                                video_id=file_id,
                                cache=cache,
                            )
                        return False
                    thumb_cache_invalidate(file_id, cache)
                    clear_thumbnail_failure(item)
                    item["has_thumb"] = True
                    item["thumb_v"] = thumb_version(cache, file_id)
                    try:
                        save_library_item(item)
                    except Exception as e:
                        log(f"[预览图] 单条索引保存失败: {e}")
                    return True

                submit_thumbnail_job(
                    thumbnail_job_key(cache, file_id),
                    generate_requested_thumb,
                    priority=THUMB_PRIORITY_VISIBLE,
                )
                diagnostic_aggregate("thumbnail_generation_queued")
            else:
                diagnostic_emit(
                    "WARN",
                    "thumbnail_source_unresolved",
                    force=True,
                    video_id=vid,
                    item_rel=item.get("rel"),
                    root=prefer_root,
                    operation_id=getattr(g, "_diag_operation_id", ""),
                )
        else:
            diagnostic_emit(
                "WARN",
                "thumbnail_generation_unavailable",
                force=True,
                video_id=vid,
                item_found=bool(item),
                ffmpeg_found=bool(ffmpeg),
                cache_found=bool(cache),
                root=prefer_root,
                operation_id=getattr(g, "_diag_operation_id", ""),
            )
        diagnostic_aggregate("thumbnail_placeholder")
        return _deferred_placeholder()

    item = find_video_by_id(vid, prefer_root=prefer_root)
    # 也可能用 thumb_id（碰撞重映射后）直接请求
    if not item:
        item = (STATE.get("by_thumb_id") or {}).get(vid)
    cache = cache_dir_for_item(item) or hint_cache or STATE.get("cache_dir")
    if cache is not None:
        cache = Path(cache)
    file_id = thumb_id_for_item(item) if item else vid
    if file_id != vid:
        served = _serve_thumb_jpeg(cache, file_id)
        if served is not None:
            return served
        served = _serve_thumb_jpeg(hint_cache, file_id)
        if served is not None:
            return served
    else:
        served = _serve_thumb_jpeg(cache, file_id)
        if served is not None:
            return served

    if cache:
        # 损坏或不存在：进入共享队列。页面图片使用 defer=1，立即释放
        # waitress 请求线程；直接访问该地址则短暂等待以兼容原有行为。
        ffmpeg = STATE.get("ffmpeg")
        if item and ffmpeg:
            src = _video_file_for_thumb(item)
            out = thumb_path(cache, file_id)
            if thumbnail_failure_is_current(item):
                diagnostic_aggregate("thumbnail_generation_skipped_persisted_fail")
                diagnostic_emit_rate_limited(
                    "INFO",
                    "thumbnail_generation_skipped_persisted_fail",
                    key=f"{prefer_root}|{file_id}",
                    interval=30.0,
                    force=True,
                    video_id=vid,
                    root=prefer_root,
                    reason=item.get("thumb_failed_reason") or "persisted_failure",
                )
                if defer:
                    return _deferred_placeholder()
                return jsonify({"ok": False, "msg": "该视频缩略图此前生成失败，已跳过重复尝试"}), 503
            bad_reason = str(item.get("bad_reason") or "").casefold()
            if item.get("bad") and ("无视频流" in bad_reason or "no video" in bad_reason):
                diagnostic_aggregate("thumbnail_generation_skipped_no_video_stream")
                src = None
            if src:
                def generate_requested_thumb() -> bool:
                    ok = make_thumbnail(ffmpeg, src, out, background=True)
                    if not ok:
                        mark_thumbnail_failure(
                            item,
                            reason=item.get("bad_reason") or "thumbnail_generation_failed",
                        )
                        try:
                            save_library_item(item)
                            diagnostic_emit_rate_limited(
                                "INFO",
                                "thumbnail_failure_marker_saved",
                                key=f"{cache}|{file_id}",
                                interval=30.0,
                                force=True,
                                video_id=file_id,
                                cache=cache,
                                reason=item.get("thumb_failed_reason"),
                            )
                        except Exception as exc:
                            diagnostic_error(
                                "thumbnail_failure_marker_save_failed",
                                exc,
                                video_id=file_id,
                                cache=cache,
                            )
                        return False
                    thumb_cache_invalidate(file_id, cache)
                    clear_thumbnail_failure(item)
                    item["has_thumb"] = True
                    item["thumb_v"] = thumb_version(cache, file_id)
                    try:
                        save_library_item(item)
                    except Exception as e:
                        log(f"[预览图] 单条索引保存失败: {e}")
                    if not STATE.get("scanning") and not STATE.get("updating"):
                        STATE["thumb_progress"] = (
                            f"已后台补全预览图：{item.get('name') or file_id}"
                        )
                    return True

                future = submit_thumbnail_job(
                    thumbnail_job_key(cache, file_id),
                    generate_requested_thumb,
                    priority=THUMB_PRIORITY_VISIBLE,
                )
                if defer:
                    diagnostic_aggregate("thumbnail_placeholder")
                    return _deferred_placeholder()
                try:
                    future.result(timeout=75)
                except Exception as exc:
                    diagnostic_error(
                        "thumbnail_generation_wait_failed",
                        exc,
                        video_id=vid,
                        file_id=file_id,
                        cache=cache,
                        operation_id=getattr(g, "_diag_operation_id", ""),
                    )
                served = _serve_thumb_jpeg(cache, file_id)
                if served is not None:
                    return served
            try:
                if out.exists() and not read_thumb_jpeg(cache, file_id):
                    _clear_path_attrs_windows(out)
                    out.unlink(missing_ok=True)
                    thumb_cache_invalidate(file_id)
                    log(f"[预览图] 已删除损坏缓存: {file_id}")
            except OSError as exc:
                diagnostic_error(
                    "thumbnail_corrupt_delete_failed",
                    exc,
                    video_id=vid,
                    file_id=file_id,
                    output=out,
                    operation_id=getattr(g, "_diag_operation_id", ""),
                )

    diagnostic_emit(
        "WARN",
        "thumbnail_placeholder_returned",
        force=True,
        video_id=vid,
        file_id=file_id,
        item_found=bool(item),
        cache_found=bool(cache),
        ffmpeg_found=bool(STATE.get("ffmpeg")),
        root=prefer_root,
        operation_id=getattr(g, "_diag_operation_id", ""),
    )
    _set_request_cache_layer(
        "L3_thumb_placeholder",
        cache="missing_or_generation_unavailable",
        video_id=vid,
        file_id=file_id,
    )
    return Response(placeholder, mimetype="image/svg+xml", headers=placeholder_headers)


def _playback_route_failure(
    route: str,
    reason: str,
    vid: str,
    *,
    status: int = 404,
    **fields,
) -> None:
    diagnostic_emit(
        "WARN",
        "playback_route_failed",
        force=True,
        route=route,
        reason=reason,
        video_id=vid,
        status=status,
        operation_id=getattr(g, "_diag_operation_id", ""),
        request_id=getattr(g, "_diag_request_id", ""),
        **fields,
    )


@app.route("/playlist/<vid>.m3u8")
def playlist_m3u8(vid: str):
    """HLS：支持自建 TS 合集，或磁盘上的 .m3u8（改写分片地址）。"""
    prefer_root = (request.args.get("root") or "").strip() or None
    diagnostic_call("playlist_m3u8", video_id=vid, root=prefer_root)
    item = find_video_by_id(vid, prefer_root=prefer_root)
    if not item:
        _playback_route_failure("playlist", "video_not_found", vid, root=prefer_root)
        abort(404)
    kind = item.get("kind") or ""

    if kind == "m3u8" or (item.get("ext") or "").lower() == ".m3u8":
        path = resolve_item_rel(item, item.get("rel") or "")
        if not path:
            _playback_route_failure(
                "playlist",
                "playlist_path_unresolved",
                vid,
                root=prefer_root,
                rel=item.get("rel"),
            )
            abort(404)
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            diagnostic_error(
                "playlist_read_failed",
                exc,
                video_id=vid,
                path=path,
                operation_id=getattr(g, "_diag_operation_id", ""),
            )
            abort(404)
        item_root = (item.get("_lib_root") or item.get("root") or prefer_root or "").strip()
        body = rewrite_m3u8_for_proxy(
            text,
            item["rel"],
            vid,
            item_root or None,
            getattr(g, "_diag_operation_id", "") or None,
        )
        diagnostic_emit(
            "INFO",
            "playlist_served",
            force=True,
            video_id=vid,
            kind=kind,
            path=path,
            bytes=len(body.encode("utf-8")),
            operation_id=getattr(g, "_diag_operation_id", ""),
        )
        return Response(
            body,
            mimetype="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache"},
        )

    if kind != "ts_set":
        _playback_route_failure(
            "playlist",
            "unsupported_kind",
            vid,
            kind=kind,
            ext=item.get("ext"),
        )
        abort(404)
    segments = item.get("segments") or []
    if len(segments) < 2:
        _playback_route_failure(
            "playlist",
            "insufficient_segments",
            vid,
            segment_count=len(segments),
        )
        abort(404)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:30",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    item_root = (item.get("_lib_root") or item.get("root") or prefer_root or "").strip()
    query_parts = []
    if item_root:
        query_parts.append(f"root={quote(item_root, safe='')}")
    operation_id = getattr(g, "_diag_operation_id", "")
    if operation_id:
        query_parts.append(f"op={quote(operation_id, safe='')}")
    root_q = ("?" + "&".join(query_parts)) if query_parts else ""
    for i in range(len(segments)):
        lines.append("#EXTINF:10.0,")
        lines.append(f"/stream/{vid}/seg/{i}{root_q}")
    lines.append("#EXT-X-ENDLIST")
    diagnostic_emit(
        "INFO",
        "playlist_served",
        force=True,
        video_id=vid,
        kind=kind,
        segment_count=len(segments),
        operation_id=operation_id,
    )
    return Response(
        "\n".join(lines) + "\n",
        mimetype="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache"},
    )


@app.route("/hls/<vid>/file")
def hls_proxy_file(vid: str):
    """代理 m3u8 分片/子列表（必须属于当前条目所在目录树）。"""
    prefer_root = (request.args.get("root") or "").strip() or None
    item = find_video_by_id(vid, prefer_root=prefer_root)
    if not item:
        _playback_route_failure("hls_proxy", "video_not_found", vid, root=prefer_root)
        abort(404)
    rel = (request.args.get("rel") or "").replace("\\", "/").strip("/")
    if not rel:
        _playback_route_failure("hls_proxy", "empty_relative_path", vid)
        abort(404)
    # 限制：分片须在播放列表所在目录或其子目录下
    base = str(Path((item.get("rel") or "x")).parent).replace("\\", "/")
    if base == ".":
        base = ""
    if base and not (rel == base or rel.startswith(base + "/")):
        # 也允许与 playlist 同级的相对解析结果
        pl_folder = (item.get("folder") or "").strip("/")
        if pl_folder and not (rel == pl_folder or rel.startswith(pl_folder + "/")):
            _playback_route_failure(
                "hls_proxy",
                "relative_path_outside_playlist",
                vid,
                status=403,
                rel=rel,
                playlist_folder=pl_folder,
            )
            abort(403)
    path = resolve_item_rel(item, rel)
    if not path:
        _playback_route_failure(
            "hls_proxy",
            "segment_path_unresolved",
            vid,
            rel=rel,
        )
        abort(404)
    if path.suffix.lower() == ".m3u8":
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            diagnostic_error(
                "hls_playlist_read_failed",
                exc,
                video_id=vid,
                path=path,
                operation_id=getattr(g, "_diag_operation_id", ""),
            )
            abort(404)
        item_root = (item.get("_lib_root") or item.get("root") or prefer_root or "").strip()
        body = rewrite_m3u8_for_proxy(
            text,
            rel,
            vid,
            item_root or None,
            getattr(g, "_diag_operation_id", "") or None,
        )
        return Response(
            body,
            mimetype="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache"},
        )
    mime = mimetypes.guess_type(str(path))[0] or "video/mp2t"
    return _stream_file(path, mime)


@app.route("/stream/<vid>/seg/<int:idx>")
def stream_seg(vid: str, idx: int):
    prefer_root = (request.args.get("root") or "").strip() or None
    item = find_video_by_id(vid, prefer_root=prefer_root)
    if not item or item.get("kind") != "ts_set":
        _playback_route_failure(
            "stream_segment",
            "video_not_found_or_wrong_kind",
            vid,
            root=prefer_root,
            kind=item.get("kind") if item else "",
            segment_index=idx,
        )
        abort(404)
    segments = item.get("segments") or []
    if idx < 0 or idx >= len(segments):
        _playback_route_failure(
            "stream_segment",
            "segment_index_out_of_range",
            vid,
            segment_index=idx,
            segment_count=len(segments),
        )
        abort(404)
    path = resolve_item_rel(item, segments[idx])
    if not path:
        _playback_route_failure(
            "stream_segment",
            "segment_path_unresolved",
            vid,
            segment_index=idx,
            rel=segments[idx],
        )
        abort(404)
    return _stream_file(path, "video/mp2t")


@app.route("/stream/<vid>")
def stream(vid: str):
    prefer_root = (request.args.get("root") or "").strip() or None
    diagnostic_call("stream", video_id=vid, root=prefer_root)
    item = find_video_by_id(vid, prefer_root=prefer_root)
    if not item:
        _playback_route_failure("stream", "video_not_found", vid, root=prefer_root)
        abort(404)
    # 分片集合：单文件直链播第一段（预览/兼容）；完整观看用 /playlist/
    path = resolve_item_rel(item, item.get("rel") or "")
    if not path:
        _playback_route_failure(
            "stream",
            "video_path_unresolved",
            vid,
            root=prefer_root,
            rel=item.get("rel"),
            kind=item.get("kind"),
        )
        abort(404)
    mime = mimetypes.guess_type(str(path))[0] or "video/mp4"
    diagnostic_emit(
        "INFO",
        "stream_resolved",
        force=True,
        video_id=vid,
        path=path,
        mime=mime,
        root=prefer_root,
        kind=item.get("kind"),
        ext=item.get("ext"),
        operation_id=getattr(g, "_diag_operation_id", ""),
    )
    return _stream_file(path, mime)


@app.route("/api/info/<vid>")
def api_info(vid: str):
    started = time.perf_counter()
    prefer_root = (request.args.get("root") or "").strip() or None
    diagnostic_call("api_info", video_id=vid, root=prefer_root)
    if prefer_root:
        ensure_library(prefer_root)
    item = find_video_by_id(vid, prefer_root=prefer_root)
    if not item:
        _playback_route_failure("api_info", "video_not_found", vid, root=prefer_root)
        abort(404)
    # Only lazily probe metadata dimensions explicitly enabled in Settings.
    # Skip if duration/audio is already in the index from a previous session.
    want_duration = probe_duration_enabled()
    want_audio = probe_audio_enabled()
    need_probe = bool(STATE.get("ffmpeg")) and _needs_metadata_probe(
        item,
        want_duration=want_duration,
        want_audio=want_audio,
    )
    metadata_changed = False
    if need_probe:
        path = _item_probe_path(item)
        if path and path.is_file() and path.suffix.lower() != ".m3u8":
            info = probe_media_info(
                STATE["ffmpeg"],
                path,
                include_duration=want_duration,
                include_audio=want_audio,
            )
            _apply_probe_to_item(
                item,
                info,
                include_duration=want_duration,
                include_audio=want_audio,
            )
            metadata_changed = True
        elif not path or not path.is_file():
            kind = item.get("kind") or ""
            if kind not in ("m3u8", "ts_set") and (item.get("ext") or "").lower() != ".m3u8":
                item["probe_ver"] = PROBE_META_VER
                if want_duration:
                    item["probe_duration_done"] = True
                if want_audio:
                    item["probe_audio_done"] = True
                item["bad"] = True
                item["bad_reason"] = "文件不存在"
                diagnostic_emit(
                    "WARN",
                    "video_marked_bad",
                    force=True,
                    video_id=vid,
                    reason="文件不存在",
                    rel=item.get("rel"),
                    root=prefer_root,
                    operation_id=getattr(g, "_diag_operation_id", ""),
                )
                metadata_changed = True
    if metadata_changed:
        try:
            save_library_item(item)
        except Exception as exc:
            diagnostic_error(
                "video_metadata_save_failed",
                exc,
                video_id=vid,
                operation_id=getattr(g, "_diag_operation_id", ""),
            )
    payload = {k: v for k, v in item.items() if k not in ("_q", "segments", "_lib_root", "_lib_cache")}
    # 本地路径（供复制 / 系统播放器）
    local = _local_path_for_item(item)
    payload["path"] = str(local) if local else ""
    if item.get("_lib_root"):
        payload["root"] = item["_lib_root"]
    elif STATE.get("root"):
        payload["root"] = str(STATE["root"])
    ext = (item.get("ext") or "").lower()
    kind = item.get("kind") or ""
    payload["browser_ok"] = (
        kind in ("m3u8", "ts_set")
        or ext in BROWSER_FRIENDLY_EXTS
    )
    payload["browser_hard"] = ext in BROWSER_HARD_EXTS and kind not in ("m3u8", "ts_set")
    payload["audio_codec"] = item.get("audio_codec") or ""
    payload["audio_hard"] = bool(item.get("audio_hard"))
    if kind == "ts_set":
        payload["seg_count"] = item.get("seg_count") or len(item.get("segments") or [])
        payload["kind"] = "ts_set"
    diagnostic_perf(
        "api_video_info",
        (time.perf_counter() - started) * 1000.0,
        force=True,
        video_id=vid,
        root=prefer_root,
        rel=item.get("rel"),
        kind=kind,
        ext=ext,
        bad=bool(item.get("bad")),
        bad_reason=item.get("bad_reason"),
        has_thumb=bool(item.get("has_thumb")),
        thumb_id=item.get("thumb_id") or thumb_id_for_item(item),
        browser_ok=payload["browser_ok"],
        browser_hard=payload["browser_hard"],
        need_probe=need_probe,
        metadata_changed=metadata_changed,
        local_path=local,
        local_exists=bool(local and local.is_file()),
        operation_id=getattr(g, "_diag_operation_id", ""),
    )
    return jsonify(payload)


def _local_path_for_item(item: dict) -> Path | None:
    """Compatibility wrapper for local-item actions still hosted in web.py."""
    return resolve_local_path(item)


def _client_play_url(vid: str, item: dict, prefer_root: str | None) -> str:
    """Absolute HTTP URL that another device can open in its own player."""
    from urllib.parse import quote

    kind = item.get("kind") or ""
    query = f"?root={quote(prefer_root)}" if prefer_root else ""
    if kind in ("m3u8", "ts_set"):
        path = f"/playlist/{vid}.m3u8{query}"
    else:
        path = f"/stream/{vid}{query}"
    return request.url_root.rstrip("/") + path


@app.route("/api/local/<vid>", methods=["POST"])
def api_local(vid: str):
    """本机操作：open=系统播放器打开，reveal=资源管理器定位，path=仅返回路径。

    非本机（局域网访客）不能在服务端调起播放器/资源管理器；open/path 改为返回
    可在访客本机打开的播放地址。
    """
    from vg.lan import is_local_client

    data = request.get_json(silent=True) or {}
    prefer_root = (
        request.args.get("root") or data.get("root") or ""
    ).strip() or None
    item = find_video_by_id(vid, prefer_root=prefer_root)
    if not item:
        return jsonify({"ok": False, "msg": "未找到视频"}), 404
    action = (data.get("action") or "path").strip().lower()
    path = _local_path_for_item(item)
    if not path:
        return jsonify({"ok": False, "msg": "文件不存在"}), 404
    path_str = str(path)
    local_client = is_local_client(_client_ip())
    play_url = _client_play_url(vid, item, prefer_root)

    if not local_client:
        if action == "reveal":
            return jsonify({
                "ok": False,
                "remote": True,
                "url": play_url,
                "msg": "「打开位置」只能在跑服务的电脑上使用",
            }), 400
        if action in ("open", "path"):
            return jsonify({
                "ok": True,
                "remote": True,
                "url": play_url,
                "path": play_url,
                "title": item.get("name") or item.get("filename") or vid,
                "msg": (
                    "局域网访问：请先运行本机播放助手，将用系统默认播放器打开"
                    if action == "open"
                    else "已返回局域网播放地址（非服务端磁盘路径）"
                ),
            })
        return jsonify({"ok": False, "msg": "未知操作"}), 400

    if action == "path":
        return jsonify({"ok": True, "path": path_str, "remote": False})

    if action == "open":
        try:
            if sys.platform == "win32":
                os.startfile(path_str)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path_str])
            else:
                subprocess.Popen(["xdg-open", path_str])
            log(f"[本地] 已用系统播放器打开: {path_str}")
            return jsonify({"ok": True, "path": path_str, "remote": False, "msg": "已调用系统播放器"})
        except Exception as e:
            return jsonify({"ok": False, "msg": f"打开失败: {e}", "path": path_str}), 500

    if action == "reveal":
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", path_str])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path_str])
            else:
                subprocess.Popen(["xdg-open", str(path.parent)])
            return jsonify({"ok": True, "path": path_str, "remote": False, "msg": "已在文件夹中显示"})
        except Exception as e:
            return jsonify({"ok": False, "msg": f"定位失败: {e}", "path": path_str}), 500

    return jsonify({"ok": False, "msg": "未知操作"}), 400


@app.route("/api/series/<sid>")
def api_series(sid: str):
    """合集分集列表。"""
    if not re.fullmatch(r"s[a-f0-9]{15}", sid or ""):
        return jsonify({"ok": False, "msg": "无效合集 id"}), 400
    items = series_episodes(videos_for_scope(None), sid)
    if not items:
        return jsonify({"ok": False, "msg": "未找到合集"}), 404
    slim = []
    for v in items:
        row = {k: v[k] for k in v if k not in ("segments", "_q")}
        attach_thumb_meta(row)
        slim.append(row)
    title = items[0].get("series_title") or items[0].get("name") or ""
    return jsonify({
        "ok": True,
        "series_id": sid,
        "title": title,
        "count": len(slim),
        "episodes": slim,
    })


def _paths_for_delete(item: dict) -> list[Path]:
    """待删除的实体路径（ts_set 删全部分片）。"""
    kind = item.get("kind") or ""
    if kind == "ts_set" and item.get("segments"):
        out = []
        for rel in item["segments"]:
            p = resolve_item_rel(item, rel)
            if p and p.is_file():
                out.append(p)
        return out
    p = _local_path_for_item(item)
    return [p] if p and p.is_file() else []


@app.route("/api/delete", methods=["POST"])
@_serialized(_delete_lock)
def api_delete():
    """移到回收站并从所属磁盘索引移除。

    Preferred body: {items: [{id, root, rel}], trash: true}. ``ids`` remains
    supported for older clients, but root+rel is required to disambiguate an
    id collision across disks.
    """
    data = request.get_json(silent=True) or {}
    requested = data.get("items")
    legacy_ids = not isinstance(requested, list)
    if not isinstance(requested, list):
        ids = data.get("ids") or []
        requested = [{"id": vid} for vid in ids] if isinstance(ids, list) else []
    if not requested:
        return jsonify({"ok": False, "msg": "请选择要删除的条目"}), 400
    if data.get("trash") is False:
        return jsonify({"ok": False, "msg": "仅支持移到回收站，请勿关闭 trash"}), 400
    if not (STATE.get("root") or get_mounted_roots()):
        return jsonify({"ok": False, "msg": "尚未选择盘符"}), 400
    if legacy_ids and len(get_mounted_roots()) > 1:
        return jsonify({"ok": False, "msg": "多盘删除必须携带每项的 root 和 rel"}), 400

    removed = []
    errors = []
    catalog = list(videos_for_scope(None))
    removed_keys: set[tuple[str, str]] = set()
    affected_roots: set[str] = set()

    def _root_s(item: dict) -> str:
        raw = (item.get("_lib_root") or item.get("root") or "").strip()
        if not raw:
            return ""
        try:
            return str(Path(raw).expanduser().resolve())
        except OSError:
            return raw

    def _rel_key(item: dict) -> str:
        return (item.get("rel") or "").replace("\\", "/").strip("/").casefold()

    for raw in requested:
        req = raw if isinstance(raw, dict) else {"id": raw}
        vid = str(req.get("id") or "")
        if not re.fullmatch(r"[a-f0-9]{16}", vid):
            errors.append({"id": vid, "msg": "无效 id"})
            continue
        prefer_root = str(req.get("root") or "").strip()
        prefer_rel = str(req.get("rel") or "").replace("\\", "/").strip("/").casefold()
        if len(get_mounted_roots()) > 1 and (not prefer_root or not prefer_rel):
            errors.append({"id": vid, "msg": "多盘删除缺少 root 或 rel"})
            continue
        item = None
        for candidate in catalog:
            if prefer_root:
                try:
                    same_root = _root_s(candidate).lower() == str(Path(prefer_root).expanduser().resolve()).lower()
                except OSError:
                    same_root = _root_s(candidate).lower() == prefer_root.lower()
                if not same_root:
                    continue
            if (
                prefer_rel
                and _rel_key(candidate) == prefer_rel
                and (candidate.get("id") == vid or candidate.get("_thumb_id") == vid)
            ):
                item = candidate
                break
            if not prefer_rel and (
                candidate.get("id") == vid or candidate.get("_thumb_id") == vid
            ):
                item = candidate
                break
        if item is None:
            item = find_video_by_id(vid, prefer_root=prefer_root or None)
        if not item:
            errors.append({"id": vid, "msg": "未找到"})
            continue
        item_root = _root_s(item)
        if prefer_root and item_root:
            try:
                wanted_root = str(Path(prefer_root).expanduser().resolve())
            except OSError:
                wanted_root = prefer_root
            if item_root.lower() != wanted_root.lower():
                errors.append({"id": vid, "msg": "磁盘归属不匹配"})
                continue
        if not item_root:
            errors.append({"id": vid, "msg": "缺少磁盘归属，未删除"})
            continue

        paths = _paths_for_delete(item)
        if not paths:
            errors.append({"id": vid, "msg": "文件不存在"})
            # 仍从索引去掉
            removed.append(vid)
            removed_keys.add((item_root.lower(), _rel_key(item)))
            affected_roots.add(item_root)
            continue
        ok_all = True
        for p in paths:
            ok, msg = move_to_trash(p)
            if not ok:
                ok_all = False
                errors.append({"id": vid, "msg": msg, "path": str(p)})
                break
        if ok_all:
            removed.append(vid)
            removed_keys.add((item_root.lower(), _rel_key(item)))
            affected_roots.add(item_root)
            thumb_cache_invalidate(item.get("_thumb_id") or item.get("id") or vid)

    def _was_removed(item: dict) -> bool:
        return (_root_s(item).lower(), _rel_key(item)) in removed_keys

    remaining = [v for v in catalog if not _was_removed(v)]
    STATE["videos"] = remaining
    for root_s in affected_roots:
        try:
            root_catalog = read_root_library(root_s)
            if root_catalog is None:
                root_catalog = [
                    v for v in catalog if _root_s(v).lower() == root_s.lower()
                ]
            save_root_library(
                root_s,
                [v for v in root_catalog if not _was_removed(v)],
            )
        except Exception as e:
            errors.append({"root": root_s, "msg": f"索引保存失败: {e}"})

    if get_mounted_roots():
        publish_unified_library()
    else:
        rebuild_indexes(remaining)

    return jsonify({
        "ok": True,
        "removed": removed,
        "errors": errors,
        "msg": f"已移除 {len(removed)} 项" + (f"，{len(errors)} 项失败" if errors else ""),
    })


@app.route("/api/thumb/<vid>", methods=["POST"])
def api_thumb_set(vid: str):
    """换封面：JSON {seek:秒} 截帧，或 multipart 上传图片字段 file。"""
    if not re.fullmatch(r"[a-f0-9]{16}", vid or ""):
        return jsonify({"ok": False, "msg": "无效 id"}), 400
    prefer_root = (request.args.get("root") or "").strip() or None
    item = find_video_by_id(vid, prefer_root=prefer_root)
    if not item:
        return jsonify({"ok": False, "msg": "未找到视频"}), 404
    cache = cache_dir_for_item(item) or STATE.get("cache_dir")
    if not cache:
        return jsonify({"ok": False, "msg": "无缓存目录"}), 400
    file_id = thumb_id_for_item(item) or vid
    out = thumb_path(cache, file_id)

    # 上传图片
    if request.files.get("file"):
        f = request.files["file"]
        raw = f.read()
        if not raw:
            return jsonify({"ok": False, "msg": "空文件"}), 400
        # 简单校验；非 jpeg 时尝试用 ffmpeg 转一下
        if raw[:2] != b"\xff\xd8":
            ffmpeg = STATE.get("ffmpeg")
            if not ffmpeg:
                return jsonify({"ok": False, "msg": "请上传 JPEG，或安装 ffmpeg 以转换"}), 400
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                src = Path(td) / "in.bin"
                jpg = Path(td) / "out.jpg"
                src.write_bytes(raw)
                try:
                    r = subprocess.run(
                        [ffmpeg, "-y", "-i", str(src), "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", str(jpg)],
                        capture_output=True,
                        timeout=30,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
                    )
                    if r.returncode != 0 or not jpg.is_file():
                        return jsonify({"ok": False, "msg": "无法解析图片"}), 400
                    raw = jpg.read_bytes()
                except Exception as e:
                    return jsonify({"ok": False, "msg": f"转码失败: {e}"}), 400
        if not save_thumbnail_jpeg(out, raw):
            return jsonify({"ok": False, "msg": "写入失败"}), 500
        thumb_cache_invalidate(file_id)
        clear_thumbnail_failure(item)
        item["has_thumb"] = True
        item["thumb_v"] = thumb_version(cache, file_id) or int(datetime.now().timestamp())
        save_library_item(item)
        return jsonify({"ok": True, "msg": "封面已更新", "thumb_v": item["thumb_v"], "thumb_id": file_id})

    data = request.get_json(silent=True) or {}
    seek = data.get("seek", 3.0)
    try:
        seek = float(seek)
    except (TypeError, ValueError):
        seek = 3.0
    seek = max(0.0, min(seek, 36000.0))
    ffmpeg = STATE.get("ffmpeg")
    if not ffmpeg:
        return jsonify({"ok": False, "msg": "未找到 ffmpeg"}), 400
    src = _video_file_for_thumb(item)
    if not src:
        return jsonify({"ok": False, "msg": "无法定位视频文件"}), 404
    # 若传 current：用播放器当前时间（前端传入）
    # This route is an explicit user retry; clear the persisted negative
    # marker before forcing a new ffmpeg attempt.
    clear_thumbnail_failure(item)
    ok = make_thumbnail(ffmpeg, src, out, seek=seek, force=True)
    if not ok:
        mark_thumbnail_failure(item, reason="explicit_retry_failed")
        try:
            save_library_item(item)
            diagnostic_emit(
                "INFO",
                "thumbnail_failure_marker_saved",
                force=True,
                video_id=file_id,
                cache=cache,
                reason="explicit_retry_failed",
                explicit_retry=True,
            )
        except Exception as exc:
            diagnostic_error(
                "thumbnail_failure_marker_save_failed",
                exc,
                video_id=file_id,
                cache=cache,
            )
        return jsonify({"ok": False, "msg": "截帧失败"}), 500
    thumb_cache_invalidate(file_id)
    item["has_thumb"] = True
    item["thumb_v"] = thumb_version(cache, file_id) or int(datetime.now().timestamp())
    item["thumb_seek"] = seek
    save_library_item(item)
    return jsonify({
        "ok": True,
        "msg": f"已截取 {seek:.1f}s 处画面为封面",
        "thumb_v": item["thumb_v"],
        "thumb_id": file_id,
        "seek": seek,
    })


from vg.routes import register_feature_routes

register_feature_routes(app)
