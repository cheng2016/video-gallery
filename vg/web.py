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
    ensure_cache_dir,
    read_thumb_jpeg,
    save_index,
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
    _kill_convert_proc,
    convert_parallel_limit,
    enqueue_convert_job,
    list_convert_jobs,
    pump_convert_queue,
)
from vg.disk_libs import (
    cache_dir_for_item,
    ensure_library,
    offline_roots,
    resolve_item_rel,
)
from vg.drives import list_drives_info, list_lan_ipv4, load_prefs, save_prefs
from vg.export import export_static_site
from vg.genres import ensure_video_genres
from vg.media import (
    _apply_probe_to_item,
    _item_probe_path,
    _video_file_for_thumb,
    make_thumbnail,
    probe_media_info,
    save_thumbnail_jpeg,
)
from vg.roots import (
    add_mount,
    filter_videos_by_lib,
    get_mounted_roots,
    publish_unified_library,
    remove_mount,
    root_label,
    roots_summary,
    set_mounted_roots,
    thumb_id_for_item,
    tree_for_scope,
)
from vg.scan import (
    _video_category,
    _video_search_text,
    build_tree,
    find_video_by_id,
    rebuild_indexes,
    start_scan,
)
from vg.series import collapse_to_series_cards, series_episodes
from vg.state import STATE, _convert_lock
from vg.streaming import _stream_file, rewrite_m3u8_for_proxy
from vg.trash import move_to_trash
from vg.util import (
    _clear_path_attrs_windows,
    format_size,
    log,
    resolve_under_root,
    resolve_video_path,
)

app = Flask(__name__, template_folder=str(APP_DIR / "templates"))


def _client_ip() -> str:
    # 本机直连，不信任伪造的 X-Forwarded-For
    return (request.remote_addr or "").strip()


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
    lib = (request.args.get("lib") or "").strip()
    all_videos = STATE["videos"] or []
    videos = filter_videos_by_lib(all_videos, lib) if lib else list(all_videos)
    tree = tree_for_scope(lib or None)

    # 按当前范围重算侧面（多根「全部」或选中某一根）
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
    mounts = roots_summary(all_videos)

    return jsonify({
        "tree": tree,
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
        "lib": lib,
        "roots": mounts,
        "multi": len(mounts) > 1,
        "bind_host": STATE.get("bind_host") or "127.0.0.1",
        "bind_port": int(STATE.get("bind_port") or 8765),
        "lan_share": bool(STATE.get("lan_share")),
        "lan_urls": _lan_urls(),
    })


def _lan_urls() -> list[str]:
    port = int(STATE.get("bind_port") or 8765)
    urls = [f"http://127.0.0.1:{port}"]
    if STATE.get("lan_share"):
        for ip in list_lan_ipv4():
            u = f"http://{ip}:{port}"
            if u not in urls:
                urls.append(u)
    return urls


@app.route("/api/share", methods=["GET", "POST"])
def api_share():
    """局域网分享开关。即时生效（服务始终绑定 0.0.0.0，用访问控制开关）。"""
    from vg.lan import ensure_firewall_allow

    port = int(STATE.get("bind_port") or 8765)
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        lan = bool(data.get("lan"))
        STATE["lan_share"] = lan
        save_prefs(lan_share=lan)
        fw_ok, fw_msg = True, ""
        if lan:
            fw_ok, fw_msg = ensure_firewall_allow(port)
        urls = _lan_urls()
        lan_only = [u for u in urls if "127.0.0.1" not in u]
        if lan:
            msg = "已开启局域网分享（立即生效）"
            if lan_only:
                msg += "。其它设备请打开：\n" + "\n".join(lan_only)
            else:
                msg += "。未检测到局域网 IP，请确认电脑已连 WiFi。"
            if fw_msg:
                msg += "\n\n" + fw_msg
            if not fw_ok:
                msg += "\n若仍「拒绝连接」，多半是防火墙拦截。"
        else:
            msg = "已关闭局域网分享（立即生效），仅本机可访问"
        return jsonify({
            "ok": True,
            "lan": lan,
            "active": lan,
            "need_restart": False,
            "firewall_ok": fw_ok,
            "urls": urls,
            "msg": msg,
        })
    prefs = load_prefs()
    return jsonify({
        "ok": True,
        "lan": bool(STATE.get("lan_share")),
        "pref_lan": bool(prefs.get("lan_share")),
        "need_restart": False,
        "urls": _lan_urls(),
        "host": STATE.get("bind_host") or "0.0.0.0",
        "port": port,
    })


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
    for vid in ids[:100]:
        h = hints.get(str(vid)) or hints.get(vid) or {}
        if isinstance(h, dict):
            r = (h.get("root") or "").strip()
            if r:
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
        row = {k: v[k] for k in v if k not in ("segments", "_q", "_lib_root", "_lib_cache")}
        # 给前端存盘符用
        if v.get("_lib_root"):
            row["root"] = v["_lib_root"]
        elif STATE.get("root"):
            row["root"] = str(STATE["root"])
        attach_thumb_meta(row)
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


