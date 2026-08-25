# -*- coding: utf-8 -*-
"""Unified facets + folder-tree disk cache.

The on-disk format avoids a cold ``tree_build`` step on every restart when
the catalog has not changed.  Previously ``STATE["facets"]`` and every
``tree_for_scope(lib)`` were recomputed on every restart; observed cost for
a 2785-video / 4-disk multi-root catalog was ``tree_build=199.8ms`` of which
``tree_ms=173ms`` was the folder tree and the rest was facets counting.
With this cache loaded, that cost drops to a few ms of json.load.

Unified files are written below the stable application cache directory
(``preview_cache/unified/``), not below the currently selected disk's catalog
directory::

    facets_cache.json        for the unified ``STATE["facets"]``
    tree_cache_<lib>.json    for one scope (lib="" -> `tree_cache_all.json`)

All writes are atomic (write to ``.tmp`` then rename) so a killed process
cannot leave a half-written JSON that would fail validation on the next run.

Validation signature (all values must match, otherwise the cache is dropped
and recomputed):

* ``schema``             : int, bumped when this file's JSON layout changes
* ``count``              : len(videos) in the scope
* ``genres_ver``         : vg.genres.GENRES_VERSION
* ``taxonomy_ver``       : vg.taxonomy.TAXONOMY_VERSION
* ``catalogs``           : mapping of ``{root_str: catalog_mtime}`` for every
                           mounted catalog.  A single SQLite write bumps the
                           mtime on one root and automatically invalidates.

Every cache read/write/invalidate emits a structured PERF line so regressions
due to the cache layer are visible in startup logs without needing a debugger.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from vg.config import VGDATA_DIR
from vg.genres import GENRES_VERSION
from vg.taxonomy import TAXONOMY_VERSION

# Bump this when the JSON envelope or the facets/tree payload schemas change
# in an incompatible way (e.g. a required field is renamed).  Caches written
# under an older schema will be ignored and overwritten on the next warm
# write.
_CACHE_SCHEMA = 1

_FACETS_FILENAME = "facets_cache.json"
_TREE_FILENAME_PREFIX = "tree_cache_"
_UNIFIED_CACHE_DIRNAME = "unified"

# Thread-local + per-path locks are unnecessary because writes only happen
# from single-threaded STATE-mutating code (apply_catalog_to_state /
# publish_unified_library / catalog_save), and reads happen from web threads
# after STATE["videos"] has been published.  Still, add a coarse lock to
# prevent two concurrent first-writes from racing.
import threading

_facets_write_lock = threading.Lock()
_tree_write_locks: dict[str, threading.Lock] = {}
_tree_write_locks_guard = threading.Lock()


def _tree_lock(lib_key: str) -> threading.Lock:
    with _tree_write_locks_guard:
        lock = _tree_write_locks.get(lib_key)
        if lock is None:
            lock = threading.Lock()
            _tree_write_locks[lib_key] = lock
        return lock


# ---------------------------------------------------------------------------
# Validation signature
# ---------------------------------------------------------------------------

def _collect_catalog_signatures(
    roots: Iterable[str | Path],
) -> dict[str, float]:
    """Return ``{root_str: catalog_mtime}`` for every mounted root.

    A missing catalog mtime is reported as ``0.0`` so a root whose catalog
    has not yet been written always causes a cache miss.
    """
    from vg.catalog_db import catalog_mtime
    from vg.cache import ensure_cache_dir

    sig: dict[str, float] = {}
    for r in roots:
        r_s = str(r).rstrip("\\/")
        try:
            cache_dir = ensure_cache_dir(Path(r))
        except Exception:
            cache_dir = None
        sig[r_s] = float(catalog_mtime(cache_dir)) if cache_dir else 0.0
    return sig


def _mounted_roots_for_signature() -> list[str]:
    """Roots used to scope the catalog signature.

    Precedence: the live unified lib (STATE["mounted_roots"]) if published,
    otherwise all currently-mounted roots from ``vg.roots.get_mounted_roots``.
    If neither source has data we return an empty list and cache writes will
    be skipped (signature would be trivially broken on the next run).
    """
    from vg.state import STATE

    mounted = [str(r) for r in (STATE.get("mounted_roots") or []) if r]
    if not mounted:
        try:
            from vg.roots import get_mounted_roots

            mounted = [str(r) for r in get_mounted_roots() if r]
        except Exception:
            mounted = []
    return mounted


def _current_signature(videos_count: int) -> dict[str, Any]:
    return {
        "schema": _CACHE_SCHEMA,
        "count": int(videos_count),
        "genres_ver": int(GENRES_VERSION),
        "taxonomy_ver": int(TAXONOMY_VERSION),
        "catalogs": _collect_catalog_signatures(_mounted_roots_for_signature()),
    }


def _signatures_equal(a: dict, b: dict) -> tuple[bool, str]:
    """Compare two signatures; return (equal, mismatch_reason)."""
    for field in ("schema", "count", "genres_ver", "taxonomy_ver"):
        if a.get(field) != b.get(field):
            return False, f"field={field} want={a.get(field)} got={b.get(field)}"
    a_cats: dict = a.get("catalogs") or {}
    b_cats: dict = b.get("catalogs") or {}
    if set(a_cats.keys()) != set(b_cats.keys()):
        missing = set(a_cats) ^ set(b_cats)
        return False, f"catalog_roots_diverge sample={sorted(missing)[:4]}"
    for root_s, mtime in a_cats.items():
        if float(mtime) != float(b_cats.get(root_s, -1)):
            return False, f"catalog_mtime_mismatch root={root_s} want={mtime} got={b_cats.get(root_s)}"
    return True, ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _primary_cache_dir() -> Path | None:
    """Return the stable cache directory for the unified library.

    ``tree_cache_all.json`` and facets describe all mounted roots, so their
    ownership must not follow ``STATE["cache_dir"]`` when the primary disk
    changes from D: to C:. Per-disk catalog and thumbnail caches remain in
    their existing ``preview_cache/<root_hash>/`` directories.
    """
    try:
        path = VGDATA_DIR / _UNIFIED_CACHE_DIRNAME
        path.mkdir(parents=True, exist_ok=True)
        return path
    except OSError:
        return None


def _atomic_write_json(path: Path, data: Any) -> int:
    """Atomically write ``data`` as JSON; return the final file size in bytes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_s = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_s)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        size = tmp_path.stat().st_size
        os.replace(tmp_path, path)
        return size
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _safe_read_json(path: Path) -> Any:
    """Read a JSON file, returning ``None`` on any corruption/decode error."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        from vg.diagnostics import error

        error("catalog_cache_json_decode_failed", exc, path=str(path))
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None


# ---------------------------------------------------------------------------
# Facets cache
# ---------------------------------------------------------------------------

def _facets_cache_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / _FACETS_FILENAME


def load_facets_disk_cache(
    expected_videos_count: int,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Try to load ``STATE["facets"]`` from disk.

    Returns ``(facets_dict or None, stats_for_logging)``.  ``stats_for_logging``
    always contains ``event`` and enough fields to explain why the load
    hit or missed, regardless of success.
    """
    t0 = time.perf_counter()
    stats: dict[str, Any] = {
        "event": "facets_disk_cache_load",
        "expected_count": int(expected_videos_count),
        "cache_scope": "unified",
    }
    cache_dir = _primary_cache_dir()
    if cache_dir is None:
        stats["miss_reason"] = "no_primary_cache_dir"
        return None, stats
    path = _facets_cache_path(cache_dir)
    stats["cache_path"] = str(path)
    raw = _safe_read_json(path)
    if not isinstance(raw, dict):
        stats["miss_reason"] = "missing_or_corrupt"
        stats["read_ms"] = f"{(time.perf_counter() - t0) * 1000.0:.1f}"
        return None, stats
    saved_sig = raw.get("signature")
    if not isinstance(saved_sig, dict):
        stats["miss_reason"] = "signature_missing"
        stats["read_ms"] = f"{(time.perf_counter() - t0) * 1000.0:.1f}"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None, stats
    expected_sig = _current_signature(expected_videos_count)
    equal, why = _signatures_equal(expected_sig, saved_sig)
    if not equal:
        stats["miss_reason"] = "signature_mismatch"
        stats["mismatch"] = why
        stats["read_ms"] = f"{(time.perf_counter() - t0) * 1000.0:.1f}"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None, stats
    facets = raw.get("facets")
    if not isinstance(facets, dict):
        stats["miss_reason"] = "facets_field_missing"
        stats["read_ms"] = f"{(time.perf_counter() - t0) * 1000.0:.1f}"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None, stats
    stats["hit"] = True
    try:
        stats["cache_bytes"] = int(path.stat().st_size)
    except OSError:
        stats["cache_bytes"] = 0
    stats["read_ms"] = f"{(time.perf_counter() - t0) * 1000.0:.1f}"
    return facets, stats


