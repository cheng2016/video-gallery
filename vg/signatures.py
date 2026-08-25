"""Cheap, sampled content fingerprints used by scanning and duplicate grouping."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Iterable


FINGERPRINT_CHUNK = 64 * 1024
FINGERPRINT_SAMPLE_BYTES = FINGERPRINT_CHUNK * 3


def file_fingerprint(path: Path, st: os.stat_result | None = None) -> str:
    """Return a cheap content fingerprint without reading a whole large file."""
    try:
        stat = st or path.stat()
        size = int(stat.st_size)
        import hashlib

        digest = hashlib.blake2b(digest_size=16)
        with path.open("rb") as stream:
            if size <= FINGERPRINT_CHUNK * 2:
                digest.update(stream.read())
            else:
                digest.update(stream.read(FINGERPRINT_CHUNK))
                stream.seek(max(0, (size // 2) - (FINGERPRINT_CHUNK // 2)))
                digest.update(stream.read(FINGERPRINT_CHUNK))
                stream.seek(max(0, size - FINGERPRINT_CHUNK))
                digest.update(stream.read(FINGERPRINT_CHUNK))
        return f"b2:{size}:{digest.hexdigest()}"
    except (OSError, ValueError):
        return ""


def backfill_file_signatures(videos: Iterable[dict]) -> dict[str, int | float]:
    """Fill missing sampled fingerprints in-place, never a full-content hash."""
    started = time.perf_counter()
    candidates = computed = unavailable = failed = sampled_bytes = 0
    unavailable_samples: list[str] = []
    failed_samples: list[str] = []
    try:
        from vg.disk_libs import resolve_item_rel, resolve_under_root_path
    except Exception:
        resolve_item_rel = None  # type: ignore[assignment]
        resolve_under_root_path = None  # type: ignore[assignment]

    for video in videos:
        if not isinstance(video, dict) or str(video.get("file_sig") or "").strip():
            continue
        if (video.get("kind") or "") in ("m3u8", "ts_set", "series"):
            continue
        candidates += 1
        label = (
            f"{video.get('_lib_root') or video.get('root') or ''}"
            f"/{video.get('rel') or video.get('filename') or video.get('id') or ''}"
        )
        path = resolve_item_rel(video, video.get("rel") or "") if resolve_item_rel else None
        if path is None and resolve_under_root_path:
            raw_root = str(video.get("_lib_root") or video.get("root") or "").strip()
            if raw_root:
                path = resolve_under_root_path(Path(raw_root), video.get("rel") or "")
        if path is None:
            unavailable += 1
            if len(unavailable_samples) < 3:
                unavailable_samples.append(label)
            continue
        try:
            stat = path.stat()
            sampled_bytes += min(int(stat.st_size), FINGERPRINT_SAMPLE_BYTES)
            sig = file_fingerprint(path, stat)
        except (OSError, ValueError):
            sig = ""
        if sig:
            video["file_sig"] = sig
            computed += 1
        else:
            failed += 1
            if len(failed_samples) < 3:
                failed_samples.append(label)

    return {
        "candidates": candidates,
        "computed": computed,
        "unavailable": unavailable,
        "failed": failed,
        "sampled_bytes": sampled_bytes,
        "unavailable_samples": unavailable_samples,
        "failed_samples": failed_samples,
        "elapsed_ms": (time.perf_counter() - started) * 1000.0,
    }
