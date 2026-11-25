# -*- coding: utf-8 -*-
"""Thumbnail identity, cache-dir listing, and cross-disk reuse."""
from __future__ import annotations

import os
import shutil
import stat
import time
from pathlib import Path

from vg.cache import list_thumb_ids, thumb_file_ready, thumb_path
from vg.config import MIN_VIDEO_FILE_BYTES, THUMB_EXT
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
    """Yield (video, cache_dir) from all known SQLite catalogs."""
    from vg.catalog_db import iter_catalog_cache_dirs, load_catalog_videos

    started = time.perf_counter()
    cpu_started = time.thread_time()
    rows: list[tuple[dict, Path]] = []
    cache = None
    enumerate_ms = 0.0
    cache_details: list[dict] = []
    failed = False
    try:
        caches = iter_catalog_cache_dirs()
        enumerate_ms = (time.perf_counter() - started) * 1000.0
        for cache in caches:
            load_started = time.perf_counter()
            load_cpu_started = time.thread_time()
            videos = load_catalog_videos(cache)
            load_ms = (time.perf_counter() - load_started) * 1000.0
            load_cpu_ms = (time.thread_time() - load_cpu_started) * 1000.0
            cache_details.append({
                "cache": str(cache),
                "rows": len(videos),
                "load_ms": round(load_ms, 1),
                "cpu_ms": round(load_cpu_ms, 1),
            })
            for video in videos:
                if isinstance(video, dict):
                    rows.append((video, cache))
    except OSError as exc:
        failed = True
        from vg.diagnostics import error

        error("thumbnail_source_catalog_failed", exc, cache=cache)
    from vg.diagnostics import perf

    perf(
        "thumbnail_source_catalogs",
        (time.perf_counter() - started) * 1000.0,
        force=failed,
        cpu_ms=round((time.thread_time() - cpu_started) * 1000.0, 1),
        enumerate_ms=round(enumerate_ms, 1),
        rows=len(rows),
        catalogs=len(cache_details),
        cache_details=sorted(cache_details, key=lambda detail: detail["load_ms"], reverse=True)[:10],
    )
    return rows


def _cache_for_item(item: dict) -> Path | None:
    from vg.disk_libs import cache_dir_for_item

    return cache_dir_for_item(item)


