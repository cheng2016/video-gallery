# -*- coding: utf-8 -*-
"""Pure duplicate detection shared by catalog badges and cleanup APIs."""
from __future__ import annotations

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


def find_duplicate_groups(videos: list[VideoItem]) -> list[DuplicateGroup]:
    """Group distinct files by the product's two duplicate heuristics.

    Rules are intentionally unchanged:
    - same normalized name;
    - exact same size when size is at least ``MIN_VIDEO_FILE_BYTES``.
    Playlist, TS-set and synthetic series cards are excluded.
    """
    by_name: dict[str, list[VideoItem]] = {}
    by_size: dict[int, list[VideoItem]] = {}
    for video in videos:
        if (video.get("kind") or "") in ("m3u8", "ts_set", "series"):
            continue
        name_key = duplicate_name_key(video)
        if name_key:
            by_name.setdefault(name_key, []).append(video)
        size = int(video.get("size") or 0)
        if size >= MIN_VIDEO_FILE_BYTES:
            by_size.setdefault(size, []).append(video)

    groups: list[DuplicateGroup] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()

    def add_group(reason: str, candidates: list[VideoItem]) -> None:
        unique = {video_identity(video): video for video in candidates}
        items = list(unique.values())
        if len(items) < 2:
            return
        key = (reason, tuple(sorted(unique)))
        if key in seen:
            return
        seen.add(key)
        groups.append({"reason": reason, "items": items})

    for candidates in by_name.values():
        add_group("同名", candidates)
    for candidates in by_size.values():
        add_group("同体积", candidates)
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
