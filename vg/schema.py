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

PROBE_RESULT_FIELDS = (
    "duration", "duration_h", "has_audio", "audio_codec", "audio_channels",
    "sample_rate", "audio_hard", "width", "height", "fps", "bitrate",
    "video_codec", "probe_ver", "probe_duration_done", "probe_audio_done",
    "probe_video_meta_done", "bad", "bad_reason",
)
THUMB_RESULT_FIELDS = (
    "thumb", "has_thumb", "thumb_v", "thumb_failed", "thumb_failed_reason",
    "thumb_failed_at", "thumb_failed_size", "thumb_failed_mtime",
)


def media_item_signature(item: dict) -> tuple:
    return (
        str(item.get("_lib_root") or item.get("root") or "").replace("/", "\\").rstrip("\\").casefold(),
        str(item.get("_thumb_id") or item.get("id") or ""),
        str(item.get("rel") or "").replace("\\", "/").strip("/").casefold(),
        int(item.get("size") or 0),
        float(item.get("mtime") or 0),
        str(item.get("kind") or ""),
        tuple(item.get("segments") or ()),
    )


def media_source_unchanged(item: dict) -> bool:
    path = item.get("_media_source_path")
    if not path:
        return True
    try:
        stat = Path(path).stat()
        return (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) == item.get("_media_source_stat")
    except OSError:
        return False


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
    thumb_failed: bool
    thumb_failed_reason: str
    thumb_failed_at: float
    thumb_failed_size: int
    thumb_failed_mtime: float
    probe_ver: int
    probe_duration_done: bool
    probe_audio_done: bool
    probe_video_meta_done: bool
    audio_codec: str
    audio_hard: bool
    video_codec: str
    width: int
    height: int
    fps: float
    bitrate: int
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
    "_media_source_path",
    "_media_source_stat",
    "_thumb_pending_path",
    "_thumb_target_path",
    "_q",
    "_q_sig",
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