def save_facets_disk_cache(
    facets: dict[str, Any],
    videos_count: int,
    *,
    only_if_missing: bool = False,
) -> dict[str, Any]:
    """Write ``facets`` alongside its validation signature.

    ``only_if_missing=True`` skips the write if a valid cache file already
    exists (used during ``_build_tree_payload`` after a read-miss to warm the
    cache without rewriting on every tree request).
    """
    t0 = time.perf_counter()
    stats: dict[str, Any] = {
        "event": "facets_disk_cache_save",
        "count": int(videos_count),
        "cache_scope": "unified",
    }
    cache_dir = _primary_cache_dir()
    if cache_dir is None:
        stats["skip_reason"] = "no_primary_cache_dir"
        return stats
    path = _facets_cache_path(cache_dir)
    stats["cache_path"] = str(path)

    if only_if_missing:
        try:
            existing = _safe_read_json(path)
            if isinstance(existing, dict):
                expected = _current_signature(videos_count)
                equal, _why = _signatures_equal(expected, existing.get("signature") or {})
                if equal:
                    stats["skip_reason"] = "already_valid"
                    stats["elapsed_ms"] = f"{(time.perf_counter() - t0) * 1000.0:.1f}"
                    return stats
        except Exception:
            pass

    signature = _current_signature(videos_count)
    payload = {
        "signature": signature,
        "generated_at_ms": int(time.time() * 1000),
        "facets": facets,
    }
    with _facets_write_lock:
        try:
            size = _atomic_write_json(path, payload)
        except (OSError, ValueError) as exc:
            from vg.diagnostics import error

            error("facets_disk_cache_write_failed", exc, path=str(path))
            stats["skip_reason"] = "write_error"
            stats["elapsed_ms"] = f"{(time.perf_counter() - t0) * 1000.0:.1f}"
            return stats
    stats["bytes_written"] = size
    stats["elapsed_ms"] = f"{(time.perf_counter() - t0) * 1000.0:.1f}"
    return stats


