# -*- coding: utf-8 -*-
"""Encrypted thumb vault and index persistence."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
import time
from pathlib import Path

from vg.config import (
    KEY_FILE,
    LEGACY_DISK_CACHE_NAMES,
    THUMB_EXT,
    THUMB_JPEG_CACHE_MAX,
    THUMB_JPEG_CACHE_MAX_BYTES,
    VGDATA_DIR,
)
from vg import state as _state
from vg.state import STATE, _thumb_jpeg_cache, _thumb_jpeg_lock
from vg.util import _clear_path_attrs_windows, log


_index_locks_guard = threading.Lock()
_index_locks: dict[str, threading.RLock] = {}


def _index_lock(cache: Path) -> threading.RLock:
    try:
        key = str(cache.resolve()).casefold()
    except OSError:
        key = str(cache).casefold()
    with _index_locks_guard:
        lock = _index_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _index_locks[key] = lock
        return lock

def _thumb_cache_key(vid: str, cache: Path | None = None) -> str:
    return f"{str(cache.resolve()).casefold()}|{vid}" if cache else vid


def thumb_cache_get(vid: str, cache: Path | None = None) -> bytes | None:
    key = _thumb_cache_key(vid, cache)
    with _thumb_jpeg_lock:
        raw = _thumb_jpeg_cache.get(key)
        if raw is not None:
            _thumb_jpeg_cache.move_to_end(key)
        return raw


def thumb_cache_put(vid: str, raw: bytes, cache: Path | None = None) -> None:
    key = _thumb_cache_key(vid, cache)
    with _thumb_jpeg_lock:
        if not _thumb_jpeg_cache:
            _state._thumb_jpeg_cache_bytes = 0
        previous = _thumb_jpeg_cache.get(key)
        if previous is not None:
            _state._thumb_jpeg_cache_bytes -= len(previous)
        _thumb_jpeg_cache[key] = raw
        _state._thumb_jpeg_cache_bytes += len(raw)
        _thumb_jpeg_cache.move_to_end(key)
        while (
            len(_thumb_jpeg_cache) > THUMB_JPEG_CACHE_MAX
            or _state._thumb_jpeg_cache_bytes > THUMB_JPEG_CACHE_MAX_BYTES
        ):
            _, removed = _thumb_jpeg_cache.popitem(last=False)
            _state._thumb_jpeg_cache_bytes -= len(removed)


def thumb_cache_invalidate(vid: str | None = None, cache: Path | None = None) -> None:
    with _thumb_jpeg_lock:
        if vid:
            if cache:
                removed = _thumb_jpeg_cache.pop(_thumb_cache_key(vid, cache), None)
                if removed is not None:
                    _state._thumb_jpeg_cache_bytes -= len(removed)
            else:
                suffix = f"|{vid}"
                for key in [k for k in _thumb_jpeg_cache if k == vid or k.endswith(suffix)]:
                    removed = _thumb_jpeg_cache.pop(key, None)
                    if removed is not None:
                        _state._thumb_jpeg_cache_bytes -= len(removed)
        else:
            _thumb_jpeg_cache.clear()
            _state._thumb_jpeg_cache_bytes = 0


def _ensure_vault_key() -> bytes:
    VGDATA_DIR.mkdir(parents=True, exist_ok=True)
    if KEY_FILE.exists():
        _clear_path_attrs_windows(KEY_FILE)
        try:
            key = KEY_FILE.read_bytes()
            if len(key) >= 32:
                return key[:32]
        except OSError as e:
            from vg.diagnostics import error

            error(
                "thumb_key_read_failed",
                e,
                key_file=KEY_FILE,
                impact="old_thumbnails_may_be_unreadable",
            )
    key = os.urandom(32)
    try:
        _clear_path_attrs_windows(KEY_FILE)
        KEY_FILE.write_bytes(key)
    except OSError as e:
        from vg.diagnostics import error

        error("thumb_key_write_failed", e, key_file=KEY_FILE)
        raise
    return key


def _xor_stream(key: bytes, nonce: bytes, length: int) -> bytes:
    seed = hashlib.sha256(key + nonce).digest()
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hashlib.sha256(seed + counter.to_bytes(8, "big")).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encrypt_blob_with_key(data: bytes, key: bytes) -> bytes:
    """用指定密钥加密（VG1 + nonce + ciphertext）。"""
    nonce = os.urandom(16)
    stream = _xor_stream(key[:32], nonce, len(data))
    cipher = bytes(a ^ b for a, b in zip(data, stream))
    return b"VG1\0" + nonce + cipher


def encrypt_blob(data: bytes) -> bytes:
    """本地预览图加密：VG1 + nonce + ciphertext（无密钥无法当图片打开）。"""
    return encrypt_blob_with_key(data, _ensure_vault_key())


def decrypt_blob_with_key(blob: bytes, key: bytes) -> bytes | None:
    if not blob.startswith(b"VG1\0") or len(blob) < 20:
        return None
    nonce = blob[4:20]
    cipher = blob[20:]
    stream = _xor_stream(key[:32], nonce, len(cipher))
    return bytes(a ^ b for a, b in zip(cipher, stream))


def decrypt_blob(blob: bytes) -> bytes | None:
    if not blob.startswith(b"VG1\0") or len(blob) < 20:
        return None
    return decrypt_blob_with_key(blob, _ensure_vault_key())


def thumb_path(cache: Path, vid: str) -> Path:
    return cache / f"{vid}{THUMB_EXT}"


def thumb_version(cache: Path | None, vid: str) -> int:
    """用于前端缓存破坏；有有效文件则返回 mtime。"""
    if not cache:
        return 0
    p = thumb_path(cache, vid)
    try:
        if p.exists() and p.stat().st_size > 24:
            return int(p.stat().st_mtime)
    except OSError:
        pass
    return 0


def thumb_file_ready(cache: Path | None, vid: str) -> bool:
    """只检查文件是否存在且非空，不解密（扫描/列表用，更快）。"""
    if not cache or not vid:
        return False
    p = thumb_path(cache, vid)
    try:
        return p.exists() and p.stat().st_size > 24
    except OSError:
        return False


def thumb_stat(cache: Path | None, vid: str) -> tuple[bool, int]:
    """Single-stat form of (thumb_file_ready, thumb_version).

    The previous ``attach_thumb_meta`` flow called ``thumb_file_ready``
    followed by ``thumb_version`` for every positive match.  Each call
    issues its own ``Path.exists()`` + ``Path.stat()``, so a single video
    cost two disk stats.  On Windows against spinning disks (and worse
    across multiple drives D/E/F/G), this compounded to >1.1s for a
    single 18-video page request:

        [PERF] api_videos_sql ... thumb_meta_ms=1184.9 ...

    This helper performs exactly one ``os.stat`` and returns both the
    "ready" boolean and the version (mtime) int.
    """
    if not cache or not vid:
        return (False, 0)
    p = thumb_path(cache, vid)
    try:
        st = os.stat(p)
    except OSError:
        return (False, 0)
    ready = st.st_size > 24
    return (ready, int(st.st_mtime) if ready else 0)


def read_thumb_jpeg(cache: Path, vid: str) -> bytes | None:
    """读取预览图（支持加密 VG1 与明文 JPEG）；带内存 LRU。"""
    from vg.privacy import unpack_thumb_bytes

    def mark_request_layer(layer: str, elapsed_ms: float) -> None:
        # Keep the cache module independent from Flask at import time.  When
        # called by /thumb/<vid>, this request-local marker is picked up by
        # vg.web's after-request logger and exposed as X-VG-Cache-Layer.
        try:
            from flask import g

            g._vg_cache_layer = layer
            g._vg_cache_fields = {
                "cache": str(cache),
                "video_id": vid,
                "read_ms": f"{elapsed_ms:.1f}",
            }
        except (ImportError, RuntimeError, AttributeError):
            return

    started = time.perf_counter()
    cached = thumb_cache_get(vid, cache)
    if cached is not None:
        from vg.diagnostics import aggregate

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        aggregate("thumb_l1_hit", elapsed_ms)
        mark_request_layer("L1_server_thumb_memory", elapsed_ms)
        return cached
    p = thumb_path(cache, vid)
    try:
        if not p.exists():
            from vg.diagnostics import aggregate

            elapsed_ms = (time.perf_counter() - started) * 1000.0
            aggregate("thumb_l2_not_found", elapsed_ms)
            mark_request_layer("L3_thumb_missing", elapsed_ms)
            return None
        size = p.stat().st_size
        if size <= 24:
            from vg.diagnostics import emit

            emit(
                "WARN",
                "thumb_cache_invalid",
                force=True,
                reason="file_too_small",
                path=p,
                size=size,
                video_id=vid,
            )
            mark_request_layer(
                "L3_thumb_invalid",
                (time.perf_counter() - started) * 1000.0,
            )
            return None
        _clear_path_attrs_windows(p)
        blob = p.read_bytes()
        raw = unpack_thumb_bytes(blob)
        if raw:
            thumb_cache_put(vid, raw, cache)
            from vg.diagnostics import aggregate

            elapsed_ms = (time.perf_counter() - started) * 1000.0
            aggregate("thumb_l2_hit", elapsed_ms)
            mark_request_layer("L2_disk_thumb", elapsed_ms)
            return raw
        from vg.diagnostics import emit

        emit(
            "WARN",
            "thumb_cache_invalid",
            force=True,
            reason="decrypt_or_jpeg_validation_failed",
            path=p,
            size=len(blob),
            prefix=blob[:4].hex(),
            video_id=vid,
        )
    except OSError as e:
        from vg.diagnostics import error

        error("thumb_read_failed", e, video_id=vid, cache=cache)
    from vg.diagnostics import aggregate

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    aggregate("thumb_cache_miss", elapsed_ms)
    mark_request_layer("L3_thumb_missing", elapsed_ms)
    return None


def has_encrypted_thumb(cache: Path, vid: str) -> bool:
    """服务端校验：优先内存缓存，否则快速文件探测，必要时再解密。"""
    if thumb_cache_get(vid, cache) is not None:
        return True
    if not thumb_file_ready(cache, vid):
        return False
    return read_thumb_jpeg(cache, vid) is not None


def ensure_program_cache_subdir(root: Path) -> Path:
    """程序目录 preview_cache/<盘符_hash>/（默认，不写视频盘）。"""
    VGDATA_DIR.mkdir(parents=True, exist_ok=True)
    from vg.privacy import encrypt_thumbs_enabled

    if encrypt_thumbs_enabled():
        _ensure_vault_key()
    try:
        drive = root.resolve().drive.rstrip(":\\/") or "disk"
    except OSError:
        drive = "disk"
    safe = re.sub(r"[^\w\-]+", "_", drive)[:16] or "disk"
    digest = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:8]
    cache = VGDATA_DIR / f"{safe}_{digest}"
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def ensure_cache_dir(root: Path) -> Path:
    """按隐私偏好选择缓存位置：程序目录（默认）或视频盘根下的隐藏目录。"""
    from vg.privacy import cache_location, resolve_cache_dir_for_root

    if cache_location() == "program":
        cleanup_legacy_disk_cache(root)
    return resolve_cache_dir_for_root(root)


def cleanup_legacy_disk_cache(root: Path) -> None:
    """删除早期版本误写在视频盘根目录的缓存（仅「程序目录缓存」模式）。"""
    if not root:
        return
    from vg.privacy import cache_location

    if cache_location() == "disk":
        return
    for name in LEGACY_DISK_CACHE_NAMES:
        p = root / name
        try:
            if not p.is_dir():
                continue
        except OSError as exc:
            from vg.diagnostics import error

            error("legacy_cache_stat_failed", exc, path=p)
            continue
        try:
            before_bytes = 0
            before_files = 0
            try:
                for child in p.iterdir():
                    if child.is_file():
                        before_files += 1
                        before_bytes += child.stat().st_size
            except OSError:
                pass
            _clear_path_attrs_windows(p)
            shutil.rmtree(p)
            from vg.diagnostics import emit

            emit(
                "WARN",
                "legacy_cache_deleted",
                force=True,
                path=p,
                reason="cache_location_is_program",
                files=before_files,
                bytes=before_bytes,
                exists_after=p.exists(),
            )
        except OSError as e:
            from vg.diagnostics import error

            error("legacy_cache_delete_failed", e, path=p)


def _normalize_folder_counts(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        try:
            folder = str(key or "").replace("\\", "/").strip("/")
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count > 0:
            out[folder] = count
    return out


def read_index_counts(cache: Path) -> tuple[int | None, dict[str, int] | None]:
    """Return (file_count, folder_counts). Both None when the catalog has no counts."""
    from vg.catalog_db import read_catalog_counts

    return read_catalog_counts(cache)


def list_thumb_ids(cache: Path | None) -> set[str]:
    """Stem names of .vgt files in one cache directory (one readdir, no video disk).

    Uses ``os.scandir`` instead of ``Path.glob + is_file``.  The previous
    glob-based form issued a fresh ``stat`` for every entry, which on a
    2785-file cache directory added up to >700 ms per call:

        [PERF] fill_thumbs_for_videos elapsed_ms=725.3 ... cached=2785

    ``scandir`` already carries the file type from the directory read on
    Windows, so no per-entry stat is needed.
    """
    ids: set[str] = set()
    if not cache:
        return ids
    try:
        with os.scandir(cache) as it:
            for entry in it:
                name = entry.name
                if not name.endswith(THUMB_EXT):
                    continue
                try:
                    if entry.is_file(follow_symlinks=False):
                        ids.add(name[: -len(THUMB_EXT)])
                except OSError:
                    # Stale entry / permission glitch — still count by name so
                    # the caller does not needlessly regenerate the thumb.
                    ids.add(name[: -len(THUMB_EXT)])
    except OSError as exc:
        from vg.diagnostics import error

        error("thumb_cache_list_failed", exc, cache=cache)
    return ids


def cleanup_thumb_files(
    cache: Path | None,
    keep_ids: set[str],
    *,
    max_bytes: int = 20 * 1024 * 1024 * 1024,
) -> tuple[int, int]:
    """Remove orphan thumbs, then oldest unprotected files above soft quota."""
    if not cache:
        return 0, 0
    cache = Path(cache)
    removed = 0
    freed = 0
    candidates: list[tuple[float, int, Path, str]] = []
    try:
        paths = list(cache.glob(f"*{THUMB_EXT}"))
    except OSError as exc:
        from vg.diagnostics import error

        error("thumb_cache_list_failed", exc, cache=cache)
        return 0, 0
    for path in paths:
        try:
            stat = path.stat()
            vid = path.stem
            if vid not in keep_ids:
                path.unlink(missing_ok=True)
                thumb_cache_invalidate(vid, cache)
                removed += 1
                freed += stat.st_size
            else:
                candidates.append((stat.st_mtime, stat.st_size, path, vid))
        except OSError as exc:
            from vg.diagnostics import error

            error("thumb_cache_cleanup_failed", exc, path=path)
    total = sum(row[1] for row in candidates)
    before_total = total + freed
    orphan_removed = removed
    quota_removed = 0
    if total > max_bytes:
        # Soft quota only removes oldest files; they can be regenerated later.
        for _mtime, size, path, vid in sorted(candidates):
            if total <= max_bytes:
                break
            try:
                path.unlink(missing_ok=True)
                thumb_cache_invalidate(vid, cache)
                total -= size
                removed += 1
                quota_removed += 1
                freed += size
            except OSError as exc:
                from vg.diagnostics import error

                error("thumb_cache_quota_failed", exc, path=path)
    from vg.diagnostics import emit

    if removed:
        emit(
            "INFO",
            "thumb_cache_cleanup",
            force=True,
            cache=cache,
            files_before=len(paths),
            keep_ids=len(keep_ids),
            orphan_removed=orphan_removed,
            quota_removed=quota_removed,
            removed=removed,
            bytes_before=before_total,
            bytes_freed=freed,
            bytes_after=max(0, before_total - freed),
            quota=max_bytes,
        )
    return removed, freed


def save_index(
    cache: Path,
    root: Path,
    videos: list[dict],
    *,
    file_count: int | None = None,
    folder_counts: dict[str, int] | None = None,
) -> bool:
    """Persist one root's catalog to SQLite (no whole-JSON rewrite)."""
    from vg.catalog_db import save_catalog

    from vg.diagnostics import timed_lock

    with timed_lock(_index_lock(cache), "cache_index_write", cache=cache):
        try:
            cache.mkdir(parents=True, exist_ok=True)
            ok = save_catalog(
                cache,
                root,
                videos,
                file_count=file_count,
                folder_counts=folder_counts,
            )
            if not ok:
                from vg.diagnostics import error

                error("catalog_save_returned_false", cache=cache, root=root)
            return ok
        except (OSError, ValueError) as e:
            from vg.diagnostics import error

            error("catalog_save_failed", e, cache=cache, root=root)
            return False