@app.route("/api/videos")
def api_videos():
    folder = request.args.get("folder", "").strip().strip("/")
    category = request.args.get("category", "").strip().strip("/")
    genre = request.args.get("genre", "").strip()
    q = request.args.get("q", "").strip().lower()
    ext = request.args.get("ext", "").strip().lower()
    sort = request.args.get("sort", "mtime_desc").strip().lower()
    lib = (request.args.get("lib") or "").strip()

    videos = filter_videos_by_lib(STATE.get("videos") or [], lib)

    # 频道：在当前 lib 范围内按一级目录
    if category == "__root__":
        videos = [v for v in videos if not (v.get("folder") or "").strip("/")]
    elif category:
        videos = [v for v in videos if _video_category(v) == category]

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

    view = (request.args.get("view") or "series").strip().lower()
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
        if v.get("kind") == "series":
            row = {k: v[k] for k in v if k not in ("_q", "_lib_root", "_lib_cache", "_folder_raw", "_thumb_id")}
            cover_id = v.get("cover_id") or ""
            if cover_id:
                cover = find_video_by_id(cover_id)
                if cover:
                    attach_thumb_meta(cover)
                    row["has_thumb"] = cover.get("has_thumb")
                    row["thumb_v"] = cover.get("thumb_v")
                    row["thumb_id"] = cover_id
            # 剧集来源盘
            eps = v.get("episodes") or []
            if eps and eps[0].get("lib_label"):
                row["lib_label"] = eps[0].get("lib_label")
            elif eps and eps[0].get("_lib_label"):
                row["lib_label"] = eps[0].get("_lib_label")
            slim.append(row)
            continue
        row = {k: v[k] for k in v if k not in ("segments", "_q", "_lib_root", "_lib_cache", "_folder_raw", "_thumb_id")} if (
            v.get("kind") == "ts_set" and v.get("segments")
        ) else {k: v[k] for k in v if k not in ("_q", "_lib_root", "_lib_cache", "_folder_raw", "_thumb_id")}
        if v.get("_lib_root") and not row.get("root"):
            row["root"] = v["_lib_root"]
        if v.get("_lib_label") and not row.get("lib_label"):
            row["lib_label"] = v["_lib_label"]
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
        "view": view if view in ("series", "flat") else "series",
        "lib": lib,
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
    """扫描所选盘；新盘自动加入片库，已加入的盘则更新其索引。"""
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

    # 先同步加入挂载列表，随后扫描。扫描任何新盘都不会替换已经加入的盘。
    ok, msg = add_mount(root, scan_if_needed=False)
    if not ok:
        return jsonify({"ok": False, "msg": msg}), 400
    ok, scan_msg = start_scan(
        root,
        do_thumbs=bool(do_thumbs),
        force=bool(force),
        replace_mounts=False,
    )
    if not ok:
        return jsonify({"ok": False, "msg": scan_msg, "roots": roots_summary()}), 409
    return jsonify({
        "ok": True,
        "msg": f"{root_label(root)} 已自动加入片库，{scan_msg}",
        "root": str(root),
        "roots": roots_summary(),
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


@app.route("/thumb/<vid>")
def thumb(vid: str):
    if not re.fullmatch(r"[a-f0-9]{16}", vid):
        abort(404)
    prefer_root = (request.args.get("root") or "").strip() or None
    item = find_video_by_id(vid, prefer_root=prefer_root)
    # 也可能用 thumb_id（碰撞重映射后）直接请求
    if not item:
        for v in STATE.get("videos") or []:
            if thumb_id_for_item(v) == vid or v.get("id") == vid:
                item = v
                break
    cache = cache_dir_for_item(item) or STATE.get("cache_dir")
    file_id = thumb_id_for_item(item) if item else vid
    placeholder = '''<svg xmlns="http://www.w3.org/2000/svg" width="480" height="270" viewBox="0 0 480 270">
      <rect fill="#1a1d24" width="480" height="270"/>
      <polygon points="210,100 210,170 280,135" fill="#4a5568"/>
    </svg>'''
    placeholder_headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
    }

    if cache:
        raw = read_thumb_jpeg(cache, file_id)
        if raw:
            return Response(
                raw,
                mimetype="image/jpeg",
                headers={"Cache-Control": "public, max-age=86400"},
            )
        # 损坏或不存在：尝试现场重建一次
        ffmpeg = STATE.get("ffmpeg")
        if item and ffmpeg:
            src = _video_file_for_thumb(item)
            out = thumb_path(cache, file_id)
            if src and make_thumbnail(ffmpeg, src, out):
                thumb_cache_invalidate(file_id)
                raw = read_thumb_jpeg(cache, file_id)
                if raw:
                    item["has_thumb"] = True
                    item["thumb_v"] = thumb_version(cache, file_id)
                    return Response(
                        raw,
                        mimetype="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"},
                    )
            try:
                if out.exists() and not read_thumb_jpeg(cache, file_id):
                    _clear_path_attrs_windows(out)
                    out.unlink(missing_ok=True)
                    thumb_cache_invalidate(file_id)
                    log(f"[预览图] 已删除损坏缓存: {file_id}")
            except OSError:
                pass

    return Response(placeholder, mimetype="image/svg+xml", headers=placeholder_headers)


