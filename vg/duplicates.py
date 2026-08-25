# -*- coding: utf-8 -*-
"""Duplicate detection shared by catalog badges and cleanup APIs."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import TypedDict

from vg.config import MIN_VIDEO_FILE_BYTES
from vg.schema import VideoItem
from vg.signatures import backfill_file_signatures


_DUP_CACHE_FILENAME = "duplicate_signatures_cache.json"
_DUP_CACHE_SCHEMA = 1


def _duplicate_cache_path() -> Path | None:
    try:
        # This cache is for the unified multi-root library, so keep it in the
        # stable application cache root rather than the currently active disk
        # cache. Otherwise a D→C scan would resample D's candidates again.
        from vg.config import VGDATA_DIR

        VGDATA_DIR.mkdir(parents=True, exist_ok=True)
        return VGDATA_DIR / _DUP_CACHE_FILENAME
    except OSError:
        pass
    return None


def _load_duplicate_cache() -> tuple[dict[str, dict], dict[str, object]]:
    started = time.perf_counter()
    path = _duplicate_cache_path()
    stats: dict[str, object] = {"hit": False, "entries": 0, "miss_reason": ""}
    if path is None:
        stats["miss_reason"] = "no_cache_dir"
        return {}, stats
    stats["path"] = str(path)
    try:
        with path.open("r", encoding="utf-8") as stream:
            raw = json.load(stream)
        entries = raw.get("entries") if isinstance(raw, dict) else None
        if not isinstance(entries, dict) or int(raw.get("schema") or 0) != _DUP_CACHE_SCHEMA:
            stats["miss_reason"] = "missing_or_schema_mismatch"
            return {}, stats
        clean = {str(k): v for k, v in entries.items() if isinstance(v, dict)}
        stats["hit"] = True
        stats["entries"] = len(clean)
        return clean, stats
    except FileNotFoundError:
        stats["miss_reason"] = "missing"
    except (OSError, ValueError, TypeError):
        stats["miss_reason"] = "corrupt"
    finally:
        stats["elapsed_ms"] = (time.perf_counter() - started) * 1000.0
    return {}, stats


def _save_duplicate_cache(entries: dict[str, dict], *, reason: str) -> dict[str, object]:
    started = time.perf_counter()
    path = _duplicate_cache_path()
    stats: dict[str, object] = {"entries": len(entries), "reason": reason}
    if path is None:
        stats["skip_reason"] = "no_cache_dir"
        return stats
    payload = {
        "schema": _DUP_CACHE_SCHEMA,
        "generated_at_ms": int(time.time() * 1000),
        "entries": entries,
    }
    tmp_path: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_path, path)
        stats["bytes_written"] = path.stat().st_size
    except (OSError, ValueError, TypeError) as exc:
        stats["skip_reason"] = "write_error"
        stats["error"] = str(exc)
        if tmp_path:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
    stats["path"] = str(path)
    stats["elapsed_ms"] = (time.perf_counter() - started) * 1000.0
    return stats


class DuplicateGroup(TypedDict):
    reason: str
    items: list[VideoItem]


def duplicate_name_key(video: VideoItem) -> str:
    """Normalized display name used by the existing duplicate rule."""
    return (
        video.get("name")
        or Path(video.get("filename") or "").stem
        or ""
    ).strip().casefold()


def video_identity(video: VideoItem) -> str:
    """Identity of one physical catalog entry, including its owning disk."""
    root = (video.get("_lib_root") or video.get("root") or "").strip().casefold()
    rel = (video.get("rel") or "").replace("\\", "/").strip("/").casefold()
    return f"{root}|{rel}|{video.get('id') or ''}"


def find_duplicate_groups(videos: list[VideoItem]) -> list[DuplicateGroup]:
    """Group distinct files by exact-size candidates plus content identity.

    Two-stage strategy to minimise disk I/O:

    1. Group by exact file size — O(N), no I/O.
    2. Within each size group, sub-group by the scan ``file_sig`` fingerprint
       (head + middle + tail, 3 × 64 KiB = 192 KiB).  Missing fingerprints
       are backfilled with these samples only; no full-content hash is used.

    Playlist, TS-set and synthetic series cards are excluded.
    """
    by_size: dict[int, list[VideoItem]] = {}
    for video in videos:
        if (video.get("kind") or "") in ("m3u8", "ts_set", "series"):
            continue
        size = int(video.get("size") or 0)
        if size >= MIN_VIDEO_FILE_BYTES:
            by_size.setdefault(size, []).append(video)

    groups: list[DuplicateGroup] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    started = time.perf_counter()
    candidate_rows = sum(len(items) for items in by_size.values() if len(items) >= 2)
    # Reuse signatures from the dedicated JSON cache. Only exact same-size
    # candidates reach this stage, so ordinary unique files never touch disk.
    if candidate_rows:
        cache_entries, cache_stats = _load_duplicate_cache()
    else:
        cache_entries = {}
        cache_stats = {"hit": False, "entries": 0, "miss_reason": "no_same_size_candidates", "elapsed_ms": 0.0}
    try:
        from vg.diagnostics import emit

        emit(
            "PERF",
            "duplicate_signature_cache_load"
            if candidate_rows
            else "duplicate_signature_cache_skipped",
            force=True,
            hit=bool(cache_stats.get("hit")),
            entries=int(cache_stats.get("entries") or 0),
            miss_reason=cache_stats.get("miss_reason") or "",
            path=cache_stats.get("path") or "",
            skipped=not bool(candidate_rows),
            elapsed_ms=float(cache_stats.get("elapsed_ms") or 0.0),
        )
    except Exception:
        pass
    cache_updates = 0
    cache_hits = 0
    for candidates in by_size.values():
        if len(candidates) < 2:
            continue
        for video in candidates:
            if str(video.get("file_sig") or "").strip():
                continue
            cached = cache_entries.get(video_identity(video))
            try:
                if (
                    cached
                    and int(cached.get("size") or -1) == int(video.get("size") or -2)
                    and abs(float(cached.get("mtime") or 0) - float(video.get("mtime") or 0)) < 1.0
                    and str(cached.get("file_sig") or "").strip()
                ):
                    video["file_sig"] = str(cached["file_sig"])
                    cache_hits += 1
            except (TypeError, ValueError):
                continue
    sig_groups_emitted = 0
    sig_groups_resolved = 0
    # Missing signatures are sampled (never full-file hashed) before grouping.
    sig_backfill_rows = 0
    sig_sample_bytes = 0
    sig_backfill_failures = 0
    sig_failure_samples: list[str] = []

    def add_group(reason: str, candidates: list[VideoItem]) -> None:
        unique = {video_identity(video): video for video in candidates}
        items = list(unique.values())
        if len(items) < 2:
            return
        key = (reason, tuple(sorted(unique)))
        if key in seen:
            return
        seen.add(key)
        groups.append({"reason": reason, "items": items})

    for candidates in by_size.values():
        if len(candidates) < 2:
            continue

        # --- Tier 2: existing scan fingerprint (zero I/O) ----------------
        by_sig: dict[str, list[VideoItem]] = {}
        missing_sig: list[VideoItem] = []
        for video in candidates:
            sig = str(video.get("file_sig") or "").strip()
            if sig:
                by_sig.setdefault(sig, []).append(video)
            else:
                missing_sig.append(video)

        # Fill missing fingerprints from head/middle/tail samples.  This is a
        # bounded read and intentionally replaces the old full-file fallback.
        if missing_sig:
            stats = backfill_file_signatures(missing_sig)
            sig_backfill_rows += int(stats.get("computed") or 0)
            sig_sample_bytes += int(stats.get("sampled_bytes") or 0)
            sig_backfill_failures += int(stats.get("unavailable") or 0) + int(
                stats.get("failed") or 0
            )
            for sample in (
                list(stats.get("unavailable_samples") or [])
                + list(stats.get("failed_samples") or [])
            ):
                if len(sig_failure_samples) >= 3:
                    break
                if sample not in sig_failure_samples:
                    sig_failure_samples.append(sample)
            for video in missing_sig:
                sig = str(video.get("file_sig") or "").strip()
                if sig:
                    by_sig.setdefault(sig, []).append(video)
                    cache_entries[video_identity(video)] = {
                        "size": int(video.get("size") or 0),
                        "mtime": float(video.get("mtime") or 0),
                        "file_sig": sig,
                    }
                    cache_updates += 1

        # Fingerprint sub-groups with 2+ members are duplicates.
        for sig, sig_members in by_sig.items():
            if len(sig_members) >= 2:
                add_group("同内容", sig_members)
                sig_groups_resolved += 1
            sig_groups_emitted += 1

    # Warn only when a fingerprint could not be produced (for example an
    # offline disk).  There is deliberately no full-file fallback anymore.
    if sig_backfill_failures >= 1:
        try:
            from vg.diagnostics import emit
            emit(
                "WARN",
                "duplicate_sig_unavailable",
                force=True,
                missing_sig_count=sig_backfill_failures,
                total_candidates=candidate_rows,
                samples=sig_failure_samples,
                hint="videos lack file_sig; sampled fingerprint unavailable; full-file hash disabled",
            )
        except Exception:
            pass
    if cache_updates:
        save_stats = _save_duplicate_cache(cache_entries, reason="duplicate_final_stage")
        try:
            from vg.diagnostics import emit

            emit(
                "PERF",
                "duplicate_signature_cache_save",
                force=True,
                **save_stats,
            )
        except Exception:
            pass
    try:
        from vg.diagnostics import perf as diagnostic_perf
    except ImportError:
        diagnostic_perf = None
    if diagnostic_perf is not None:
        diagnostic_perf(
            "duplicate_content_signature",
            (time.perf_counter() - started) * 1000.0,
            force=True,
            input_rows=len(videos),
            same_size_candidate_rows=candidate_rows,
            sig_groups_emitted=sig_groups_emitted,
            sig_groups_resolved=sig_groups_resolved,
            sig_backfill_rows=sig_backfill_rows,
            sig_sample_bytes=sig_sample_bytes,
            sig_backfill_failures=sig_backfill_failures,
            sig_failure_samples=sig_failure_samples,
            full_hash_attempts=0,
            full_hash_bytes=0,
            duplicate_groups=len(groups),
            duplicate_rows=sum(len(group["items"]) for group in groups),
            duplicate_cache_hit=bool(cache_stats.get("hit")),
            duplicate_cache_entries=int(cache_stats.get("entries") or 0),
            duplicate_cache_hits=cache_hits,
            duplicate_cache_updates=cache_updates,
        )
    return groups


def mark_duplicates(videos: list[VideoItem]) -> None:
    """Apply runtime duplicate badges using the same groups as cleanup."""
    for video in videos:
        video.pop("dup", None)
        video.pop("dup_n", None)
        video.pop("dup_reason", None)

    for group in find_duplicate_groups(videos):
        reason = group["reason"]
        count = len(group["items"])
        for video in group["items"]:
            video["dup"] = True
            video["dup_n"] = max(int(video.get("dup_n") or 0), count)
            reasons = set(str(video.get("dup_reason") or "").split("+"))
            reasons.discard("")
            reasons.add(reason)
            video["dup_reason"] = "+".join(sorted(reasons))