def build_thumb_source_index(wanted_keys: set[str] | None = None) -> dict[str, Path]:
    """Map content identity -> existing .vgt path, optionally only wanted keys."""
    started = time.perf_counter()
    cpu_started = time.thread_time()
    index: dict[str, Path] = {}
    candidate_rows = 0
    filtered_rows = 0
    checked_paths = 0
    ready_paths = 0
    stat_errors = 0
    duplicate_paths = 0
    path_cache_hits = 0
    stat_calls = 0
    cache_resolve_calls = 0
    cache_resolve_ms = 0.0
    file_check_ms = 0.0
    key_build_ms = 0.0
    caches: set[str] = set()
    # Scope the memo to this build; a later build must recheck changed files.
    path_stats: dict[Path, os.stat_result | None] = {}

    def register(video: dict, cache: Path | None = None) -> None:
        nonlocal candidate_rows, filtered_rows, checked_paths, ready_paths
        nonlocal stat_errors, duplicate_paths, path_cache_hits, stat_calls
        nonlocal cache_resolve_calls, cache_resolve_ms, file_check_ms, key_build_ms
        key_started = time.perf_counter()
        keys = thumb_content_keys(video)
        if wanted_keys is not None:
            keys = [key for key in keys if key in wanted_keys]
        key_build_ms += (time.perf_counter() - key_started) * 1000.0
        if wanted_keys is not None and not keys:
            filtered_rows += 1
            return
        candidate_rows += 1
        if video.get("has_thumb") is False:
            return
        vid = (video.get("id") or "").strip()
        if not vid:
            return
        if cache is None:
            cache_started = time.perf_counter()
            cache_resolve_calls += 1
            cache = _cache_for_item(video)
            cache_resolve_ms += (time.perf_counter() - cache_started) * 1000.0
        if not cache:
            return
        path = thumb_path(cache, vid)
        checked_paths += 1
        caches.add(str(cache))
        file_started = time.perf_counter()
        try:
            if path in path_stats:
                duplicate_paths += 1
                path_cache_hits += 1
                source_stat = path_stats[path]
            else:
                stat_calls += 1
                try:
                    source_stat = path.stat()
                except (FileNotFoundError, NotADirectoryError):
                    # These were ordinary misses in Path.is_file(), not failures.
                    source_stat = None
                except OSError as exc:
                    source_stat = None
                    stat_errors += 1
                    from vg.diagnostics import error

                    error("thumbnail_source_stat_failed", exc, cache=cache, video_id=vid)
                path_stats[path] = source_stat
            if source_stat is None or not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size <= 24:
                return
        finally:
            file_check_ms += (time.perf_counter() - file_started) * 1000.0
        ready_paths += 1
        key_started = time.perf_counter()
        for key in keys:
            index.setdefault(key, path)
        key_build_ms += (time.perf_counter() - key_started) * 1000.0

    memory_videos = _iter_memory_videos()
    memory_collect_ms = (time.perf_counter() - started) * 1000.0
    stage_started = time.perf_counter()
    stage_cpu_started = time.thread_time()
    for video in memory_videos:
        register(video)
    memory_register_ms = (time.perf_counter() - stage_started) * 1000.0
    memory_register_cpu_ms = (time.thread_time() - stage_cpu_started) * 1000.0
    stage_started = time.perf_counter()
    catalog_lookup_skipped = wanted_keys is not None and wanted_keys.issubset(index)
    catalog_videos = [] if catalog_lookup_skipped else _iter_preview_cache_index_videos()
    catalog_load_ms = (time.perf_counter() - stage_started) * 1000.0
    stage_started = time.perf_counter()
    stage_cpu_started = time.thread_time()
    for video, cache in catalog_videos:
        register(video, cache)
    disk_register_ms = (time.perf_counter() - stage_started) * 1000.0
    disk_register_cpu_ms = (time.thread_time() - stage_cpu_started) * 1000.0
    from vg.diagnostics import perf

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    cpu_ms = (time.thread_time() - cpu_started) * 1000.0
    register_other_ms = max(
        0.0,
        memory_register_ms + disk_register_ms - cache_resolve_ms - file_check_ms - key_build_ms,
    )
    perf(
        "thumbnail_source_index",
        elapsed_ms,
        force=bool(stat_errors),
        memory_rows=len(memory_videos),
        catalog_rows=len(catalog_videos),
        catalog_lookup_skipped=catalog_lookup_skipped,
        candidate_rows=candidate_rows,
        filtered_rows=filtered_rows,
        keys=len(index),
        caches=len(caches),
        checked_paths=checked_paths,
        unique_paths=len(path_stats),
        duplicate_paths=duplicate_paths,
        path_cache_hits=path_cache_hits,
        stat_calls=stat_calls,
        is_file_calls=0,
        size_stat_calls=stat_calls,
        cache_resolve_calls=cache_resolve_calls,
        ready_paths=ready_paths,
        stat_errors=stat_errors,
        cpu_ms=round(cpu_ms, 1),
        non_cpu_ms=round(max(0.0, elapsed_ms - cpu_ms), 1),
        memory_collect_ms=round(memory_collect_ms, 1),
        memory_register_ms=round(memory_register_ms, 1),
        memory_register_cpu_ms=round(memory_register_cpu_ms, 1),
        catalog_load_ms=round(catalog_load_ms, 1),
        disk_register_ms=round(disk_register_ms, 1),
        disk_register_cpu_ms=round(disk_register_cpu_ms, 1),
        cache_resolve_ms=round(cache_resolve_ms, 1),
        file_check_ms=round(file_check_ms, 1),
        key_build_ms=round(key_build_ms, 1),
        register_other_ms=round(register_other_ms, 1),
    )
    return index


def reuse_existing_thumb(item: dict, cache: Path, sources: dict[str, Path] | None = None) -> bool:
    """Copy/hardlink a matching .vgt from any cache. Does not run ffmpeg or JPEG LRU."""
    vid = (item.get("id") or "").strip()
    if not vid:
        return False
    dest = thumb_path(cache, vid)
    if item.get("has_thumb") is not False and thumb_file_ready(cache, vid):
        return True
    mapping = sources if sources is not None else build_thumb_source_index()
    src: Path | None = None
    for key in thumb_content_keys(item):
        hit = mapping.get(key)
        if hit is not None:
            src = hit
            break
    if src is None or (item.get("has_thumb") is False and src == dest):
        return False
    pending = None
    if item.get("_media_source_path"):
        from uuid import uuid4
        pending = dest.with_name(f".{dest.stem}.{uuid4().hex}.pending")
    try:
        if not link_or_copy_thumb(src, pending or dest):
            return False
        item["has_thumb"] = True
        item["thumb"] = f"{vid}{THUMB_EXT}"
        if pending:
            from vg.disk_libs import patch_library_media_items
            from vg.schema import THUMB_RESULT_FIELDS
            clear_thumbnail_failure(item)
            item["_thumb_pending_path"] = str(pending)
            item["_thumb_target_path"] = str(dest)
            return bool(patch_library_media_items([item], fields=THUMB_RESULT_FIELDS))
        return True
    finally:
        if pending:
            pending.unlink(missing_ok=True)
            item.pop("_thumb_pending_path", None)
            item.pop("_thumb_target_path", None)