@app.route("/playlist/<vid>.m3u8")
def playlist_m3u8(vid: str):
    """HLS：支持自建 TS 合集，或磁盘上的 .m3u8（改写分片地址）。"""
    prefer_root = (request.args.get("root") or "").strip() or None
    item = find_video_by_id(vid, prefer_root=prefer_root)
    if not item:
        abort(404)
    kind = item.get("kind") or ""

    if kind == "m3u8" or (item.get("ext") or "").lower() == ".m3u8":
        path = resolve_item_rel(item, item.get("rel") or "")
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
    prefer_root = (request.args.get("root") or "").strip() or None
    item = find_video_by_id(vid, prefer_root=prefer_root)
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
    path = resolve_item_rel(item, rel)
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
    prefer_root = (request.args.get("root") or "").strip() or None
    item = find_video_by_id(vid, prefer_root=prefer_root)
    if not item or item.get("kind") != "ts_set":
        abort(404)
    segments = item.get("segments") or []
    if idx < 0 or idx >= len(segments):
        abort(404)
    path = resolve_item_rel(item, segments[idx])
    if not path:
        abort(404)
    return _stream_file(path, "video/mp2t")


@app.route("/stream/<vid>")
def stream(vid: str):
    prefer_root = (request.args.get("root") or "").strip() or None
    item = find_video_by_id(vid, prefer_root=prefer_root)
    if not item:
        abort(404)
    # 分片集合：单文件直链播第一段（预览/兼容）；完整观看用 /playlist/
    path = resolve_item_rel(item, item.get("rel") or "")
    if not path:
        abort(404)
    mime = mimetypes.guess_type(str(path))[0] or "video/mp4"
    return _stream_file(path, mime)


@app.route("/api/info/<vid>")
def api_info(vid: str):
    prefer_root = (request.args.get("root") or "").strip() or None
    if prefer_root:
        ensure_library(prefer_root)
    item = find_video_by_id(vid, prefer_root=prefer_root)
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
    return jsonify(payload)


def _local_path_for_item(item: dict) -> Path | None:
    if item.get("kind") == "ts_set" and item.get("segments"):
        return resolve_item_rel(item, item["segments"][0])
    return resolve_item_rel(item, item.get("rel") or "")


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


