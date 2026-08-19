# -*- coding: utf-8 -*-
"""Thumbnail identity, cache-dir listing, and cross-disk reuse."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from vg.cache import list_thumb_ids, thumb_file_ready, thumb_path
from vg.config import INDEX_NAME, MIN_VIDEO_FILE_BYTES, THUMB_EXT, VGDATA_DIR
from vg.duplicates import duplicate_name_key, video_identity
from vg.state import STATE
from vg.util import log


def folder_key(folder: str | None) -> str:
    value = (folder or "").replace("\\", "/").strip("/")
    return "" if value in {".", ""} else value


def item_folder_key(item: dict) -> str:
    raw = item.get("_folder_raw") or item.get("folder")
    if raw:
        return folder_key(str(raw))
    rel = (item.get("rel") or "").replace("\\", "/").strip("/")
    if not rel or "/" not in rel:
        return ""
    return folder_key(str(Path(rel).parent))


def thumb_content_keys(video: dict) -> list[str]:
    """Stable keys so the same file on another disk can reuse a .vgt."""
    keys: list[str] = []
    sig = str(video.get("file_sig") or "").strip()
    if sig:
        keys.append(f"sig:{sig}")
    kind = video.get("kind") or ""
    if kind in ("m3u8", "ts_set", "series"):
        return keys
    name = duplicate_name_key(video)
    try:
        size = int(video.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    if name and size >= MIN_VIDEO_FILE_BYTES:
        keys.append(f"name_size:{name}|{size}")
    return keys


def link_or_copy_thumb(src: Path, dest: Path) -> bool:
    """Hardlink when possible (same volume under preview_cache); else copy."""
    try:
        if not src.is_file() or src.stat().st_size <= 24:
            return False
    except OSError:
        return False
    try:
        if dest.exists() and dest.resolve() == src.resolve():
            return True
    except OSError:
        pass
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        if dest.exists():
            dest.unlink()
    except OSError:
        pass
    try:
        os.link(src, dest)
        return True
    except OSError:
        try:
            shutil.copy2(src, dest)
            return dest.is_file() and dest.stat().st_size > 24
        except OSError:
            return False


def _iter_memory_videos() -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for video in list(STATE.get("videos") or []):
        if not isinstance(video, dict):
            continue
        ident = video_identity(video)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(video)
    for lib in (STATE.get("disk_libs") or {}).values():
        if not isinstance(lib, dict):
            continue
        for video in (lib.get("by_id") or {}).values():
            if not isinstance(video, dict):
                continue
            ident = video_identity(video)
            if ident in seen:
                continue
            seen.add(ident)
            out.append(video)
    return out


def _iter_preview_cache_index_videos() -> list[tuple[dict, Path]]:
    rows: list[tuple[dict, Path]] = []
    try:
        if not VGDATA_DIR.is_dir():
            return rows
        for index_path in VGDATA_DIR.glob(f"*/{INDEX_NAME}"):
            cache = index_path.parent
            try:
                payload = json.loads(index_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            videos = payload.get("videos") if isinstance(payload, dict) else None
            if not isinstance(videos, list):
                continue
            for video in videos:
                if isinstance(video, dict):
                    rows.append((video, cache))
    except OSError:
        pass
    return rows


def _cache_for_item(item: dict) -> Path | None:
    from vg.disk_libs import cache_dir_for_item

    return cache_dir_for_item(item)


def build_thumb_source_index() -> dict[str, Path]:
    """Map content identity -> existing .vgt path across all known caches."""
    index: dict[str, Path] = {}

    def register(video: dict, cache: Path | None) -> None:
        if not cache:
            return
        vid = (video.get("id") or "").strip()
        if not vid:
            return
        path = thumb_path(cache, vid)
        try:
            if not path.is_file() or path.stat().st_size <= 24:
                return
        except OSError:
            return
        for key in thumb_content_keys(video):
            index.setdefault(key, path)

    for video in _iter_memory_videos():
        register(video, _cache_for_item(video))
    for video, cache in _iter_preview_cache_index_videos():
        register(video, cache)
    return index


def reuse_existing_thumb(item: dict, cache: Path, sources: dict[str, Path] | None = None) -> bool:
    """Copy/hardlink a matching .vgt from any cache. Does not run ffmpeg or JPEG LRU."""
    vid = (item.get("id") or "").strip()
    if not vid:
        return False
    dest = thumb_path(cache, vid)
    if thumb_file_ready(cache, vid):
        return True
    mapping = sources if sources is not None else build_thumb_source_index()
    src: Path | None = None
    for key in thumb_content_keys(item):
        hit = mapping.get(key)
        if hit is not None:
            src = hit
            break
    if src is None:
        return False
    if link_or_copy_thumb(src, dest):
        item["has_thumb"] = True
        item["thumb"] = f"{vid}{THUMB_EXT}"
        return True
    return False


def missing_thumb_items(videos: list[dict], cache: Path | None) -> list[dict]:
    """Videos whose cache dir has no .vgt. Does not walk the video disk."""
    have = list_thumb_ids(cache)
    missing: list[dict] = []
    for video in videos:
        vid = (video.get("id") or "").strip()
        if not vid:
            continue
        if vid in have:
            video["has_thumb"] = True
            continue
        missing.append(video)
    return missing


def adopt_thumbs_from_caches(missing: list[dict], cache: Path) -> tuple[list[dict], int]:
    """Reuse cross-disk thumbs. Returns (still_missing, reused_count)."""
    if not missing:
        return [], 0
    sources = build_thumb_source_index()
    leftover: list[dict] = []
    reused = 0
    for item in missing:
        if reuse_existing_thumb(item, cache, sources):
            reused += 1
        else:
            leftover.append(item)
    if reused:
        log(f"[预览图] 跨盘复用 {reused} 个，无需重新截帧")
    return leftover, reused