# Per-root cache_dir cache: avoids repeated is_dir() / ensure_cache_dir() calls
# for every video in a page (all videos on the same root share one cache dir).
_cache_dir_by_root: dict[str, Path | None] = {}
_cache_dir_by_root_lock = threading.Lock()


def _resolve_cache_dir_for_item(v: dict) -> Path | None:
    """Resolve cache_dir with per-root memoisation (one is_dir per root)."""
    raw = (v.get("_lib_cache") or "").strip()
    if raw:
        p = Path(raw)
        try:
            if p.is_dir():
                return p
        except OSError:
            pass
    root = (v.get("_lib_root") or v.get("root") or "").strip()
    if root:
        with _cache_dir_by_root_lock:
            cached = _cache_dir_by_root.get(root)
            if cached is not None:
                return cached
        try:
            d = ensure_cache_dir(Path(root))
            with _cache_dir_by_root_lock:
                _cache_dir_by_root[root] = d
            return d
        except OSError:
            pass
    cache = STATE.get("cache_dir")
    return Path(cache) if cache else None


# Negative thumb cache: remember videos confirmed to have no thumb so we skip
# the file-system stat on subsequent calls.  Cleared whenever lib_gen changes
# (scan may have generated new thumbs).
_thumb_neg_cache: dict[str, bool] = {}
_thumb_neg_cache_gen: int = -1
_thumb_neg_cache_lock = threading.Lock()
_THUMB_NEG_CACHE_MAX = 8192