@app.route("/api/series/<sid>")
def api_series(sid: str):
    """合集分集列表。"""
    if not re.fullmatch(r"s[a-f0-9]{15}", sid or ""):
        return jsonify({"ok": False, "msg": "无效合集 id"}), 400
    items = series_episodes(STATE.get("videos") or [], sid)
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


@app.route("/api/cleanup")
def api_cleanup():
    """重复 / 损坏列表。?type=dup|bad"""
    kind = (request.args.get("type") or "dup").strip().lower()
    videos = STATE.get("videos") or []

    def _row(v: dict, extra: dict | None = None) -> dict:
        row = {
            "id": v.get("id"),
            "name": v.get("name"),
            "path": str(_local_path_for_item(v) or ""),
            "size": int(v.get("size") or 0),
            "size_h": v.get("size_h") or "",
            "folder": v.get("folder") or "",
            "mtime": float(v.get("mtime") or 0),
            "mtime_h": v.get("mtime_h") or "",
            "ext": v.get("ext") or "",
            "kind": v.get("kind") or "",
        }
        if extra:
            row.update(extra)
        attach_thumb_meta(row)
        return row

    if kind == "bad":
        rows = []
        for v in videos:
            if not v.get("bad"):
                continue
            rows.append(_row(v, {"reason": v.get("bad_reason") or "无法读取"}))
        return jsonify({"ok": True, "type": "bad", "groups": [{"reason": "损坏", "items": rows}], "count": len(rows)})

    # dup groups by name_key / size
    by_name: dict[str, list] = {}
    by_size: dict[int, list] = {}
    for v in videos:
        if not v.get("dup"):
            continue
        if (v.get("kind") or "") in ("m3u8", "ts_set"):
            continue
        nk = (v.get("name") or "").strip().casefold()
        if nk and "同名" in str(v.get("dup_reason") or ""):
            by_name.setdefault(nk, []).append(v)
        sz = int(v.get("size") or 0)
        if sz and "同体积" in str(v.get("dup_reason") or ""):
            by_size.setdefault(sz, []).append(v)

    groups = []
    seen_ids: set[str] = set()

    def _pack(label: str, items: list) -> None:
        if len(items) < 2:
            return
        ids = tuple(sorted(x.get("id") or "" for x in items))
        key = label + "|" + ",".join(ids)
        if key in seen_ids:
            return
        seen_ids.add(key)
        groups.append({
            "reason": label,
            "items": [_row(x) for x in items],
        })

    for items in by_name.values():
        _pack("同名", items)
    for items in by_size.values():
        _pack("同体积", items)

    return jsonify({"ok": True, "type": "dup", "groups": groups, "count": sum(len(g["items"]) for g in groups)})


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
def api_delete():
    """移到回收站并从片库移除。body: { ids: [], trash: true }"""
    data = request.get_json(silent=True) or {}
    ids = data.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"ok": False, "msg": "请选择要删除的条目"}), 400
    if data.get("trash") is False:
        return jsonify({"ok": False, "msg": "仅支持移到回收站，请勿关闭 trash"}), 400
    if not STATE.get("root"):
        return jsonify({"ok": False, "msg": "尚未选择盘符"}), 400

    removed = []
    errors = []
    videos = list(STATE.get("videos") or [])
    by_id = {v.get("id"): v for v in videos if v.get("id")}

    for vid in ids:
        vid = str(vid or "")
        if not re.fullmatch(r"[a-f0-9]{16}", vid):
            errors.append({"id": vid, "msg": "无效 id"})
            continue
        item = by_id.get(vid)
        if not item:
            errors.append({"id": vid, "msg": "未找到"})
            continue
        paths = _paths_for_delete(item)
        if not paths:
            errors.append({"id": vid, "msg": "文件不存在"})
            # 仍从索引去掉
            videos = [v for v in videos if v.get("id") != vid]
            removed.append(vid)
            continue
        ok_all = True
        for p in paths:
            ok, msg = move_to_trash(p)
            if not ok:
                ok_all = False
                errors.append({"id": vid, "msg": msg, "path": str(p)})
                break
        if ok_all:
            videos = [v for v in videos if v.get("id") != vid]
            removed.append(vid)
            thumb_cache_invalidate(vid)

    STATE["videos"] = videos
    if STATE.get("root"):
        STATE["tree"] = build_tree(Path(STATE["root"]), videos)
    rebuild_indexes(videos)
    cache = STATE.get("cache_dir") or ensure_cache_dir(Path(STATE["root"])) if STATE.get("root") else None
    if cache and STATE.get("root"):
        save_index(cache, Path(STATE["root"]), videos)

    return jsonify({
        "ok": True,
        "removed": removed,
        "errors": errors,
        "msg": f"已移除 {len(removed)} 项" + (f"，{len(errors)} 项失败" if errors else ""),
    })


