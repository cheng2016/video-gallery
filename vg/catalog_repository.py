# -*- coding: utf-8 -*-
"""Narrow read interfaces over the runtime/multi-disk catalog.

Features depend on these protocols instead of knowing how STATE, disk_libs and
mounted roots cooperate.
"""
from __future__ import annotations

from typing import Protocol

from vg.disk_libs import (
    ensure_cached_indexes_scanned,
    ensure_library,
    find_in_disk_libs,
    read_root_library,
    _norm_root_str,
)
from vg.roots import get_mounted_roots, roots_summary, videos_for_scope
from vg.state import STATE


class VideoLookup(Protocol):
    def find_video(self, vid: str, prefer_root: str | None = None) -> dict | None:
        """Find a video, optionally constrained to one owning root."""
        ...


class CatalogScopeReader(Protocol):
    def videos_for_scope(self, lib: str | None = None) -> list[dict]:
        """Read all videos in one disk/all mounted disks."""
        ...

    def roots_summary(self) -> list[dict]:
        """Read mounted-root labels, counts and categories."""
        ...


class MountedRootsReader(Protocol):
    def mounted_roots(self) -> list[str]:
        """Read normalized mounted roots."""
        ...


class CatalogRepository(VideoLookup, CatalogScopeReader, MountedRootsReader, Protocol):
    """Combined interface for features that need all catalog read capabilities."""


def _same_root(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        return _norm_root_str(left).casefold() == _norm_root_str(right).casefold()
    except Exception:
        return str(left).replace("/", "\\").rstrip("\\").casefold() == str(
            right
        ).replace("/", "\\").rstrip("\\").casefold()


class RuntimeCatalogRepository:
    """Default adapter over current in-memory and per-disk catalog storage."""

    def videos_for_scope(self, lib: str | None = None) -> list[dict]:
        return videos_for_scope(lib)

    def roots_summary(self) -> list[dict]:
        return roots_summary(None)

    def mounted_roots(self) -> list[str]:
        return get_mounted_roots()

    def find_video(self, vid: str, prefer_root: str | None = None) -> dict | None:
        prefer = (prefer_root or "").strip() or None
        if prefer:
            ensure_library(prefer)
            hit = find_in_disk_libs(vid, prefer_root=prefer)
            if hit is not None:
                return hit
            for item in STATE.get("videos") or []:
                item_root = item.get("_lib_root") or item.get("root") or ""
                if not _same_root(item_root, prefer):
                    continue
                if item.get("id") == vid or item.get("_thumb_id") == vid:
                    return item
            saved = read_root_library(prefer)
            if saved is not None:
                hit = next(
                    (
                        item
                        for item in saved
                        if item.get("id") == vid or item.get("_thumb_id") == vid
                    ),
                    None,
                )
                if hit is not None:
                    return hit
            return None

        by_id = STATE.get("by_id") or {}
        hit = by_id.get(vid)
        if hit is not None:
            return hit
        hit = next(
            (
                item
                for item in STATE.get("videos") or []
                if item.get("id") == vid
            ),
            None,
        )
        if hit is not None:
            return hit
        hit = find_in_disk_libs(vid, prefer_root=None)
        if hit is not None:
            return hit
        ensure_cached_indexes_scanned()
        return find_in_disk_libs(vid, prefer_root=None)


catalog_repository = RuntimeCatalogRepository()


def find_video_by_id(vid: str, prefer_root: str | None = None) -> dict | None:
    """Compatibility function backed by the default repository."""
    return catalog_repository.find_video(vid, prefer_root)