def attach_thumb_meta(v: dict) -> dict:
    """给列表项补 has_thumb / thumb_v（只看文件是否存在，避免列表接口解密过慢）。"""
    from vg.roots import thumb_id_for_item
    from vg.diagnostics import aggregate

    vid = thumb_id_for_item(v) or (v.get("id") or "")
    # Trust scan/index flags on the hot list path — re-statting every card on
    # each channel switch made post-scan browsing hitch on large libraries.
    if v.get("has_thumb") and vid:
        if not v.get("thumb_id"):
            v["thumb_id"] = vid
        if not v.get("thumb_v"):
            v["thumb_v"] = 1
        aggregate("attach_thumb_meta_fast_path", 0.0)
        return v

    # Negative cache: if we already confirmed this video has no thumb in the
    # current generation, skip the file-system checks entirely.
    global _thumb_neg_cache_gen
    cur_gen = int(STATE.get("lib_gen") or 0)
    if cur_gen != _thumb_neg_cache_gen:
        with _thumb_neg_cache_lock:
            _thumb_neg_cache.clear()
            _thumb_neg_cache_gen = cur_gen
    if vid:
        with _thumb_neg_cache_lock:
            if _thumb_neg_cache.get(vid):
                v["has_thumb"] = False
                v["thumb_v"] = 0
                aggregate("attach_thumb_meta_neg_cache", 0.0)
                return v

    cache = _resolve_cache_dir_for_item(v) or STATE.get("cache_dir")
    # Prefer the stamped owner root so SQL-backed list rows still resolve .vgt
    # without falling back to the active disk cache.
    if cache is None:
        root = (v.get("root") or v.get("_lib_root") or "").strip()
        if root:
            try:
                from vg.privacy import resolve_cache_dir_for_root

                cache = resolve_cache_dir_for_root(Path(root))
            except OSError:
                cache = None
    # In-memory LRU hit short-circuits before any disk stat.
    if cache and vid and thumb_cache_get(vid, cache) is not None:
        v["has_thumb"] = True
        v["thumb_v"] = thumb_version(cache, vid) or 1
        v["thumb_id"] = vid
        aggregate("attach_thumb_meta_mem_hit", 0.0)
        return v
    # Single os.stat for both "ready" and "version" instead of calling
    # thumb_file_ready + thumb_version separately (two stats per video).
    ready, ver = thumb_stat(cache, vid) if (cache and vid) else (False, 0)
    if ready:
        v["has_thumb"] = True
        v["thumb_v"] = ver or 1
        v["thumb_id"] = vid
        aggregate("attach_thumb_meta_disk_hit", 0.0)
        return v
    # Record negative result so the next call for this video is O(1).
    if vid:
        with _thumb_neg_cache_lock:
            _thumb_neg_cache[vid] = True
            while len(_thumb_neg_cache) > _THUMB_NEG_CACHE_MAX:
                _thumb_neg_cache.pop(next(iter(_thumb_neg_cache)), None)
    v["has_thumb"] = False
    v["thumb_v"] = 0
    aggregate("attach_thumb_meta_disk_miss", 0.0)
    return v