# ---------------------------------------------------------------------------
# Tree cache (per scope = lib)
# ---------------------------------------------------------------------------

def _lib_filename_suffix(lib: str) -> str:
    lib = (lib or "").strip().rstrip("\\/")
    if not lib:
        return "all"
    # Windows path libs (``F:\``) are case-insensitive; normalise before
    # hashing.  Keep the hex short: 12 hex chars = 48 bits, enough to avoid
    # collisions on a handful of roots per machine.
    digest = hashlib.sha1(lib.casefold().encode("utf-8")).hexdigest()[:12]
    # Prefix with a short displayable fragment so humans can tell files apart
    # without looking at the hash alone.
    safe_chars = [c for c in lib if c.isalnum() or c in ("-", "_")]
    prefix = "".join(safe_chars)[:16] or "lib"
    return f"{prefix}-{digest}"


def _tree_cache_path(cache_dir: Path, lib: str) -> Path:
    return Path(cache_dir) / f"{_TREE_FILENAME_PREFIX}{_lib_filename_suffix(lib)}.json"


def load_tree_disk_cache(
    lib: str,
    expected_videos_count: int,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Load a pre-computed folder ``tree`` for scope ``lib``."""
    t0 = time.perf_counter()
    stats: dict[str, Any] = {
        "event": "tree_disk_cache_load",
        "lib": lib or "all",
        "expected_count": int(expected_videos_count),
        "cache_scope": "unified",
    }
    cache_dir = _primary_cache_dir()
    if cache_dir is None:
        stats["miss_reason"] = "no_primary_cache_dir"
        return None, stats
    path = _tree_cache_path(cache_dir, lib)
    stats["cache_path"] = str(path)
    raw = _safe_read_json(path)
    if not isinstance(raw, dict):
        stats["miss_reason"] = "missing_or_corrupt"
        stats["read_ms"] = f"{(time.perf_counter() - t0) * 1000.0:.1f}"
        return None, stats
    saved_sig = raw.get("signature")
    if not isinstance(saved_sig, dict):
        stats["miss_reason"] = "signature_missing"
        stats["read_ms"] = f"{(time.perf_counter() - t0) * 1000.0:.1f}"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None, stats
    expected_sig = _current_signature(expected_videos_count)
    equal, why = _signatures_equal(expected_sig, saved_sig)
    if not equal:
        stats["miss_reason"] = "signature_mismatch"
        stats["mismatch"] = why
        stats["read_ms"] = f"{(time.perf_counter() - t0) * 1000.0:.1f}"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None, stats
    tree = raw.get("tree")
    if not isinstance(tree, dict):
        stats["miss_reason"] = "tree_field_missing"
        stats["read_ms"] = f"{(time.perf_counter() - t0) * 1000.0:.1f}"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None, stats
    # A tree's top-level ``count`` must match the scoped videos count exactly.
    # If the cache was generated with a different view (e.g. before a rescan
    # added videos to a specific lib) the counts would diverge even though
    # the whole-library signature matches (because signature count == total).
    tree_count = int(tree.get("count") or -1)
    if expected_videos_count and tree_count != int(expected_videos_count):
        stats["miss_reason"] = f"tree_count_mismatch want={expected_videos_count} got={tree_count}"
        stats["read_ms"] = f"{(time.perf_counter() - t0) * 1000.0:.1f}"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None, stats
    stats["hit"] = True
    try:
        stats["cache_bytes"] = int(path.stat().st_size)
    except OSError:
        stats["cache_bytes"] = 0
    stats["read_ms"] = f"{(time.perf_counter() - t0) * 1000.0:.1f}"
    return tree, stats


def save_tree_disk_cache(
    lib: str,
    tree: dict[str, Any],
    expected_videos_count: int,
    *,
    only_if_missing: bool = False,
) -> dict[str, Any]:
    """Persist ``tree`` with the same catalog signature used for facets."""
    t0 = time.perf_counter()
    stats: dict[str, Any] = {
        "event": "tree_disk_cache_save",
        "lib": lib or "all",
        "count": int(expected_videos_count),
        "cache_scope": "unified",
    }
    cache_dir = _primary_cache_dir()
    if cache_dir is None:
        stats["skip_reason"] = "no_primary_cache_dir"
        return stats
    path = _tree_cache_path(cache_dir, lib)
    stats["cache_path"] = str(path)

    if only_if_missing:
        try:
            existing = _safe_read_json(path)
            if isinstance(existing, dict):
                expected = _current_signature(expected_videos_count)
                equal, _why = _signatures_equal(expected, existing.get("signature") or {})
                equal_count = int(expected_videos_count) == int(
                    (existing.get("tree") or {}).get("count") or -1
                )
                if equal and equal_count:
                    stats["skip_reason"] = "already_valid"
                    stats["elapsed_ms"] = f"{(time.perf_counter() - t0) * 1000.0:.1f}"
                    return stats
        except Exception:
            pass

    signature = _current_signature(expected_videos_count)
    payload = {
        "signature": signature,
        "generated_at_ms": int(time.time() * 1000),
        "tree": tree,
    }
    with _tree_lock(lib or ""):
        try:
            size = _atomic_write_json(path, payload)
        except (OSError, ValueError) as exc:
            from vg.diagnostics import error

            error("tree_disk_cache_write_failed", exc, path=str(path))
            stats["skip_reason"] = "write_error"
            stats["elapsed_ms"] = f"{(time.perf_counter() - t0) * 1000.0:.1f}"
            return stats
    stats["bytes_written"] = size
    stats["elapsed_ms"] = f"{(time.perf_counter() - t0) * 1000.0:.1f}"
    return stats


# ---------------------------------------------------------------------------
# Logging helpers (thin wrappers so the caller can emit one unified line)
# ---------------------------------------------------------------------------

def emit_load_log(level: str, event: str, *, force: bool = True, **fields) -> None:
    from vg.diagnostics import emit as _emit

    _emit(level, event, force=force, **fields)


def emit_save_log(level: str, event: str, *, force: bool = True, **fields) -> None:
    from vg.diagnostics import emit as _emit

    _emit(level, event, force=force, **fields)
