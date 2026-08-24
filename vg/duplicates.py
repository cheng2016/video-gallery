# -*- coding: utf-8 -*-
"""Pure duplicate detection shared by catalog badges and cleanup APIs."""
from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from typing import TypedDict

from vg.config import MIN_VIDEO_FILE_BYTES
from vg.schema import VideoItem


class DuplicateGroup(TypedDict):
    reason: str
    items: list[VideoItem]


def duplicate_name_key(video: VideoItem) -> str:
    """Normalized display name used by the existing duplicate rule."""
    return (
        video.get("name")
        or Path(video.get("filename") or "").stem
        or ""
    ).strip().casefold()


def video_identity(video: VideoItem) -> str:
    """Identity of one physical catalog entry, including its owning disk."""
    root = (video.get("_lib_root") or video.get("root") or "").strip().casefold()
    rel = (video.get("rel") or "").replace("\\", "/").strip("/").casefold()
    return f"{root}|{rel}|{video.get('id') or ''}"


_CONTENT_HASH_CHUNK = 1024 * 1024
_content_hash_cache_lock = threading.RLock()
_content_hash_cache: dict[tuple[str, int, int, str], str] = {}
_CONTENT_HASH_CACHE_MAX = 4096


def _content_hash(video: VideoItem) -> tuple[str, int, str]:
    """Return a full-content hash for one real file.

    The caller only invokes this after the files have been grouped by exact
    size. This digest reads the complete file to avoid false positives from
    the scan fingerprint's sampled chunks.
    """
    try:
        from vg.disk_libs import resolve_item_rel, resolve_under_root_path

        path = resolve_item_rel(video, video.get("rel") or "")
        if path is None:
            root = str(video.get("_lib_root") or video.get("root") or "").strip()
            if root:
                path = resolve_under_root_path(Path(root), video.get("rel") or "")
        if path is None:
            return "", 0, "path_unavailable"
        stat = path.stat()
        size = int(stat.st_size)
        key = (
            str(path).casefold(),
            size,
            int(getattr(stat, "st_mtime_ns", 0)),
            str(video.get("file_sig") or ""),
        )
        with _content_hash_cache_lock:
            cached = _content_hash_cache.get(key)
        if cached:
            return cached, 0, "cache"

        digest = hashlib.blake2b(digest_size=32)
        bytes_read = 0
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(_CONTENT_HASH_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                bytes_read += len(chunk)
        result = f"b2full:{size}:{digest.hexdigest()}"
        with _content_hash_cache_lock:
            if len(_content_hash_cache) >= _CONTENT_HASH_CACHE_MAX:
                _content_hash_cache.pop(next(iter(_content_hash_cache)))
            _content_hash_cache[key] = result
        return result, bytes_read, "computed"
    except (OSError, ValueError):
        return "", 0, "read_failed"


def find_duplicate_groups(videos: list[VideoItem]) -> list[DuplicateGroup]:
    """Group distinct files by exact-size candidates plus full content hash.

    Files enter the expensive content-hash stage only when at least two files
    have the exact same size and are at least ``MIN_VIDEO_FILE_BYTES`` bytes.
    A full digest is required before a duplicate group is emitted.
    Playlist, TS-set and synthetic series cards are excluded.
    """
    by_size: dict[int, list[VideoItem]] = {}
    for video in videos:
        if (video.get("kind") or "") in ("m3u8", "ts_set", "series"):
            continue
        size = int(video.get("size") or 0)
        if size >= MIN_VIDEO_FILE_BYTES:
            by_size.setdefault(size, []).append(video)

    groups: list[DuplicateGroup] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    hash_started = time.perf_counter()
    candidate_rows = sum(len(items) for items in by_size.values() if len(items) >= 2)
    hash_attempts = 0
    hash_cache_hits = 0
    hash_failures = 0
    hash_bytes = 0

    def add_group(candidates: list[VideoItem]) -> None:
        unique = {video_identity(video): video for video in candidates}
        items = list(unique.values())
        if len(items) < 2:
            return
        key = ("同内容", tuple(sorted(unique)))
        if key in seen:
            return
        seen.add(key)
        groups.append({"reason": "同内容", "items": items})

    for candidates in by_size.values():
        if len(candidates) < 2:
            continue
        by_hash: dict[str, list[VideoItem]] = {}
        for video in candidates:
            digest, bytes_read, source = _content_hash(video)
            hash_attempts += 1
            hash_bytes += bytes_read
            if source == "cache":
                hash_cache_hits += 1
            if not digest:
                hash_failures += 1
                continue
            by_hash.setdefault(digest, []).append(video)
        for hashed in by_hash.values():
            add_group(hashed)

    try:
        from vg.diagnostics import perf as diagnostic_perf
    except ImportError:
        diagnostic_perf = None
    if diagnostic_perf is not None:
        diagnostic_perf(
            "duplicate_content_hash",
            (time.perf_counter() - hash_started) * 1000.0,
            force=True,
            input_rows=len(videos),
            same_size_candidate_rows=candidate_rows,
            hash_attempts=hash_attempts,
            hash_cache_hits=hash_cache_hits,
            hash_failures=hash_failures,
            hash_bytes=hash_bytes,
            duplicate_groups=len(groups),
            duplicate_rows=sum(len(group["items"]) for group in groups),
        )
    return groups


def mark_duplicates(videos: list[VideoItem]) -> None:
    """Apply runtime duplicate badges using the same groups as cleanup."""
    for video in videos:
        video.pop("dup", None)
        video.pop("dup_n", None)
        video.pop("dup_reason", None)

    for group in find_duplicate_groups(videos):
        reason = group["reason"]
        count = len(group["items"])
        for video in group["items"]:
            video["dup"] = True
            video["dup_n"] = max(int(video.get("dup_n") or 0), count)
            reasons = set(str(video.get("dup_reason") or "").split("+"))
            reasons.discard("")
            reasons.add(reason)
            video["dup_reason"] = "+".join(sorted(reasons))
