# -*- coding: utf-8 -*-
"""Narrow read interfaces over the runtime/multi-disk catalog.

Features depend on these protocols instead of knowing how STATE, disk_libs and
mounted roots cooperate.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Protocol

from vg.disk_libs import (
    ensure_cached_indexes_scanned,
    find_in_disk_libs,
    read_root_library,
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


@lru_cache(maxsize=512)
def _root_compare_key(root: str) -> str:
    """Filesystem-free root identity for hot lookup loops.

    ``Path.resolve()`` hits the disk. Soft ``/api/videos-by-ids`` used to call
    it once per STATE.videos row per id (20×1000 under scan/thumb IO) and
    stall waitress for 20–30s while ``/api/videos`` on other threads stayed
    fine. String normalize is enough for ownership checks.
    """
    s = str(root or "").strip().replace("/", "\\")
    if not s:
        return ""
    # Keep drive roots as ``e:\`` so ``E:`` and ``E:\`` compare equal.
    if len(s) == 2 and s[1] == ":":
        s = s + "\\"
    else:
        s = s.rstrip("\\")
        if len(s) == 2 and s[1] == ":":
            s = s + "\\"
    return os.path.normcase(s)


def _same_root(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        return _root_compare_key(str(left)) == _root_compare_key(str(right))
    except Exception:
        return str(left).replace("/", "\\").rstrip("\\").casefold() == str(
            right
        ).replace("/", "\\").rstrip("\\").casefold()


def _emit_lookup_skip(vid: str, prefer: str | None) -> None:
    try:
        from vg.diagnostics import emit_rate_limited

        emit_rate_limited(
            "WARN",
            "catalog_lookup_skip_sqlite_writer_busy",
            key=f"lookup-skip|{prefer or ''}",
            interval=5.0,
            force=True,
            video_id=str(vid)[:24],
            prefer_root=prefer or "",
            scanning=bool(STATE.get("scanning")),
            updating=bool(STATE.get("updating")),
            meta_progress=str(STATE.get("meta_progress") or "")[:120],
            reason="avoid_catalog_lock_deadlock",
        )
    except Exception:
        pass


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
        catalog_writer_busy = bool(
            STATE.get("scanning")
            or STATE.get("updating")
            or STATE.get("meta_progress")
        )
        if prefer:
            # The current/unified runtime indexes already contain the owning
            # root.  Check them before touching per-disk persistence: the old
            # order called ensure_library twice and, for the active root,
            # archived the entire catalog on every deferred /thumb request.
            prefer_key = _root_compare_key(prefer)
            for index_name in ("by_id", "by_thumb_id"):
                hit = (STATE.get(index_name) or {}).get(vid)
                if hit is None:
                    continue
                item_root = hit.get("_lib_root") or hit.get("root") or ""
                if _root_compare_key(str(item_root)) == prefer_key:
                    return hit
            for item in STATE.get("videos") or []:
                item_root = item.get("_lib_root") or item.get("root") or ""
                if _root_compare_key(str(item_root)) != prefer_key:
                    continue
                if item.get("id") == vid or item.get("_thumb_id") == vid:
                    return item
            # While scan/meta is rewriting catalogs, skip disk_libs + SQLite.
            # find_in_disk_libs still takes _libs_lock / Path.resolve and was
            # stacking 20–30s stalls on by-ids even after ensure was skipped.
            if catalog_writer_busy:
                _emit_lookup_skip(vid, prefer)
                return None
            # find_in_disk_libs performs the one required ensure_library call.
            hit = find_in_disk_libs(vid, prefer_root=prefer)
            if hit is not None:
                return hit
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
        hit = (STATE.get("by_thumb_id") or {}).get(vid)
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
        if catalog_writer_busy:
            _emit_lookup_skip(vid, None)
            return None
        hit = find_in_disk_libs(vid, prefer_root=None)
        if hit is not None:
            return hit
        ensure_cached_indexes_scanned()
        return find_in_disk_libs(vid, prefer_root=None)


catalog_repository = RuntimeCatalogRepository()


def find_video_by_id(vid: str, prefer_root: str | None = None) -> dict | None:
    """Compatibility function backed by the default repository."""
    return catalog_repository.find_video(vid, prefer_root)
