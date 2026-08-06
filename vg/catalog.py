# -*- coding: utf-8 -*-
"""Pure catalog indexing/tree helpers and the single catalog STATE writer."""
from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from vg.config import GENRE_DEFS
from vg.disk_libs import stamp_lib_meta
from vg.duplicates import mark_duplicates
from vg.genres import ensure_video_genres
from vg.series import attach_series
from vg.state import STATE

CATEGORY_PREFER_ORDER = (
    "电影",
    "电视剧",
    "综艺",
    "动漫",
    "少儿",
    "纪录片",
    "短剧",
    "体育",
    "音乐",
    "教育",
    "其他",
    "",
)
_CATEGORY_RANK = {name: index for index, name in enumerate(CATEGORY_PREFER_ORDER)}


class CatalogIndexes(TypedDict):
    by_id: dict[str, dict]
    by_category: dict[str, list[dict]]
    facets: dict


def video_category(video: dict) -> str:
    """Return the first folder segment used as a channel."""
    folder = (video.get("folder") or "").strip("/")
    return folder.split("/", 1)[0] if folder else ""


def video_search_text(video: dict) -> str:
    """Return and cache the normalized search text for one video."""
    cached = video.get("_q")
    if cached is not None:
        return cached
    from vg.search import build_search_text

    text = build_search_text(video)
    video["_q"] = text
    return text


def build_category_facets(counts: dict[str, int]) -> list[dict]:
    """Build category rows with the product's stable channel order."""
    return [
        {
            "id": name,
            "name": "未分类" if name == "" else name,
            "count": count,
        }
        for name, count in sorted(
            counts.items(),
            key=lambda item: (
                _CATEGORY_RANK.get(item[0], 100),
                -item[1],
                item[0].lower(),
            ),
        )
    ]


def build_tree(root: Path, videos: list[dict], *, with_videos: bool = False) -> dict:
    """Build the folder tree without embedding full video rows by default."""
    root_node = {
        "name": root.name or str(root),
        "path": "",
        "children": {},
        "videos": [],
    }

    for video in videos:
        parts = Path(video["rel"]).parts
        folders = parts[:-1]
        node = root_node
        cumulative = []
        for folder in folders:
            cumulative.append(folder)
            key = "/".join(cumulative)
            if folder not in node["children"]:
                node["children"][folder] = {
                    "name": folder,
                    "path": key,
                    "children": {},
                    "videos": [],
                }
            node = node["children"][folder]
        node["videos"].append(video if with_videos else 1)

    def finalize(node: dict) -> dict:
        children = [
            finalize(child)
            for child in sorted(
                node["children"].values(),
                key=lambda child: child["name"].lower(),
            )
        ]
        count = len(node["videos"]) + sum(child["count"] for child in children)
        embedded = (
            sorted(node["videos"], key=lambda video: (video.get("name") or "").lower())
            if with_videos
            else []
        )
        return {
            "name": node["name"],
            "path": node["path"],
            "count": count,
            "children": children,
            "videos": embedded,
        }

    return finalize(root_node)


def compute_catalog(videos: list[dict], *, heavy: bool = True) -> CatalogIndexes:
    """Compute derived indexes; does not write global STATE.

    Derived fields on video rows remain in-place for compatibility with the
    current card/series behavior.
    """
    if heavy:
        mark_duplicates(videos)
    attach_series(videos)

    by_category: dict[str, list[dict]] = {}
    by_id: dict[str, dict] = {}
    type_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    genre_counts: dict[str, int] = {}

    for video in videos:
        vid = video.get("id")
        if vid:
            by_id[vid] = video
        if heavy:
            video.pop("_q", None)
        video_search_text(video)
        category = video_category(video)
        by_category.setdefault(category, []).append(video)
        category_counts[category] = category_counts.get(category, 0) + 1
        ext = (video.get("ext") or "").lower() or "unknown"
        type_counts[ext] = type_counts.get(ext, 0) + 1
        for genre in ensure_video_genres(video):
            genre_counts[genre] = genre_counts.get(genre, 0) + 1

    types = [
        {"ext": ext, "count": count, "label": ext.lstrip(".").upper() or "未知"}
        for ext, count in sorted(type_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    genre_order = {name: index for index, (name, _) in enumerate(GENRE_DEFS)}
    genres = [
        {"id": name, "name": name, "count": count}
        for name, count in sorted(
            genre_counts.items(),
            key=lambda item: (
                genre_order.get(item[0], 999),
                -item[1],
                item[0],
            ),
        )
        if count > 0
    ]
    return {
        "by_id": by_id,
        "by_category": by_category,
        "facets": {
            "types": types,
            "genres": genres,
            "categories": build_category_facets(category_counts),
            "count": len(videos),
        },
    }


def apply_catalog_to_state(videos: list[dict], indexes: CatalogIndexes) -> None:
    """Apply one computed catalog to global runtime state."""
    STATE["videos"] = videos
    STATE["by_category"] = indexes["by_category"]
    STATE["by_id"] = indexes["by_id"]
    # Existing ownership must survive a unified multi-disk rebuild.
    stamp_lib_meta(videos, overwrite=False)
    STATE["facets"] = indexes["facets"]
    STATE["lib_gen"] = int(STATE.get("lib_gen") or 0) + 1


def rebuild_indexes(videos: list[dict] | None = None, *, heavy: bool = True) -> None:
    """Compatibility orchestrator for catalog computation and STATE update."""
    selected = videos if videos is not None else (STATE.get("videos") or [])
    apply_catalog_to_state(selected, compute_catalog(selected, heavy=heavy))
