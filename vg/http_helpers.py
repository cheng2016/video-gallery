# -*- coding: utf-8 -*-
"""Small transport-facing helpers shared by HTTP feature modules."""
from __future__ import annotations

from pathlib import Path

from vg.catalog import video_category
from vg.disk_libs import resolve_item_rel


def resolve_local_path(item: dict) -> Path | None:
    """Resolve the representative local file for a catalog item."""
    if item.get("kind") == "ts_set" and item.get("segments"):
        return resolve_item_rel(item, item["segments"][0])
    return resolve_item_rel(item, item.get("rel") or "")


def filter_videos_by_scope(
    videos: list[dict],
    *,
    category: str = "",
    folder: str = "",
    include_descendants: bool = True,
) -> list[dict]:
    """Apply the shared category/folder scope without mutating input."""
    selected = videos
    category = (category or "").strip().strip("/")
    folder = (folder or "").strip().strip("/").replace("\\", "/")

    if category == "__root__":
        selected = [video for video in selected if not (video.get("folder") or "").strip("/")]
    elif category:
        selected = [video for video in selected if video_category(video) == category]

    if folder:
        def matches(video: dict) -> bool:
            current = (video.get("folder") or "").replace("\\", "/").strip("/")
            return current == folder or (
                include_descendants and current.startswith(folder + "/")
            )

        selected = [video for video in selected if matches(video)]
    return selected
