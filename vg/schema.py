# -*- coding: utf-8 -*-
"""Shared video-item data contract and index serialization rules.

Feature modules may add derived runtime fields, but every catalog writer
must pass through :func:`serialize_video_item`.  Unknown persisted fields are
kept for forward compatibility; only fields explicitly declared runtime-only
are removed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, TypedDict

INDEX_SCHEMA_VERSION = 3

VideoKind = Literal["m3u8", "ts_set", "series"]


class VideoItem(TypedDict, total=False):
    """Canonical video record shared by scan, browse, cleanup and conversion."""

    # Stable persisted identity and filesystem metadata.
    id: str
    name: str
    filename: str
    rel: str
    folder: str
    ext: str
    size: int
    size_h: str
    file_sig: str
    mtime: float
    mtime_h: str
    duration: float | None
    duration_h: str
    kind: VideoKind
    segments: list[str]
    seg_count: int
    genres: list[str]
    themes: list[str]
    backgrounds: list[str]
    taxonomy_ver: int
    root: str
    _lib_root: str
    _lib_cache: str
    _folder_raw: str

    # Persisted media/thumbnail status.
    thumb: str
    has_thumb: bool
    thumb_v: int
    probe_ver: int
    probe_duration_done: bool
    probe_audio_done: bool
    audio_codec: str
    audio_hard: bool
    bad: bool
    bad_reason: str

    # Runtime-only search, ownership aliases and derived presentation fields.
    _q: str
    _thumb_id: str
    _lib_label: str
    lib_label: str
    dup: bool
    dup_n: int
    dup_reason: str
    series_id: str
    series_title: str
    series_n: int
    cover_id: str
    is_series: bool
    thumb_id: str
    episodes: list[dict[str, Any]]


# This is the single source of truth for fields that must never enter the catalog.
RUNTIME_ONLY_FIELDS = frozenset({
    "_q",
    "_thumb_id",
    "_lib_label",
    "lib_label",
    "dup",
    "dup_n",
    "dup_reason",
    "series_id",
    "series_title",
    "series_n",
    "cover_id",
    "is_series",
    "thumb_id",
    "episodes",
})


def serialize_video_item(
    item: dict[str, Any],
    *,
    source_id: str | None = None,
    root: str | Path | None = None,
    cache: str | Path | None = None,
) -> dict[str, Any]:
    """Return an index-safe copy without mutating the runtime item.

    ``source_id`` restores the per-disk id after a unified-catalog collision.
    ``root`` and ``cache`` are optional ownership normalization used by the
    per-disk persistence layer.
    """
    out = {key: value for key, value in item.items() if key not in RUNTIME_ONLY_FIELDS}

    persisted_id = (source_id or item.get("_thumb_id") or item.get("id") or "").strip()
    if persisted_id:
        out["id"] = persisted_id

    if root is not None:
        root_s = str(root)
        out["root"] = root_s
        out["_lib_root"] = root_s
    if cache is not None:
        out["_lib_cache"] = str(cache)
    if "_folder_raw" not in out:
        out["_folder_raw"] = (out.get("folder") or "").replace("\\", "/").strip("/")
    return out
