# -*- coding: utf-8 -*-
"""Privacy preferences: encrypt thumbs, cache on/off video disk."""
from __future__ import annotations

from pathlib import Path

from vg.drives import load_prefs, save_prefs
from vg.config import VGDATA_DIR, WRITABLE_ROOT


def encrypt_thumbs_enabled() -> bool:
    """Default ON: preview files are not usable as plain images."""
    prefs = load_prefs()
    if "encrypt_thumbs" not in prefs:
        return True
    return bool(prefs.get("encrypt_thumbs"))


def cache_location() -> str:
    """program = app preview_cache (default); disk = under each scan root."""
    loc = str(load_prefs().get("cache_location") or "program").strip().lower()
    return "disk" if loc == "disk" else "program"


def probe_duration_enabled() -> bool:
    """Whether ffprobe may read video duration. Default OFF."""
    return bool(load_prefs().get("probe_video_duration", False))


def probe_audio_enabled() -> bool:
    """Whether ffprobe may inspect the first audio stream. Default OFF."""
    return bool(load_prefs().get("probe_video_audio", False))


def full_logging_enabled() -> bool:
    """Detailed success-path logging; failures are always logged."""
    return bool(load_prefs().get("full_logging", False))


def refresh_logging_runtime() -> bool:
    enabled = full_logging_enabled()
    from vg.diagnostics import set_full_logging

    set_full_logging(enabled)
    return enabled


def privacy_snapshot() -> dict:
    loc = cache_location()
    return {
        "encrypt_thumbs": encrypt_thumbs_enabled(),
        "cache_location": loc,
        "probe_video_duration": probe_duration_enabled(),
        "probe_video_audio": probe_audio_enabled(),
        "full_logging": full_logging_enabled(),
        "cache_path": str(VGDATA_DIR.resolve()),
        "cache_hint": (
            "每个视频盘根目录下的 .video_gallery_cache"
            if loc == "disk"
            else f"程序目录 {VGDATA_DIR.resolve()}"
        ),
        "writable_root": str(WRITABLE_ROOT.resolve()),
    }


def set_privacy(
    *,
    encrypt_thumbs: bool | None = None,
    cache_location_value: str | None = None,
    probe_video_duration: bool | None = None,
    probe_video_audio: bool | None = None,
    full_logging: bool | None = None,
) -> dict:
    """Persist settings. cache_location change needs remount/rescan to take full effect."""
    kwargs = {}
    if encrypt_thumbs is not None:
        kwargs["encrypt_thumbs"] = bool(encrypt_thumbs)
    if cache_location_value is not None:
        loc = str(cache_location_value).strip().lower()
        kwargs["cache_location"] = "disk" if loc == "disk" else "program"
    if probe_video_duration is not None:
        kwargs["probe_video_duration"] = bool(probe_video_duration)
    if probe_video_audio is not None:
        kwargs["probe_video_audio"] = bool(probe_video_audio)
    if full_logging is not None:
        kwargs["full_logging"] = bool(full_logging)
    if kwargs:
        save_prefs(**kwargs)
    refresh_logging_runtime()
    return privacy_snapshot()


def pack_thumb_bytes(jpeg: bytes) -> bytes:
    """Store format for a JPEG preview: encrypted VG1 blob or plain JPEG."""
    if encrypt_thumbs_enabled():
        from vg.cache import encrypt_blob

        return encrypt_blob(jpeg)
    return jpeg


def unpack_thumb_bytes(blob: bytes) -> bytes | None:
    """Accept both plain JPEG and VG1-encrypted thumbnails."""
    if not blob or len(blob) < 24:
        return None
    if blob[:2] == b"\xff\xd8":
        return blob
    from vg.cache import decrypt_blob

    raw = decrypt_blob(blob)
    if raw and len(raw) > 100 and raw[:2] == b"\xff\xd8":
        return raw
    return None


def resolve_cache_dir_for_root(root: Path) -> Path:
    """Where this scan root's index + thumbs live."""
    from vg.config import THUMB_DIR_NAME

    if cache_location() == "disk":
        cache = root / THUMB_DIR_NAME
        cache.mkdir(parents=True, exist_ok=True)
        return cache
    # program dir (default)
    from vg.cache import ensure_program_cache_subdir

    return ensure_program_cache_subdir(root)
