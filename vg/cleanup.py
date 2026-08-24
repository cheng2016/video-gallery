# -*- coding: utf-8 -*-
"""Cleanup feature orchestration independent from Flask routes."""
from __future__ import annotations

from vg.cache import attach_thumb_meta
from vg.catalog import build_category_facets, video_category
from vg.catalog_repository import CatalogScopeReader, catalog_repository
from vg.config import MIN_VIDEO_FILE_BYTES
from vg.duplicates import find_duplicate_groups
from vg.http_helpers import filter_videos_by_scope, resolve_local_path


def cleanup_categories_for_lib(
    lib: str = "",
    *,
    reader: CatalogScopeReader = catalog_repository,
) -> list[dict]:
    """Return selectable cleanup channels for one disk/all disks."""
    counts: dict[str, int] = {}
    for video in reader.videos_for_scope(lib or None):
        category = video_category(video)
        counts[category] = counts.get(category, 0) + 1
    rows = build_category_facets(counts)
    return [
        {
            **row,
            "id": "__root__" if row["id"] == "" else row["id"],
        }
        for row in rows
    ]


def serialize_cleanup_row(video: dict, *, extra: dict | None = None) -> dict:
    """Build the stable cleanup API row for one video."""
    enriched = dict(video)
    attach_thumb_meta(enriched)
    row = {
        "id": video.get("id"),
        "name": video.get("name"),
        "rel": video.get("rel") or "",
        "path": str(resolve_local_path(video) or ""),
        "size": int(video.get("size") or 0),
        "size_h": video.get("size_h") or "",
        "folder": video.get("folder") or "",
        "mtime": float(video.get("mtime") or 0),
        "mtime_h": video.get("mtime_h") or "",
        "ext": video.get("ext") or "",
        "kind": video.get("kind") or "",
        "root": video.get("root") or video.get("_lib_root") or "",
        "lib_label": video.get("lib_label") or video.get("_lib_label") or "",
        "has_thumb": bool(enriched.get("has_thumb")),
        "thumb_v": int(enriched.get("thumb_v") or 0),
        "thumb_id": (
            enriched.get("thumb_id")
            or video.get("_thumb_id")
            or video.get("id")
        ),
        "category": video_category(video) or "未分类",
    }
    if extra:
        row.update(extra)
    return row


def build_cleanup_response(
    kind: str = "dup",
    *,
    lib: str = "",
    category: str = "",
    folder: str = "",
    reader: CatalogScopeReader = catalog_repository,
) -> dict:
    """Build the complete cleanup response for duplicate or bad-file mode."""
    kind = (kind or "dup").strip().lower()
    lib = (lib or "").strip()
    category = (category or "").strip().strip("/")
    folder = (folder or "").strip().strip("/").replace("\\", "/")
    videos = filter_videos_by_scope(
        list(reader.videos_for_scope(lib or None)),
        category=category,
        folder=folder,
        include_descendants=True,
    )
    scope = {
        "lib": lib,
        "category": category,
        "folder": folder,
        "video_count": len(videos),
        "rules": (
            "同体积（≥"
            f"{MIN_VIDEO_FILE_BYTES // 1024}KB）后内容哈希相同的不同文件；"
            "仅在当前所选盘/频道范围内比对"
        ),
    }

    if kind == "bad":
        rows = [
            serialize_cleanup_row(
                video,
                extra={"reason": video.get("bad_reason") or "无法读取"},
            )
            for video in videos
            if video.get("bad")
        ]
        groups = [{"reason": "损坏", "items": rows}]
        count = len(rows)
    else:
        groups = [
            {
                "reason": group["reason"],
                "items": [
                    serialize_cleanup_row(video)
                    for video in group["items"]
                ],
            }
            for group in find_duplicate_groups(videos)
        ]
        count = sum(len(group["items"]) for group in groups)

    return {
        "ok": True,
        "type": "bad" if kind == "bad" else "dup",
        "groups": groups,
        "count": count,
        "scope": scope,
        "categories": cleanup_categories_for_lib(lib, reader=reader),
        "roots": reader.roots_summary(),
    }
