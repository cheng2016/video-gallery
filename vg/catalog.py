# -*- coding: utf-8 -*-
"""Pure catalog indexing/tree helpers and the single catalog STATE writer."""
from __future__ import annotations

import time
from pathlib import Path
from typing import TypedDict

from vg.config import GENRE_DEFS
from vg.disk_libs import stamp_lib_meta
from vg.duplicates import mark_duplicates
from vg.genres import ensure_video_genres
from vg.series import attach_series
from vg.state import STATE, invalidate_query_caches
from vg.taxonomy import ensure_video_taxonomy, taxonomy_facets

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
    by_thumb_id: dict[str, dict]
    by_category: dict[str, list[dict]]
    facets: dict


def video_category(video: dict) -> str:
    """Return the first folder segment used as a channel."""
    folder = (video.get("folder") or "").strip("/")
    return folder.split("/", 1)[0] if folder else ""


def video_search_text(video: dict) -> str:
    """Return and cache the normalized search text for one video."""
    signature = _search_cache_signature(video)
    cached = video.get("_q")
    cached_signature = video.get("_q_sig")
    if cached is not None and (cached_signature is None or cached_signature == signature):
        video["_q_sig"] = signature
        return cached
    from vg.search import build_search_text

    text = build_search_text(video)
    video["_q"] = text
    video["_q_sig"] = _search_cache_signature(video)
    return text


def _search_cache_signature(video: dict) -> tuple:
    """Fields that affect build_search_text; prevents stale warm-cache hits."""
    def values(key: str) -> tuple[str, ...]:
        value = video.get(key) or []
        return tuple(str(item) for item in value) if isinstance(value, (list, tuple)) else (str(value),)

    return (
        str(video.get("name") or ""),
        str(video.get("rel") or ""),
        str(video.get("folder") or ""),
        str(video.get("series_title") or ""),
        values("genres"),
        values("themes"),
        values("backgrounds"),
        values("actors"),
    )


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
    compute_started = time.perf_counter()
    duplicate_ms = series_ms = index_loop_ms = facets_build_ms = 0.0
    search_cache_hits = 0
    search_cache_misses = 0
    if heavy:
        stage_started = time.perf_counter()
        mark_duplicates(videos)
        duplicate_ms = (time.perf_counter() - stage_started) * 1000.0
    stage_started = time.perf_counter()
    attach_series(videos)
    series_ms = (time.perf_counter() - stage_started) * 1000.0

    by_category: dict[str, list[dict]] = {}
    by_id: dict[str, dict] = {}
    by_thumb_id: dict[str, dict] = {}
    type_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    genre_counts: dict[str, int] = {}

    stage_started = time.perf_counter()
    for video in videos:
        vid = video.get("id")
        if vid:
            by_id[vid] = video
        thumb_id = (video.get("_thumb_id") or vid or "").strip()
        if thumb_id:
            by_thumb_id[thumb_id] = video
        ensure_video_taxonomy(video)
        if isinstance(video.get("_q"), str):
            search_cache_hits += 1
        else:
            search_cache_misses += 1
        video_search_text(video)
        category = video_category(video)
        by_category.setdefault(category, []).append(video)
        category_counts[category] = category_counts.get(category, 0) + 1
        ext = (video.get("ext") or "").lower() or "unknown"
        type_counts[ext] = type_counts.get(ext, 0) + 1
        for genre in ensure_video_genres(video):
            genre_counts[genre] = genre_counts.get(genre, 0) + 1
    index_loop_ms = (time.perf_counter() - stage_started) * 1000.0

    stage_started = time.perf_counter()
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
    facets_build_ms = (time.perf_counter() - stage_started) * 1000.0
    try:
        from vg.diagnostics import perf

        perf(
            "catalog_compute_breakdown",
            (time.perf_counter() - compute_started) * 1000.0,
            force=True,
            videos=len(videos),
            heavy=heavy,
            duplicate_ms=duplicate_ms,
            series_ms=series_ms,
            index_loop_ms=index_loop_ms,
            facets_build_ms=facets_build_ms,
            search_cache_hits=search_cache_hits,
            search_cache_misses=search_cache_misses,
        )
    except Exception:
        pass
    return {
        "by_id": by_id,
        "by_thumb_id": by_thumb_id,
        "by_category": by_category,
        "facets": {
            "types": types,
            "genres": genres,
            "themes": taxonomy_facets(videos, "themes"),
            "backgrounds": taxonomy_facets(videos, "backgrounds"),
            "categories": build_category_facets(category_counts),
            "count": len(videos),
        },
    }


def apply_catalog_to_state(videos: list[dict], indexes: CatalogIndexes) -> None:
    """Apply one computed catalog to global runtime state."""
    STATE["videos"] = videos
    STATE["by_category"] = indexes["by_category"]
    STATE["by_id"] = indexes["by_id"]
    STATE["by_thumb_id"] = indexes["by_thumb_id"]
    # Existing ownership must survive a unified multi-disk rebuild.
    stamp_lib_meta(videos, overwrite=False)
    facets = indexes["facets"]
    STATE["facets"] = facets
    STATE["lib_gen"] = int(STATE.get("lib_gen") or 0) + 1
    invalidate_query_caches()
    try:
        from vg.web import invalidate_response_caches

        invalidate_response_caches()
    except ImportError:
        pass
    # Warm the on-disk facets cache so a restart does not need to re-count
    # 2785 videos again (was ~13 ms facets_ms, but with per-scope tree +
    # type/genre counting on every cold start it adds up quickly).  This is
    # best-effort: any write failure is logged via save_facets_disk_cache's
    # own diagnostics but must not crash state publication.
    try:
        from vg.catalog_cache import (
            emit_save_log,
            save_facets_disk_cache,
        )

        # The same signature used by the tree cache covers catalog mtimes,
        # mounted roots, schema and count.  Avoid rewriting an unchanged
        # facets payload on every restart; the detailed skip log makes cache
        # reuse observable without adding write I/O to the hot path.
        save_stats = save_facets_disk_cache(
            facets,
            len(videos),
            only_if_missing=True,
        )
        event = save_stats.pop("event", "facets_disk_cache_save")
        # "skip_reason" means nothing was written; treat as INFO so it does
        # not spam WARN on every per-root catalog load during startup.
        if save_stats.get("skip_reason") and not save_stats.get("bytes_written"):
            emit_save_log("INFO", event, **save_stats)
        else:
            emit_save_log("PERF", event, force=True, **save_stats)
    except Exception as exc:
        from vg.diagnostics import error

        error("facets_disk_cache_save_unexpected_exception", exc)


def rebuild_indexes(videos: list[dict] | None = None, *, heavy: bool = True) -> None:
    """Compatibility orchestrator for catalog computation and STATE update."""
    selected = videos if videos is not None else (STATE.get("videos") or [])
    started = time.perf_counter()
    indexes = compute_catalog(selected, heavy=heavy)
    compute_ms = (time.perf_counter() - started) * 1000.0
    apply_started = time.perf_counter()
    apply_catalog_to_state(selected, indexes)
    apply_ms = (time.perf_counter() - apply_started) * 1000.0
    try:
        from vg.diagnostics import perf

        perf(
            "catalog_rebuild_breakdown",
            (time.perf_counter() - started) * 1000.0,
            force=True,
            videos=len(selected),
            heavy=heavy,
            compute_ms=compute_ms,
            apply_ms=apply_ms,
        )
    except Exception:
        pass
