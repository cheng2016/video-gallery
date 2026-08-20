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
from vg.taxonomy import ensure_video_taxonomy, taxonomy_facets
from vg.thumb_jobs import (
    THUMB_PRIORITY_VISIBLE,
    note_frontend_activity,
    submit_thumbnail_job,
    thumbnail_job_key,
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
    call as diagnostic_call,
    emit as diagnostic_emit,
    emit_rate_limited as diagnostic_emit_rate_limited,
    error as diagnostic_error,
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
    return (
        int(STATE.get("lib_gen") or 0),
        id(STATE.get("videos")),
        _stable_request_args(),
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
    return None


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
    note_frontend_activity(0.45)
    key = (
        int(STATE.get("lib_gen") or 0),
        id(STATE.get("videos")),
        _stable_request_args(),
    )
    g._video_response_cache_key = key
    cached = _cached_video_response(key)
    if cached is None:
        diagnostic_aggregate("api_videos_response_cache_miss")
        return None
    diagnostic_aggregate("api_videos_response_cache_hit")
    body, status, mimetype = cached
    return Response(body, status=status, mimetype=mimetype)


@app.before_request
def _let_foreground_requests_preempt_thumbnails():
    """Give list rendering and playback a quiet window between ffmpeg jobs."""
    if request.method != "GET":
        return None
    path = request.path or ""
    if path.startswith(("/stream/", "/playlist/", "/hls/")):
        note_frontend_activity(4.0)
    elif path in ("/", "/api/tree", "/api/videos", "/api/videos-by-ids") or path.startswith(
        ("/thumb/", "/api/series/")
    ):
        note_frontend_activity(0.45)
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
    resp.headers["X-VG-Request-ID"] = rid
    if operation_id:
        resp.headers["X-VG-Operation-ID"] = operation_id
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
def _api_no_store(resp):
    """局域网 IP 下浏览器常缓存 GET /api/*；本机 127.0.0.1 往往不缓存，会造成「左边对右边数量错」。"""
    if (request.path or "").startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


@app.after_request
def _cache_videos_response(resp):
    if request.method == "GET" and request.path == "/api/videos":
        _store_video_response(_video_response_cache_key(), resp)
    return resp


@app.route("/")
def index():
    """返回页面；用字符串注入盘符 JSON，不依赖 Jinja，避免 {{ }} 原样显示。"""
    html_path = APP_DIR / "templates" / "index.html"
    html = html_path.read_text(encoding="utf-8")
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
            {"drives": drives, "current": current, "scanning": STATE["scanning"]},
            ensure_ascii=False,
        )
    except Exception as e:
        print(f"【错误】页面注入盘符失败: {e}")
        payload = '{"drives":[],"current":"","scanning":false}'
    boot = f"<script>window.__BOOT_DRIVES__ = {payload};</script>"
    if "</head>" in html:
        html = html.replace("</head>", boot + "\n</head>", 1)
    else:
        html = boot + html
    return Response(html, mimetype="text/html; charset=utf-8")


@app.route("/api/client-log", methods=["POST"])
def api_client_log():
    """Receive ordered browser actions/errors so one operation can be traced end-to-end."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "msg": "日志格式错误"}), 400
    event = str(data.get("event") or "").strip()[:80]
    if not event or not re.fullmatch(r"[A-Za-z0-9_.:-]+", event):
        return jsonify({"ok": False, "msg": "事件名错误"}), 400
    raw_fields = data.get("fields")
    fields: dict[str, object] = {}
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
                fields[safe_key] = str(value).replace("\r", " ").replace("\n", " ")[:500]
    operation_id = str(data.get("operation_id") or _operation_id()).strip()[:64]
    level = str(data.get("level") or "INFO").upper()
    if level not in {"INFO", "WARN", "ERROR"}:
        level = "INFO"
    if event.startswith("player_") and event not in {
        "player_error",
        "player_play_rejected",
        "player_opened",
        "player_closed",
        "player_source_set",
    }:
        diagnostic_aggregate("client_player_noise")
        return jsonify({"ok": True})
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
        force=True,
        action=event,
        operation_id=operation_id,
        request_id=getattr(g, "_diag_request_id", ""),
        client_ip=_client_ip(),
        page=str(data.get("page") or "")[:200],
        **fields,
    )
    return jsonify({"ok": True})


def _tree_cache_key(lib: str) -> tuple:
    return (
        int(STATE.get("lib_gen") or 0),
        id(STATE.get("videos")),
        (lib or "").strip().casefold(),
        bool(STATE.get("scanning")),
        bool(STATE.get("updating")),
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
    scope_started = time.perf_counter()
    videos = videos_for_scope(lib or None)
    scope_ms = (time.perf_counter() - scope_started) * 1000.0
    tree_started = time.perf_counter()
    tree = tree_for_scope(lib or None)
    tree_ms = (time.perf_counter() - tree_started) * 1000.0

    # Prefer precomputed facets for the unified catalog; scoped views recompute.
    # Also reject cache when it disagrees with the folder tree (same video source).
    facets = STATE.get("facets") or {}
    tree_count = int((tree or {}).get("count") or 0)
    use_cached = (
        not lib
        and facets
        and int(facets.get("count") or -1) == len(videos)
        and int(facets.get("count") or -1) == tree_count
        and not STATE.get("scanning")
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
        type_counts: dict[str, int] = {}
        cat_counts: dict[str, int] = {}
        genre_counts: dict[str, int] = {}
        for v in videos:
            ext = (v.get("ext") or "").lower() or "unknown"
            type_counts[ext] = type_counts.get(ext, 0) + 1
            cat = _video_category(v)
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
            for g in ensure_video_genres(v):
                genre_counts[g] = genre_counts.get(g, 0) + 1
        types = [
            {"ext": ext, "count": cnt, "label": ext.lstrip(".").upper() or "未知"}
            for ext, cnt in sorted(type_counts.items(), key=lambda x: (-x[1], x[0]))
        ]
        genre_order = {name: i for i, (name, _) in enumerate(GENRE_DEFS)}
        genres = [
            {"id": name, "name": name, "count": cnt}
            for name, cnt in sorted(
                genre_counts.items(),
                key=lambda x: (genre_order.get(x[0], 999), -x[1], x[0]),
            )
            if cnt > 0
        ]
        categories = build_category_facets(cat_counts)
        themes = taxonomy_facets(videos, "themes")
        backgrounds = taxonomy_facets(videos, "backgrounds")
        count = len(videos)
    facets_ms = (time.perf_counter() - facets_started) * 1000.0
    mounts_started = time.perf_counter()
    mounts = roots_summary(all_videos)
    mounts_ms = (time.perf_counter() - mounts_started) * 1000.0
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
    heavy = _cached_tree_payload(cache_key)
    cache_result = "hit" if heavy is not None else "miss"
    if heavy is None:
        heavy = _build_tree_payload(lib)
        if not STATE.get("scanning") and not STATE.get("updating"):
            _store_tree_payload(cache_key, heavy)

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
        force=True,
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
    videos = list(videos_for_scope(lib or None))
    videos = filter_videos_by_scope(videos, category=category)

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

    if folder:
        has_children = bool(_subfolder_facets(videos, "", folder))
        videos = filter_videos_by_scope(
            videos,
            folder=folder,
            include_descendants=not (has_children and not folder_all),
        )
    if ext:
        ext_n = ext if ext.startswith(".") else "." + ext
        videos = [v for v in videos if (v.get("ext") or "").lower() == ext_n]
    if q_raw:
        videos = [v for v in videos if video_matches_query(v, parsed, _video_search_text)]

    scoped_genres = _genre_facets(videos)
    scoped_themes = taxonomy_facets(videos, "themes")
    scoped_backgrounds = taxonomy_facets(videos, "backgrounds")
    scoped_subs = _subfolder_facets(videos, category, folder)

    if genre:
        videos = [v for v in videos if genre in ensure_video_genres(v)]
    if theme:
        videos = [v for v in videos if theme in ensure_video_taxonomy(v)[0]]
    if background:
        videos = [v for v in videos if background in ensure_video_taxonomy(v)[1]]

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

    videos = sorted(videos, key=key_fn, reverse=reverse)
    return (
        videos,
        raw_count,
        scoped_genres,
        scoped_themes,
        scoped_backgrounds,
        scoped_subs,
        subfolder_levels,
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
    if sql_eligible:
        from vg.catalog_db import (
            merge_catalog_facets,
            query_catalog_facets,
            query_catalog_page,
            query_catalogs_page,
        )

        sql_caches = _catalog_caches_for_roots(sql_roots)
        if not sql_caches:
            sql_skip_reason = "no_catalog_cache"
            sql_eligible = False
        else:
            # Match Python scope: exact folder only when user unchecked「全部」
            # on a folder that still has children; otherwise include descendants.
            include_descendants = True
            if folder and not folder_all:
                has_children = False
                for cache in sql_caches:
                    probe = query_catalog_facets(
                        cache,
                        category=category,
                        folder=folder,
                        include_descendants=True,
                        ext=ext,
                        search=q_raw,
                    )
                    if probe.get("subfolders"):
                        has_children = True
                        break
                include_descendants = not has_children

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
            slim = []
            for video in page:
                enriched = dict(video)
                if not enriched.get("root"):
                    enriched["root"] = lib or enriched.get("_lib_root") or ""
                if lib and not enriched.get("root"):
                    enriched["root"] = lib
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
            if offset == 0:
                facet_rows = [
                    query_catalog_facets(
                        cache,
                        category=category,
                        folder=folder,
                        include_descendants=include_descendants,
                        ext=ext,
                        search=q_raw,
                    )
                    for cache in sql_caches
                ]
                facets = (
                    facet_rows[0]
                    if len(facet_rows) == 1
                    else merge_catalog_facets(facet_rows)
                )
            else:
                facets = {"genres": [], "themes": [], "backgrounds": [], "subfolders": []}
            facets_ms = (time.perf_counter() - facet_started) * 1000.0
            levels = []
            if offset == 0 and facets["subfolders"]:
                levels = [{
                    "label": "子类",
                    "prefix": folder or category,
                    "all_id": "" if not folder else folder,
                    "selected": "",
                    "items": facets["subfolders"],
                }]
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
                "subfolder_levels": levels,
                "view": "flat",
                "lib": lib,
                "facets_included": offset == 0,
            })
            serialize_ms = (time.perf_counter() - serialize_started) * 1000.0
            diagnostic_perf(
                "api_videos_sql",
                (time.perf_counter() - api_started) * 1000.0,
                force=True,
                rows=len(slim),
                total_rows=total,
                offset=offset,
                caches=len(sql_caches),
                sql_ms=f"{sql_ms:.1f}",
                facets_ms=f"{facets_ms:.1f}",
                thumb_meta_ms=f"{thumb_meta_ms:.1f}",
                serialize_ms=f"{serialize_ms:.1f}",
                response_bytes=response.calculate_content_length(),
                category=category or "all",
                folder=folder,
                sort=sort,
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
        id(STATE.get("videos")),
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
    payload = {
        "videos": slim,
        "count": total,
        "raw_count": raw_count,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(slim) < total,
        "genres": scoped_genres,
        "themes": scoped_themes,
        "backgrounds": scoped_backgrounds,
        "subfolders": scoped_subs,
        "subfolder_levels": subfolder_levels,
        "view": view if view in ("series", "flat") else "flat",
        "lib": lib,
    }
    serialize_started = time.perf_counter()
    response = jsonify(payload)
    serialize_ms = (time.perf_counter() - serialize_started) * 1000.0
    total_ms = (time.perf_counter() - api_started) * 1000.0
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
        cache=query_cache,
        response_bytes=response.calculate_content_length(),
        category=category or "all",
        folder=folder,
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
    try:
        root = Path(path).expanduser().resolve()
    except OSError as e:
        return jsonify({"ok": False, "msg": f"路径无效: {e}"}), 400
    if not root.is_dir():
        return jsonify({"ok": False, "msg": f"目录不存在: {root}"}), 400

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
        note_frontend_activity(0.8)
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
            if src:
                def generate_requested_thumb() -> bool:
                    ok = make_thumbnail(ffmpeg, src, out, background=True)
                    if not ok:
                        return False
                    thumb_cache_invalidate(file_id, cache)
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
            if src:
                def generate_requested_thumb() -> bool:
                    ok = make_thumbnail(ffmpeg, src, out, background=True)
                    if not ok:
                        return False
                    thumb_cache_invalidate(file_id, cache)
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
    ok = make_thumbnail(ffmpeg, src, out, seek=seek, force=True)
    if not ok:
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
