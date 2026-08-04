# -*- coding: utf-8 -*-
"""Flask application and HTTP routes."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re


import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

try:
    from flask import Flask, Response, abort, jsonify, render_template, request, send_file
except ImportError:
    print("=" * 50)
    print("【错误】未安装依赖 Flask")
    print("请在本目录运行:")
    print(r'  .venv\Scripts\pip.exe install -r requirements.txt')
    print("或重新双击 start.bat")
    print("=" * 50)
    input("按回车键退出…")
    sys.exit(1)

from vg.cache import (
    attach_thumb_meta,
    read_thumb_jpeg,
    thumb_cache_invalidate,
    thumb_path,
    thumb_version,
)
from vg.config import (
    APP_DIR,
    BROWSER_FRIENDLY_EXTS,
    BROWSER_HARD_EXTS,
    GENRE_DEFS,
    PROBE_META_VER,
    STATIC_EXPORT_DIRNAME,
)
from vg.convert import (
    _convert_worker,
    _fix_audio_worker,
    _kill_convert_proc,
)
from vg.drives import list_drives_info
from vg.export import export_static_site
from vg.genres import ensure_video_genres
from vg.media import (
    _apply_probe_to_item,
    _item_probe_path,
    _video_file_for_thumb,
    make_thumbnail,
    probe_media_info,
)
from vg.scan import (
    _video_category,
    _video_search_text,
    find_video_by_id,
    rebuild_indexes,
    start_scan,
)
from vg.state import STATE, _convert_lock
from vg.streaming import _stream_file, rewrite_m3u8_for_proxy
from vg.util import (
    _clear_path_attrs_windows,
    log,
    resolve_under_root,
    resolve_video_path,
)

app = Flask(__name__, template_folder=str(APP_DIR / "templates"))

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


@app.route("/api/tree")
def api_tree():
    root = STATE["root"]
    videos = STATE["videos"]
    facets = STATE.get("facets")
    if not facets or STATE.get("scanning"):
        # 扫描中或尚未建索引：即时统计（或触发一次重建）
        if videos and not facets:
            rebuild_indexes(videos)
            facets = STATE.get("facets")
    if facets and not STATE.get("scanning"):
        types = facets.get("types") or []
        genres = facets.get("genres") or []
        categories = facets.get("categories") or []
        count = facets.get("count", len(videos))
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
        prefer = ["电影", "电视剧", "综艺", "动漫", "少儿", "纪录片", "短剧", "体育", "音乐", "教育", "其他", ""]
        prefer_rank = {n: i for i, n in enumerate(prefer)}

        def cat_sort_key(item: tuple[str, int]):
            name, cnt = item
            return (prefer_rank.get(name, 100), -cnt, name.lower())

        categories = []
        for name, cnt in sorted(cat_counts.items(), key=cat_sort_key):
            categories.append({
                "id": name,
                "name": "未分类" if name == "" else name,
                "count": cnt,
            })
        count = len(videos)

    return jsonify({
        "tree": STATE["tree"],
        "types": types,
        "genres": genres,
        "categories": categories,
        "scanning": STATE["scanning"],
        "updating": bool(STATE.get("updating")),
        "exporting": bool(STATE.get("exporting")),
        "export_msg": STATE.get("export_msg") or "",
        "export_path": STATE.get("export_path") or "",
        "export_ok": STATE.get("export_ok"),
        "scan_progress": STATE["scan_progress"],
        "thumb_progress": STATE["thumb_progress"],
        "meta_progress": STATE.get("meta_progress") or "",
        "count": count,
        "has_ffmpeg": bool(STATE["ffmpeg"]),
        "root": str(root) if root else "",
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


@app.route("/api/videos")
def api_videos():
    folder = request.args.get("folder", "").strip().strip("/")
    category = request.args.get("category", "").strip().strip("/")
    genre = request.args.get("genre", "").strip()
    q = request.args.get("q", "").strip().lower()
    ext = request.args.get("ext", "").strip().lower()
    sort = request.args.get("sort", "mtime_desc").strip().lower()

    # 优先用预建频道索引，避免每次全表拷贝+过滤
    by_cat = STATE.get("by_category") or {}
    if category == "__root__":
        videos = list(by_cat.get("", []) or [
            v for v in STATE["videos"] if not (v.get("folder") or "").strip("/")
        ])
    elif category and category in by_cat:
        videos = list(by_cat[category])
    elif category:
        videos = [v for v in STATE["videos"] if _video_category(v) == category]
    else:
        videos = list(STATE["videos"])

    # 多层子类用「频道内全部片」统计各层兄弟项
    cat_videos = videos
    if ext or q:
        cat_videos = list(videos)
        if ext:
            ext_n = ext if ext.startswith(".") else "." + ext
            cat_videos = [v for v in cat_videos if (v.get("ext") or "").lower() == ext_n]
        if q:
            cat_videos = [v for v in cat_videos if q in _video_search_text(v)]

    subfolder_levels = _subfolder_levels(cat_videos, category, folder)

    if folder:
        folder = folder.replace("\\", "/")
        # 有子分类时：folder_all=1（默认）含子目录全部；取消全部则只看本层根目录
        # has_children 用未按搜索/格式收窄的频道列表，避免搜索时误判无子类
        folder_all = request.args.get("folder_all", "").strip() in ("1", "true", "yes")
        has_children = bool(_subfolder_facets(videos, "", folder))
        if has_children and not folder_all:
            videos = [
                v for v in videos
                if (v.get("folder") or "").replace("\\", "/") == folder
            ]
        else:
            videos = [
                v for v in videos
                if (v.get("folder") or "").replace("\\", "/") == folder
                or (v.get("folder") or "").replace("\\", "/").startswith(folder + "/")
            ]
    if ext:
        if not ext.startswith("."):
            ext = "." + ext
        videos = [v for v in videos if (v.get("ext") or "").lower() == ext]
    if q:
        videos = [v for v in videos if q in _video_search_text(v)]

    scoped_genres = _genre_facets(videos)
    scoped_subs = _subfolder_facets(videos, category, folder)

    if genre:
        videos = [v for v in videos if genre in ensure_video_genres(v)]

    reverse = True
    key_fn = lambda v: v.get("mtime") or 0
    if sort == "mtime_asc":
        reverse = False
    elif sort == "name":
        key_fn = lambda v: (v.get("name") or "").lower()
        reverse = False
    elif sort == "size_desc":
        key_fn = lambda v: v.get("size") or 0
        reverse = True
    elif sort == "size_asc":
        key_fn = lambda v: v.get("size") or 0
        reverse = False
    elif sort == "duration_desc":
        key_fn = lambda v: v.get("duration") or 0
        reverse = True
    elif sort == "duration_asc":
        key_fn = lambda v: v.get("duration") or 0
        reverse = False
    else:
        reverse = True
        key_fn = lambda v: v.get("mtime") or 0

    videos.sort(key=key_fn, reverse=reverse)
    total = len(videos)
    try:
        offset = max(0, int(request.args.get("offset", 0) or 0))
    except ValueError:
        offset = 0
    try:
        limit = int(request.args.get("limit", 60) or 60)
    except ValueError:
        limit = 60
    limit = max(1, min(limit, 200))

    page = videos[offset: offset + limit]
    slim = []
    for v in page:
        row = {k: v[k] for k in v if k not in ("segments", "_q")} if (
            v.get("kind") == "ts_set" and v.get("segments")
        ) else {k: v[k] for k in v if k != "_q"}
        attach_thumb_meta(row)
        slim.append(row)

    return jsonify({
        "videos": slim,
        "count": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(slim) < total,
        "genres": scoped_genres,
        "subfolders": scoped_subs,
        "subfolder_levels": subfolder_levels,
    })


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
    """选择盘符或目录并扫描。body: { path?: "E:\\", drive?: "E:", thumbs?: true, force?: true }"""
    data = request.get_json(silent=True) or {}
    path = (data.get("path") or data.get("drive") or "").strip().strip('"')
    if not path:
        return jsonify({"ok": False, "msg": "请选择盘符"}), 400
    # 允许 E: / E:\ / E:/
    if len(path) == 2 and path[1] == ":":
        path = path + "\\"
    do_thumbs = data.get("thumbs", True)
    force = data.get("force", False)
    ok, msg = start_scan(Path(path), do_thumbs=bool(do_thumbs), force=bool(force))
    if not ok:
        return jsonify({"ok": False, "msg": msg}), 409
    return jsonify({"ok": True, "msg": msg, "root": str(Path(path).resolve())})


@app.route("/api/rescan", methods=["POST"])
def api_rescan():
    if not STATE["root"]:
        return jsonify({"ok": False, "msg": "尚未选择盘符"}), 400
    data = request.get_json(silent=True) or {}
    do_thumbs = data.get("thumbs", True)
    ok, msg = start_scan(STATE["root"], do_thumbs=bool(do_thumbs), force=True)
    if not ok:
        return jsonify({"ok": False, "msg": msg}), 409
    return jsonify({"ok": True, "msg": msg})


@app.route("/thumb/<vid>")
def thumb(vid: str):
    if not re.fullmatch(r"[a-f0-9]{16}", vid):
        abort(404)
    cache = STATE["cache_dir"]
    placeholder = '''<svg xmlns="http://www.w3.org/2000/svg" width="480" height="270" viewBox="0 0 480 270">
      <rect fill="#1a1d24" width="480" height="270"/>
      <polygon points="210,100 210,170 280,135" fill="#4a5568"/>
    </svg>'''
    placeholder_headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
    }

    if cache:
        raw = read_thumb_jpeg(cache, vid)
        if raw:
            return Response(
                raw,
                mimetype="image/jpeg",
                headers={"Cache-Control": "public, max-age=86400"},
            )
        # 损坏或不存在：尝试现场重建一次
        item = find_video_by_id(vid)
        ffmpeg = STATE.get("ffmpeg")
        if item and ffmpeg:
            src = _video_file_for_thumb(item)
            out = thumb_path(cache, vid)
            if src and make_thumbnail(ffmpeg, src, out):
                thumb_cache_invalidate(vid)
                raw = read_thumb_jpeg(cache, vid)
                if raw:
                    item["has_thumb"] = True
                    item["thumb_v"] = thumb_version(cache, vid)
                    return Response(
                        raw,
                        mimetype="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"},
                    )
            # 解密失败的坏文件删掉，避免反复失败
            try:
                if out.exists() and not read_thumb_jpeg(cache, vid):
                    _clear_path_attrs_windows(out)
                    out.unlink(missing_ok=True)
                    thumb_cache_invalidate(vid)
                    log(f"[预览图] 已删除损坏缓存: {vid}")
            except OSError:
                pass

    return Response(placeholder, mimetype="image/svg+xml", headers=placeholder_headers)


@app.route("/playlist/<vid>.m3u8")
def playlist_m3u8(vid: str):
    """HLS：支持自建 TS 合集，或磁盘上的 .m3u8（改写分片地址）。"""
    item = find_video_by_id(vid)
    if not item:
        abort(404)
    kind = item.get("kind") or ""

    if kind == "m3u8" or (item.get("ext") or "").lower() == ".m3u8":
        path = resolve_under_root(item.get("rel") or "")
        if not path:
            abort(404)
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            abort(404)
        body = rewrite_m3u8_for_proxy(text, item["rel"], vid)
        return Response(
            body,
            mimetype="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache"},
        )

    if kind != "ts_set":
        abort(404)
    segments = item.get("segments") or []
    if len(segments) < 2:
        abort(404)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:30",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    for i in range(len(segments)):
        lines.append("#EXTINF:10.0,")
        lines.append(f"/stream/{vid}/seg/{i}")
    lines.append("#EXT-X-ENDLIST")
    return Response(
        "\n".join(lines) + "\n",
        mimetype="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache"},
    )


@app.route("/hls/<vid>/file")
def hls_proxy_file(vid: str):
    """代理 m3u8 分片/子列表（必须属于当前条目所在目录树）。"""
    item = find_video_by_id(vid)
    if not item:
        abort(404)
    rel = (request.args.get("rel") or "").replace("\\", "/").strip("/")
    if not rel:
        abort(404)
    # 限制：分片须在播放列表所在目录或其子目录下
    base = str(Path((item.get("rel") or "x")).parent).replace("\\", "/")
    if base == ".":
        base = ""
    if base and not (rel == base or rel.startswith(base + "/")):
        # 也允许与 playlist 同级的相对解析结果
        pl_folder = (item.get("folder") or "").strip("/")
        if pl_folder and not (rel == pl_folder or rel.startswith(pl_folder + "/")):
            abort(403)
    path = resolve_under_root(rel)
    if not path:
        abort(404)
    if path.suffix.lower() == ".m3u8":
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            abort(404)
        body = rewrite_m3u8_for_proxy(text, rel, vid)
        return Response(
            body,
            mimetype="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache"},
        )
    mime = mimetypes.guess_type(str(path))[0] or "video/mp2t"
    return _stream_file(path, mime)


@app.route("/stream/<vid>/seg/<int:idx>")
def stream_seg(vid: str, idx: int):
    item = find_video_by_id(vid)
    if not item or item.get("kind") != "ts_set":
        abort(404)
    segments = item.get("segments") or []
    if idx < 0 or idx >= len(segments):
        abort(404)
    path = resolve_video_path(segments[idx])
    if not path:
        abort(404)
    return _stream_file(path, "video/mp2t")


@app.route("/stream/<vid>")
def stream(vid: str):
    item = find_video_by_id(vid)
    if not item:
        abort(404)
    # 分片集合：单文件直链播第一段（预览/兼容）；完整观看用 /playlist/
    path = resolve_video_path(item["rel"])
    if not path:
        abort(404)
    mime = mimetypes.guess_type(str(path))[0] or "video/mp4"
    return _stream_file(path, mime)


@app.route("/api/info/<vid>")
def api_info(vid: str):
    item = find_video_by_id(vid)
    if not item:
        abort(404)
    # 懒加载时长 / 音频编码 / 损坏标记
    need_probe = (
        STATE.get("ffmpeg")
        and (
            int(item.get("probe_ver") or 0) < PROBE_META_VER
            or ("audio_codec" not in item and not item.get("bad"))
            or (not item.get("duration") and not item.get("bad"))
        )
    )
    if need_probe:
        path = _item_probe_path(item)
        if path and path.is_file() and path.suffix.lower() != ".m3u8":
            info = probe_media_info(STATE["ffmpeg"], path)
            _apply_probe_to_item(item, info)
        elif not path or not path.is_file():
            kind = item.get("kind") or ""
            if kind not in ("m3u8", "ts_set") and (item.get("ext") or "").lower() != ".m3u8":
                item["probe_ver"] = PROBE_META_VER
                item["bad"] = True
                item["bad_reason"] = "文件不存在"
    payload = {k: v for k, v in item.items() if k not in ("_q", "segments")}
    # 本地路径（供复制 / 系统播放器）
    local = _local_path_for_item(item)
    payload["path"] = str(local) if local else ""
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
    return jsonify(payload)


def _local_path_for_item(item: dict) -> Path | None:
    if item.get("kind") == "ts_set" and item.get("segments"):
        return resolve_under_root(item["segments"][0])
    return resolve_under_root(item.get("rel") or "")


@app.route("/api/local/<vid>", methods=["POST"])
def api_local(vid: str):
    """本机操作：open=系统播放器打开，reveal=资源管理器定位，path=仅返回路径。"""
    item = find_video_by_id(vid)
    if not item:
        return jsonify({"ok": False, "msg": "未找到视频"}), 404
    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "path").strip().lower()
    path = _local_path_for_item(item)
    if not path:
        return jsonify({"ok": False, "msg": "文件不存在"}), 404
    path_str = str(path)

    if action == "path":
        return jsonify({"ok": True, "path": path_str})

    if action == "open":
        try:
            if sys.platform == "win32":
                os.startfile(path_str)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path_str])
            else:
                subprocess.Popen(["xdg-open", path_str])
            log(f"[本地] 已用系统播放器打开: {path_str}")
            return jsonify({"ok": True, "path": path_str, "msg": "已调用系统播放器"})
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
            return jsonify({"ok": True, "path": path_str, "msg": "已在文件夹中显示"})
        except Exception as e:
            return jsonify({"ok": False, "msg": f"定位失败: {e}", "path": path_str}), 500

    return jsonify({"ok": False, "msg": "未知操作"}), 400


@app.route("/api/convert-mp4/<vid>", methods=["POST"])
def api_convert_mp4_start(vid: str):
    """将 m3u8 / ts_set 转为同目录 MP4（后台任务）。"""
    if not re.fullmatch(r"[a-f0-9]{16}", vid or ""):
        return jsonify({"ok": False, "msg": "无效 id"}), 400
    if not STATE.get("ffmpeg"):
        return jsonify({"ok": False, "msg": "未找到 ffmpeg，请先安装后再试"}), 400
    if not STATE.get("root"):
        return jsonify({"ok": False, "msg": "尚未选择盘符"}), 400
    item = find_video_by_id(vid)
    if not item:
        return jsonify({"ok": False, "msg": "未找到视频"}), 404
    kind = item.get("kind") or ""
    if kind not in ("m3u8", "ts_set") and (item.get("ext") or "").lower() != ".m3u8":
        return jsonify({"ok": False, "msg": "仅支持 m3u8 / TS 合集"}), 400

    with _convert_lock:
        for jid, job in STATE["convert_jobs"].items():
            if job.get("vid") == vid and job.get("status") in ("queued", "running"):
                return jsonify({
                    "ok": True,
                    "job_id": jid,
                    "msg": "已有转换任务进行中",
                    "status": job.get("status"),
                })
        job_id = hashlib.md5(f"{vid}-{datetime.now().timestamp()}".encode()).hexdigest()[:12]
        STATE["convert_jobs"][job_id] = {
            "id": job_id,
            "vid": vid,
            "status": "queued",
            "msg": "排队中…",
            "percent": 0,
            "out_path": "",
            "added_id": "",
            "cancel": False,
            "proc": None,
        }

    threading.Thread(
        target=_convert_worker,
        args=(job_id, vid),
        daemon=True,
        name=f"convert-mp4-{vid[:8]}",
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "msg": "已开始转换", "status": "queued"})


@app.route("/api/convert-mp4/job/<job_id>")
def api_convert_mp4_status(job_id: str):
    with _convert_lock:
        job = STATE["convert_jobs"].get(job_id)
        if not job:
            return jsonify({"ok": False, "msg": "任务不存在"}), 404
        return jsonify({
            "ok": True,
            "job_id": job_id,
            "vid": job.get("vid") or "",
            "status": job.get("status") or "error",
            "msg": job.get("msg") or "",
            "percent": int(job.get("percent") or 0),
            "out_path": job.get("out_path") or "",
            "added_id": job.get("added_id") or "",
        })


@app.route("/api/convert-mp4/job/<job_id>/cancel", methods=["POST"])
def api_convert_mp4_cancel(job_id: str):
    with _convert_lock:
        job = STATE["convert_jobs"].get(job_id)
        if not job:
            return jsonify({"ok": False, "msg": "任务不存在"}), 404
        if job.get("status") in ("done", "error", "cancelled"):
            return jsonify({"ok": True, "msg": "任务已结束", "status": job.get("status")})
        job["cancel"] = True
        job["msg"] = "正在取消…"
        proc = job.get("proc")
    _kill_convert_proc(proc)
    return jsonify({"ok": True, "msg": "已请求取消", "status": "cancelling"})


@app.route("/api/fix-audio/<vid>", methods=["POST"])
def api_fix_audio_start(vid: str):
    """将不兼容浏览器的音轨转为 AAC，输出同目录 *_browser.mp4。"""
    if not re.fullmatch(r"[a-f0-9]{16}", vid or ""):
        return jsonify({"ok": False, "msg": "无效 id"}), 400
    if not STATE.get("ffmpeg"):
        return jsonify({"ok": False, "msg": "未找到 ffmpeg，请先安装后再试"}), 400
    if not STATE.get("root"):
        return jsonify({"ok": False, "msg": "尚未选择盘符"}), 400
    item = find_video_by_id(vid)
    if not item:
        return jsonify({"ok": False, "msg": "未找到视频"}), 404
    kind = item.get("kind") or ""
    if kind in ("m3u8", "ts_set") or (item.get("ext") or "").lower() == ".m3u8":
        return jsonify({"ok": False, "msg": "流媒体请用「转成 MP4」"}), 400

    with _convert_lock:
        for jid, job in STATE["convert_jobs"].items():
            if job.get("vid") == vid and job.get("status") in ("queued", "running"):
                return jsonify({
                    "ok": True,
                    "job_id": jid,
                    "msg": "已有任务进行中",
                    "status": job.get("status"),
                })
        job_id = hashlib.md5(f"fix-audio-{vid}-{datetime.now().timestamp()}".encode()).hexdigest()[:12]
        STATE["convert_jobs"][job_id] = {
            "id": job_id,
            "vid": vid,
            "kind": "fix_audio",
            "status": "queued",
            "msg": "排队中…",
            "percent": 0,
            "out_path": "",
            "added_id": "",
            "cancel": False,
            "proc": None,
        }

    threading.Thread(
        target=_fix_audio_worker,
        args=(job_id, vid),
        daemon=True,
        name=f"fix-audio-{vid[:8]}",
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "msg": "已开始修复声音", "status": "queued"})


@app.route("/api/export-static", methods=["POST"])
def api_export_static():
    """导出纯静态站到视频盘根目录/_video_gallery_static/（后台线程，不挡浏览）。"""
    if STATE.get("exporting"):
        return jsonify({"ok": False, "msg": "正在导出中，请稍候…", "exporting": True})
    if not STATE.get("root"):
        return jsonify({"ok": False, "msg": "请先打开/扫描一个盘"}), 400
    if not (STATE.get("videos") or []):
        return jsonify({"ok": False, "msg": "当前没有可导出的视频"}), 400
    if STATE.get("scanning"):
        return jsonify({"ok": False, "msg": "扫描进行中，请稍后再导出"}), 400

    data = request.get_json(silent=True) or {}
    open_folder = bool(data.get("open_folder", True))

    def job() -> None:
        STATE["exporting"] = True
        STATE["export_ok"] = None
        STATE["export_msg"] = "正在导出静态站…"
        STATE["export_path"] = ""
        try:
            ok, msg, path = export_static_site()
            STATE["export_ok"] = ok
            STATE["export_msg"] = msg
            STATE["export_path"] = path or ""
            if ok and open_folder and path:
                try:
                    if sys.platform == "win32":
                        os.startfile(path)  # type: ignore[attr-defined]
                    else:
                        subprocess.Popen(["xdg-open", path])
                except Exception as e:
                    log(f"[静态导出] 打开目录失败: {e}")
        except Exception as e:
            STATE["export_ok"] = False
            STATE["export_msg"] = f"导出失败: {e}"
            log(f"[静态导出] 异常: {e}")
        finally:
            STATE["exporting"] = False

    threading.Thread(target=job, daemon=True, name="export-static").start()
    return jsonify({"ok": True, "msg": "已开始导出静态站，完成后会打开文件夹", "exporting": True})


@app.route("/api/export-static/status")
def api_export_static_status():
    return jsonify({
        "exporting": bool(STATE.get("exporting")),
        "ok": STATE.get("export_ok"),
        "msg": STATE.get("export_msg") or "",
        "path": STATE.get("export_path") or "",
    })


@app.route("/api/export-static/reveal", methods=["POST"])
def api_export_static_reveal():
    root = STATE.get("root")
    path = STATE.get("export_path") or ""
    if not path and root:
        path = str(Path(root) / STATIC_EXPORT_DIRNAME)
    if not path or not Path(path).is_dir():
        return jsonify({"ok": False, "msg": "尚未导出，或目录不存在"}), 404
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return jsonify({"ok": True, "path": path})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e), "path": path}), 500

