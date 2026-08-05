# -*- coding: utf-8 -*-
"""Static site export."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import urllib.request
from datetime import datetime
from pathlib import Path

from vg.segments import _normalize_playlist_rel as _normalize_rel_join

from vg.cache import (
    cleanup_legacy_disk_cache,
    ensure_cache_dir,
    read_thumb_jpeg,
    thumb_file_ready,
)
from vg.config import APP_DIR, BROWSER_HARD_EXTS, STATIC_EXPORT_DIRNAME
from vg.drives import save_prefs
from vg.genres import ensure_video_genres
from vg.state import STATE
from vg.util import _hide_path_windows, log, video_id

def _rel_between(from_dir: Path, to_path: Path) -> str:
    return Path(os.path.relpath(str(to_path), str(from_dir))).as_posix()


def _ensure_hls_js(dest: Path) -> bool:
    """把 hls.min.js 放到静态站 _cache 目录（优先本地缓存，其次下载）。"""
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / "hls.min.js"
    if target.exists() and target.stat().st_size > 1000:
        return True
    local_candidates = [
        APP_DIR / "static" / "hls.min.js",
        APP_DIR / "preview_cache" / "hls.min.js",
    ]
    for c in local_candidates:
        try:
            if c.exists() and c.stat().st_size > 1000:
                target.write_bytes(c.read_bytes())
                return True
        except OSError:
            pass
    urls = [
        "https://cdn.jsdelivr.net/npm/hls.js@1.5.18/dist/hls.min.js",
        "https://unpkg.com/hls.js@1.5.18/dist/hls.min.js",
    ]
    try:
        import urllib.request
        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=20) as resp:
                    data = resp.read()
                if data and len(data) > 1000:
                    target.write_bytes(data)
                    (APP_DIR / "preview_cache").mkdir(parents=True, exist_ok=True)
                    (APP_DIR / "preview_cache" / "hls.min.js").write_bytes(data)
                    return True
            except Exception:
                continue
    except Exception:
        pass
    # 没有 hls 也能打开页面，只是 TS/m3u8 浏览器播可能受限
    target.write_text("/* hls.js missing */\n", encoding="utf-8")
    return False


def _export_playlist_files(export_dir: Path, root: Path, item: dict) -> str | None:
    """为 ts_set / m3u8 生成相对路径播放列表，返回相对 index.html 的路径。"""
    play_dir = export_dir / "_cache" / "playlists"
    play_dir.mkdir(parents=True, exist_ok=True)
    vid = item.get("id") or "x"
    out = play_dir / f"{vid}.m3u8"
    kind = item.get("kind") or ""

    if kind == "ts_set" and item.get("segments"):
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-TARGETDURATION:30",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
        ]
        for seg in item["segments"]:
            seg_path = root / seg
            if not seg_path.is_file():
                continue
            rel = _rel_between(play_dir, seg_path)
            lines.append("#EXTINF:10.0,")
            lines.append(rel)
        lines.append("#EXT-X-ENDLIST")
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return f"_cache/playlists/{vid}.m3u8"

    if kind == "m3u8" or (item.get("ext") or "").lower() == ".m3u8":
        src = root / (item.get("rel") or "")
        if not src.is_file():
            return None
        try:
            text = src.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        base_dir = str(Path(item["rel"]).parent).replace("\\", "/")
        if base_dir == ".":
            base_dir = ""
        lines = []
        for line in text.splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or re.match(r"https?://", raw, re.I):
                lines.append(line)
                continue
            seg_rel = _normalize_rel_join(base_dir, raw)
            seg_path = root / seg_rel
            if seg_path.is_file():
                lines.append(_rel_between(play_dir, seg_path))
            else:
                lines.append(line)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return f"_cache/playlists/{vid}.m3u8"
    return None


def export_static_site(root: Path | None = None, videos: list[dict] | None = None) -> tuple[bool, str, str]:
    """
    导出纯静态站到「视频盘根目录/_video_gallery_static/」。
    预览图等资源全部在站内 _cache/，不写盘根其它缓存目录。
    返回 (ok, msg, export_path)。
    """
    root = Path(root or STATE.get("root") or "")
    if not root.is_dir():
        return False, "请先打开/扫描一个盘", ""
    videos = list(videos if videos is not None else (STATE.get("videos") or []))
    if not videos:
        return False, "当前没有可导出的视频，请先扫描", ""

    cleanup_legacy_disk_cache(root)

    cache = STATE.get("cache_dir") or ensure_cache_dir(root)
    export_dir = root / STATIC_EXPORT_DIRNAME
    cache_dir = export_dir / "_cache"
    thumbs_dir = cache_dir / "thumbs"
    try:
        export_dir.mkdir(parents=True, exist_ok=True)
        thumbs_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"无法创建导出目录: {e}", ""

    # 清理旧版导出在站根的 thumbs/（已迁入 _cache）
    for old in (export_dir / "thumbs", export_dir / "playlists", export_dir / "hls.min.js"):
        try:
            if old.is_dir():
                shutil.rmtree(old)
            elif old.is_file():
                old.unlink()
        except OSError:
            pass

    log(f"[静态导出] 开始 → {export_dir}（共 {len(videos)} 个）")
    STATE["export_msg"] = f"正在导出静态站 0/{len(videos)}…"
    _ensure_hls_js(cache_dir)

    # 预览图目录整夹重建（比逐个删旧 .js/.vgt 快很多）
    try:
        if thumbs_dir.exists():
            shutil.rmtree(thumbs_dir)
        thumbs_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"无法重建预览图目录: {e}", str(export_dir)

    # 文件名混淆盐：预览图仍是 JPEG，只是文件名看不出，扩展名也不是 .jpg
    name_salt = os.urandom(8).hex()

    def obscure_thumb_name(vid: str) -> str:
        digest = hashlib.sha256(f"{name_salt}:{vid}".encode("utf-8")).hexdigest()[:20]
        return f"{digest}.vgj"

    exported = []
    thumb_ok = thumb_fail = 0
    for i, v in enumerate(videos, 1):
        vid = v.get("id") or video_id(v.get("rel") or str(i))
        rel = (v.get("rel") or "").replace("\\", "/")
        folder = (v.get("folder") or "").strip("/")
        category = folder.split("/")[0] if folder else ""
        kind = v.get("kind") or ""
        abs_path = None
        if kind == "ts_set" and v.get("segments"):
            abs_path = root / v["segments"][0]
        else:
            abs_path = root / rel if rel else None

        src_rel = ""
        file_url = ""
        path_str = ""
        if abs_path and abs_path.is_file():
            src_rel = _rel_between(export_dir, abs_path)
            try:
                file_url = abs_path.resolve().as_uri()
            except OSError:
                file_url = ""
            path_str = str(abs_path)

        # 预览图：只拷贝已有缓存图，改个看不出的文件名（不做重加密，避免导出卡死）
        thumb_rel = ""
        raw = None
        if cache and thumb_file_ready(cache, vid):
            raw = read_thumb_jpeg(cache, vid)
        if raw:
            try:
                fname = obscure_thumb_name(vid)
                (thumbs_dir / fname).write_bytes(raw)
                thumb_rel = f"_cache/thumbs/{fname}"
                thumb_ok += 1
            except OSError:
                thumb_fail += 1
        else:
            thumb_fail += 1

        playlist = None
        note = ""
        if kind in ("ts_set", "m3u8") or (v.get("ext") or "").lower() == ".m3u8":
            playlist = _export_playlist_files(export_dir, root, v)
            note = "HLS/分片，建议 Firefox 或系统播放器"
        elif (v.get("ext") or "").lower() in BROWSER_HARD_EXTS:
            note = "浏览器可能无法内嵌播放，请用「打开文件」"

        exported.append({
            "id": vid,
            "name": v.get("name") or Path(rel).stem,
            "folder": folder,
            "category": category,
            "ext": v.get("ext") or "",
            "kind": kind,
            "size": int(v.get("size") or 0),
            "size_h": v.get("size_h") or "",
            "mtime": float(v.get("mtime") or 0),
            "mtime_h": v.get("mtime_h") or "",
            "duration": v.get("duration"),
            "duration_h": v.get("duration_h") or "",
            "genres": ensure_video_genres(v),
            "seg_count": int(v.get("seg_count") or 0),
            "dup": bool(v.get("dup")),
            "dup_n": int(v.get("dup_n") or 0),
            "dup_reason": v.get("dup_reason") or "",
            "bad": bool(v.get("bad")),
            "bad_reason": v.get("bad_reason") or "",
            "series_id": v.get("series_id") or "",
            "series_title": v.get("series_title") or "",
            "season": v.get("season"),
            "episode": v.get("episode"),
            "src": src_rel,
            "path": path_str,
            "file_url": file_url,
            "thumb": thumb_rel,
            "playlist": playlist,
            "note": note,
        })
        if i % 50 == 0 or i == len(videos):
            STATE["export_msg"] = f"正在导出静态站 {i}/{len(videos)}…"
            log(f"[静态导出] 进度 {i}/{len(videos)}…")

    data_js = (
        "window.__VG_DATA__ = "
        + json.dumps(
            {
                "root": str(root),
                "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "count": len(exported),
                "bridge_port": 8767,
                "videos": exported,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + ";\n"
    )
    (export_dir / "data.js").write_text(data_js, encoding="utf-8")

    html_src = APP_DIR / "templates" / "static_site.html"
    if not html_src.exists():
        return False, "缺少 templates/static_site.html", str(export_dir)
    (export_dir / "index.html").write_text(html_src.read_text(encoding="utf-8"), encoding="utf-8")

    bridge_src = APP_DIR / "templates" / "static_bridge.py"
    if bridge_src.exists():
        (cache_dir / "static_bridge.py").write_text(bridge_src.read_text(encoding="utf-8"), encoding="utf-8")
    (cache_dir / "bridge.json").write_text(
        json.dumps({"root": str(root), "port": 8767}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    paths_map = {row["id"]: row["path"] for row in exported if row.get("id") and row.get("path")}
    (cache_dir / "paths.json").write_text(
        json.dumps(paths_map, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    hta_src = APP_DIR / "templates" / "static_launcher.hta"
    if hta_src.exists():
        (export_dir / "打开图库.hta").write_text(hta_src.read_text(encoding="utf-8"), encoding="utf-8")

    try:
        _hide_path_windows(cache_dir)
    except Exception:
        pass

    bat = export_dir / "打开图库.bat"
    bat.write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "cd /d \"%~dp0\"\r\n"
        "set VG_STATIC_PORT=8767\r\n"
        "where python >nul 2>&1 && set \"PY=python\"\r\n"
        "if not defined PY where py >nul 2>&1 && set \"PY=py\"\r\n"
        "if not defined PY (\r\n"
        "  echo 未找到 Python，改用 打开图库.hta\r\n"
        "  if exist \"打开图库.hta\" (start \"\" \"打开图库.hta\") else (start \"\" \"index.html\")\r\n"
        "  pause\r\n"
        "  exit /b 1\r\n"
        ")\r\n"
        "echo ========================================\r\n"
        "echo  静态视频库助手\r\n"
        "echo  浏览器将自动打开 http://127.0.0.1:%VG_STATIC_PORT%/\r\n"
        "echo  关闭本窗口 = 停止（系统播放器依赖此窗口）\r\n"
        "echo ========================================\r\n"
        "%PY% \"_cache\\static_bridge.py\"\r\n"
        "pause\r\n",
        encoding="utf-8",
    )
    readme = export_dir / "说明.txt"
    readme.write_text(
        "本地视频库 · 静态离线版\n"
        "====================\n"
        "【重要】请双击「打开图库.bat」，不要双击 index.html。\n"
        "\n"
        "1. 打开图库.bat：启动本地助手 + 浏览器（系统播放器可用）\n"
        "2. 打开图库.hta：无 Python 时的备用（系统播放器也可用）\n"
        "3. 地址栏应是 http://127.0.0.1:8767/ 才正常\n"
        "4. 预览图在 _cache\\thumbs\\，勿删 _cache\n"
        "5. 影片仍在原位置，未复制\n"
        f"6. 导出时间：{datetime.now().isoformat(timespec='seconds')}\n"
        f"7. 视频根目录：{root}\n",
        encoding="utf-8",
    )

    save_prefs(last_static_export=str(export_dir))
    msg = (
        f"已导出 {len(exported)} 个到：\n{export_dir}\n"
        f"预览图成功 {thumb_ok}，缺图 {thumb_fail}。\n"
        "双击该目录下「打开图库.bat」即可离线浏览。"
    )
    log(f"[静态导出] 完成：{export_dir}")