def thumbnail_failure_is_current(item: dict) -> bool:
    """Whether a persisted thumbnail failure still matches this source file."""
    if not item.get("thumb_failed"):
        return False
    try:
        saved_size = int(item.get("thumb_failed_size"))
        current_size = int(item.get("size") or 0)
    except (TypeError, ValueError):
        saved_size = current_size = 0
    try:
        saved_mtime = float(item.get("thumb_failed_mtime"))
        current_mtime = float(item.get("mtime") or 0)
    except (TypeError, ValueError):
        saved_mtime = current_mtime = 0.0
    # Legacy markers without source metadata remain conservative skips.
    if not item.get("thumb_failed_size") and not item.get("thumb_failed_mtime"):
        return True
    return saved_size == current_size and abs(saved_mtime - current_mtime) < 1.0


def mark_thumbnail_failure(item: dict, *, reason: str = "generation_failed") -> None:
    """Persist a source-specific negative thumbnail result on the item."""
    item["thumb_failed"] = True
    item["thumb_failed_reason"] = str(reason or "generation_failed")[:120]
    item["thumb_failed_at"] = time.time()
    item["thumb_failed_size"] = int(item.get("size") or 0)
    try:
        item["thumb_failed_mtime"] = float(item.get("mtime") or 0)
    except (TypeError, ValueError):
        item["thumb_failed_mtime"] = 0.0
    item["has_thumb"] = False
    item["thumb_v"] = 0


def clear_thumbnail_failure(item: dict) -> bool:
    """Clear a negative marker after success or an explicit user retry."""
    changed = False
    for key in (
        "thumb_failed",
        "thumb_failed_reason",
        "thumb_failed_at",
        "thumb_failed_size",
        "thumb_failed_mtime",
    ):
        if key in item:
            item.pop(key, None)
            changed = True
    return changed


def missing_thumb_items(videos: list[dict], cache: Path | None) -> list[dict]:
    """Videos whose cache dir has no .vgt. Does not walk the video disk."""
    have = list_thumb_ids(cache)
    missing: list[dict] = []
    skipped_failed = 0
    for video in videos:
        vid = (video.get("id") or "").strip()
        if not vid:
            continue
        if vid in have and video.get("has_thumb") is not False:
            video["has_thumb"] = True
            continue
        if thumbnail_failure_is_current(video):
            skipped_failed += 1
            video["has_thumb"] = False
            video["thumb_v"] = 0
            continue
        missing.append(video)
    if skipped_failed:
        from vg.diagnostics import aggregate, emit

        aggregate("thumbnail_generation_skipped_persisted_fail", skipped_failed)
        emit(
            "INFO",
            "thumbnail_generation_skipped_persisted_fail",
            force=True,
            skipped=skipped_failed,
            candidates=len(videos),
        )
    return missing


def adopt_thumbs_from_caches(missing: list[dict], cache: Path) -> tuple[list[dict], int]:
    """Reuse cross-disk thumbs. Returns (still_missing, reused_count)."""
    if not missing:
        return [], 0
    started = time.perf_counter()
    wanted_keys = {key for item in missing for key in thumb_content_keys(item)}
    sources = build_thumb_source_index(wanted_keys=wanted_keys)
    index_ms = (time.perf_counter() - started) * 1000.0
    stage_started = time.perf_counter()
    leftover: list[dict] = []
    reused = 0
    for item in missing:
        if reuse_existing_thumb(item, cache, sources):
            reused += 1
        else:
            leftover.append(item)
    match_copy_ms = (time.perf_counter() - stage_started) * 1000.0
    from vg.diagnostics import perf

    perf(
        "thumbnail_reuse_stages",
        (time.perf_counter() - started) * 1000.0,
        cache=cache,
        candidates=len(missing),
        reused=reused,
        remaining=len(leftover),
        index_ms=round(index_ms, 1),
        match_copy_ms=round(match_copy_ms, 1),
    )
    if reused:
        log(f"[预览图] 跨盘复用 {reused} 个，无需重新截帧")
    return leftover, reused