@app.route("/api/convert-mp4/<vid>", methods=["POST"])
def api_convert_mp4_start(vid: str):
    """将 m3u8 / ts_set 转为同目录 MP4（进入限速队列）。"""
    if not re.fullmatch(r"[a-f0-9]{16}", vid or ""):
        return jsonify({"ok": False, "msg": "无效 id"}), 400
    if not STATE.get("ffmpeg"):
        return jsonify({"ok": False, "msg": "未找到 ffmpeg，请先安装后再试"}), 400
    if not (STATE.get("root") or get_mounted_roots()):
        return jsonify({"ok": False, "msg": "尚未选择盘符"}), 400
    item = find_video_by_id(vid)
    if not item:
        return jsonify({"ok": False, "msg": "未找到视频"}), 404
    kind = item.get("kind") or ""
    if kind not in ("m3u8", "ts_set") and (item.get("ext") or "").lower() != ".m3u8":
        return jsonify({"ok": False, "msg": "仅支持 m3u8 / TS 合集"}), 400
    ok, msg, job_id = enqueue_convert_job(vid, kind="mp4", name=item.get("name") or "")
    return jsonify({"ok": ok, "job_id": job_id, "msg": msg, "status": "queued"})


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
            "kind": job.get("kind") or "mp4",
            "name": job.get("name") or "",
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
        if job.get("status") == "queued":
            job["status"] = "cancelled"
            job["msg"] = "已取消"
            proc = None
        else:
            proc = job.get("proc")
    if proc:
        _kill_convert_proc(proc)
    pump_convert_queue()
    return jsonify({"ok": True, "msg": "已请求取消", "status": "cancelling"})


@app.route("/api/fix-audio/<vid>", methods=["POST"])
def api_fix_audio_start(vid: str):
    """将不兼容浏览器的音轨转为 AAC（进入限速队列）。"""
    if not re.fullmatch(r"[a-f0-9]{16}", vid or ""):
        return jsonify({"ok": False, "msg": "无效 id"}), 400
    if not STATE.get("ffmpeg"):
        return jsonify({"ok": False, "msg": "未找到 ffmpeg，请先安装后再试"}), 400
    if not (STATE.get("root") or get_mounted_roots()):
        return jsonify({"ok": False, "msg": "尚未选择盘符"}), 400
    item = find_video_by_id(vid)
    if not item:
        return jsonify({"ok": False, "msg": "未找到视频"}), 404
    kind = item.get("kind") or ""
    if kind in ("m3u8", "ts_set") or (item.get("ext") or "").lower() == ".m3u8":
        return jsonify({"ok": False, "msg": "流媒体请用「转成 MP4」"}), 400
    ok, msg, job_id = enqueue_convert_job(vid, kind="fix_audio", name=item.get("name") or "")
    return jsonify({"ok": ok, "job_id": job_id, "msg": msg, "status": "queued"})


@app.route("/api/convert/queue")
def api_convert_queue():
    jobs = list_convert_jobs(50)
    active = sum(1 for j in jobs if j.get("status") in ("queued", "running"))
    return jsonify({
        "ok": True,
        "jobs": jobs,
        "active": active,
        "parallel": convert_parallel_limit(),
    })


@app.route("/api/convert/batch", methods=["POST"])
def api_convert_batch():
    """批量加入转换队列。body: { ids: [], kind: "mp4"|"fix_audio", parallel?: 1-4 }"""
    data = request.get_json(silent=True) or {}
    ids = data.get("ids") or []
    kind = (data.get("kind") or "mp4").strip().lower()
    if kind not in ("mp4", "fix_audio"):
        return jsonify({"ok": False, "msg": "kind 应为 mp4 或 fix_audio"}), 400
    if data.get("parallel") is not None:
        try:
            STATE["convert_parallel"] = max(1, min(4, int(data["parallel"])))
            from vg.drives import save_prefs
            save_prefs(convert_parallel=STATE["convert_parallel"])
            pump_convert_queue()
        except (TypeError, ValueError):
            pass
    if not isinstance(ids, list):
        return jsonify({"ok": False, "msg": "ids 无效"}), 400
    # 仅改并发
    if not ids:
        return jsonify({
            "ok": True,
            "queued": [],
            "skipped": [],
            "msg": f"并发已设为 {convert_parallel_limit()}",
            "parallel": convert_parallel_limit(),
        })
    if not STATE.get("ffmpeg"):
        return jsonify({"ok": False, "msg": "未找到 ffmpeg"}), 400

    queued = []
    skipped = []
    for vid in ids[:80]:
        vid = str(vid or "")
        if not re.fullmatch(r"[a-f0-9]{16}", vid):
            skipped.append({"id": vid, "msg": "无效 id"})
            continue
        item = find_video_by_id(vid)
        if not item:
            skipped.append({"id": vid, "msg": "未找到"})
            continue
        ik = item.get("kind") or ""
        ext = (item.get("ext") or "").lower()
        if kind == "mp4":
            if ik not in ("m3u8", "ts_set") and ext != ".m3u8":
                skipped.append({"id": vid, "msg": "不是 m3u8/TS 合集"})
                continue
        else:
            if ik in ("m3u8", "ts_set") or ext == ".m3u8":
                skipped.append({"id": vid, "msg": "流媒体请用转 MP4"})
                continue
            if not item.get("audio_hard"):
                skipped.append({"id": vid, "msg": "无需修声音"})
                continue
        ok, msg, job_id = enqueue_convert_job(vid, kind=kind, name=item.get("name") or "")
        if ok:
            queued.append({"id": vid, "job_id": job_id, "name": item.get("name") or ""})
        else:
            skipped.append({"id": vid, "msg": msg})
    return jsonify({
        "ok": True,
        "queued": queued,
        "skipped": skipped,
        "msg": f"已排队 {len(queued)} 个" + (f"，跳过 {len(skipped)}" if skipped else ""),
        "parallel": convert_parallel_limit(),
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
    return jsonify({
        "ok": True,
        "msg": f"已截取 {seek:.1f}s 处画面为封面",
        "thumb_v": item["thumb_v"],
        "thumb_id": file_id,
        "seek": seek,
    })


@app.route("/api/roots", methods=["GET", "POST"])
def api_roots():
    """多根目录：GET 列表；POST {action: add|remove|set|publish, path?, paths?}"""
    if request.method == "GET":
        mounts = roots_summary()
        primary = ""
        try:
            if STATE.get("root"):
                primary = str(Path(STATE["root"]).resolve())
        except OSError:
            primary = str(STATE.get("root") or "")
        for m in mounts:
            m["current"] = bool(primary) and m.get("path", "").lower() == primary.lower()
        return jsonify({
            "ok": True,
            "roots": mounts,
            "count": len(mounts),
            "multi": len(mounts) > 1,
            "primary": primary,
        })

    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "add").strip().lower()
    if action == "publish":
        n = publish_unified_library()
        return jsonify({"ok": True, "msg": f"已刷新统一片库（{n}）", "count": n})
    if action == "set":
        paths = data.get("paths") or []
        if not isinstance(paths, list):
            return jsonify({"ok": False, "msg": "paths 无效"}), 400
        cleaned = set_mounted_roots([str(p) for p in paths])
        n = publish_unified_library() if cleaned else 0
        return jsonify({"ok": True, "msg": f"已设置 {len(cleaned)} 个目录", "roots": cleaned, "count": n})
    path = (data.get("path") or data.get("drive") or "").strip().strip('"')
    if not path:
        return jsonify({"ok": False, "msg": "请提供 path"}), 400
    if len(path) == 2 and path[1] == ":":
        path = path + "\\"
    if action == "remove":
        ok, msg = remove_mount(path)
        return jsonify({"ok": ok, "msg": msg, "roots": roots_summary()})
    # add
    ok, msg = add_mount(path)
    return jsonify({"ok": ok, "msg": msg, "roots": roots_summary()})


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

