# -*- coding: utf-8 -*-
"""Keep recently opened disk indexes so history/stream still work after switching drives."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from vg.cache import ensure_cache_dir, save_index
from vg.config import VGDATA_DIR
from vg.schema import RUNTIME_ONLY_FIELDS, serialize_video_item
from vg.state import STATE
from vg.util import log

_MAX_DISK_LIBS = 12
_libs_lock = threading.RLock()
_scanned_caches = False
_load_log_ts: dict[str, float] = {}


def _libs_guard(operation: str):
    from vg.diagnostics import timed_lock

    return timed_lock(_libs_lock, operation)

# Backward-compatible private alias; schema.py is the single source of truth.
_RUNTIME_INDEX_FIELDS = RUNTIME_ONLY_FIELDS


def _root_key(root: Path | str) -> str:
    try:
        value = str(Path(root).expanduser().resolve())
    except OSError:
        value = str(Path(root).expanduser())
    return os.path.normcase(os.path.normpath(value))


def _norm_root_str(root: str | Path | None) -> str:
    if not root:
        return ""
    try:
        return str(Path(root).expanduser().resolve())
    except OSError:
        return str(root).strip()


def stamp_lib_meta(
    videos: list[dict] | None = None,
    root: Path | str | None = None,
    cache: Path | str | None = None,
    *,
    overwrite: bool = True,
) -> None:
    """Tag items with which disk/cache they belong to (for cross-disk resolve).

    overwrite=False：已有 _lib_root 的条目不改写（多盘合并后 rebuild 绝不能冲掉归属）。
    """
    root = root if root is not None else STATE.get("root")
    cache = cache if cache is not None else STATE.get("cache_dir")
    root_s = _norm_root_str(root) if root else ""
    cache_s = str(cache) if cache else ""
    for v in videos if videos is not None else (STATE.get("videos") or []):
        if root_s and (overwrite or not (v.get("_lib_root") or "").strip()):
            v["_lib_root"] = root_s
        if cache_s and (overwrite or not (v.get("_lib_cache") or "").strip()):
            v["_lib_cache"] = cache_s


def _disk_item(item: dict, root_s: str, cache: Path) -> dict:
    """Return a canonical per-disk record from a possibly merged runtime item."""
    return serialize_video_item(item, root=root_s, cache=cache)


def item_belongs_to_root(item: dict, root: Path | str) -> bool:
    """Legacy untagged items are accepted; explicitly foreign items are not."""
    tagged = (item.get("_lib_root") or item.get("root") or "").strip()
    if not tagged:
        return True
    try:
        return _norm_root_str(tagged).lower() == _norm_root_str(root).lower()
    except Exception:
        return tagged.lower() == str(root).strip().lower()


def read_root_library(root: Path | str) -> list[dict] | None:
    """Read one per-disk catalog; prefer in-memory cache to avoid reopening SQLite."""
    from vg.catalog_db import catalog_exists, catalog_mtime, load_catalog_videos

    root_s = _norm_root_str(root)
    cache = ensure_cache_dir(Path(root_s))

    # Live scan snapshot beats stale index while this root is being scanned.
    with _libs_lock:
        existing = (STATE.get("disk_libs") or {}).get(root_s)
        if not existing:
            for k, val in (STATE.get("disk_libs") or {}).items():
                if str(k).lower() == root_s.lower():
                    existing = val
                    break
        if existing and existing.get("by_id") and existing.get("live"):
            return list(existing["by_id"].values())

    if not catalog_exists(cache):
        return None

    index_mtime = catalog_mtime(cache)

    with _libs_lock:
        existing = (STATE.get("disk_libs") or {}).get(root_s)
        if (
            existing
            and existing.get("by_id")
            and not existing.get("live")
            and float(existing.get("index_mtime") or 0) == float(index_mtime or 0)
        ):
            return list(existing["by_id"].values())

    videos = load_catalog_videos(cache, root_s)
    if not videos:
        return None
    clean = [
        _disk_item(v, root_s, cache)
        for v in videos
        if isinstance(v, dict) and v.get("id") and item_belongs_to_root(v, root_s)
    ]
    by_id = {v["id"]: v for v in clean}
    _store_lib(root_s, cache, by_id, index_mtime=index_mtime)
    return list(by_id.values())


def store_live_library(root: Path | str, videos: list[dict]) -> None:
    """Publish in-progress scan results into disk_libs without writing the catalog.

    Lets other disks keep their indexes while the scanning disk becomes visible
    immediately in /api/tree and /api/videos.
    """
    root_s = _norm_root_str(root)
    try:
        cache = ensure_cache_dir(Path(root_s))
    except OSError:
        cache = None
    by_id: dict[str, dict] = {}
    for item in videos:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        stamped = dict(item)
        stamp_lib_meta([stamped], root=root_s, cache=cache, overwrite=True)
        stamped["root"] = root_s
        if "_folder_raw" not in stamped:
            stamped["_folder_raw"] = (stamped.get("folder") or "").replace("\\", "/").strip("/")
        source_id = stamped.get("_thumb_id") or stamped["id"]
        by_id[source_id] = stamped
    with _libs_lock:
        libs = STATE.setdefault("disk_libs", {})
        libs[root_s] = {
            "root": root_s,
            "cache_dir": str(cache) if cache else None,
            "by_id": by_id,
            "updated": time.time(),
            "index_mtime": 0,
            "live": True,
            "live_count": len(by_id),
        }
    STATE["lib_gen"] = int(STATE.get("lib_gen") or 0) + 1


def sync_disk_lib_memory(root: Path | str, videos: list[dict]) -> None:
    """Refresh in-memory disk_libs after the catalog was written (clears live flag)."""
    root_s = _norm_root_str(root)
    try:
        cache = ensure_cache_dir(Path(root_s))
    except OSError:
        cache = None
    by_id: dict[str, dict] = {}
    for item in videos:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        if cache is not None:
            by_id[item.get("_thumb_id") or item["id"]] = _disk_item(item, root_s, cache)
        else:
            stamped = dict(item)
            stamp_lib_meta([stamped], root=root_s, cache=None, overwrite=True)
            stamped["root"] = root_s
            by_id[stamped.get("_thumb_id") or stamped["id"]] = stamped
    index_mtime = 0.0
    if cache is not None:
        from vg.catalog_db import catalog_mtime

        index_mtime = catalog_mtime(cache)
    _store_lib(root_s, cache, by_id, index_mtime=index_mtime)


def save_root_library(root: Path | str, videos: list[dict]) -> list[dict]:
    """Persist exactly one root's catalog and refresh its in-memory archive.

    Foreign-root records are rejected so a unified STATE catalog can never be
    written wholesale into one disk's index.
    """
    root_s = _norm_root_str(root)
    cache = ensure_cache_dir(Path(root_s))
    clean: list[dict] = []
    for item in videos:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        tagged = (item.get("_lib_root") or item.get("root") or "").strip()
        if tagged:
            if not item_belongs_to_root(item, root_s):
                continue
        else:
            # Untagged records are only safe when the caller explicitly passes
            # a single-root list; stamp them to that root here.
            item["_lib_root"] = root_s
            item["root"] = root_s
        clean.append(_disk_item(item, root_s, cache))

    by_id = {v["id"]: v for v in clean if v.get("id")}
    with _libs_lock:
        if not save_index(cache, Path(root_s), list(by_id.values())):
            raise OSError(f"保存片库索引失败: {cache}")
        _store_lib(root_s, cache, by_id)
    return list(by_id.values())


def save_libraries_by_root(
    videos: list[dict],
    *,
    fallback_root: Path | str | None = None,
) -> dict[str, int]:
    """Split a runtime catalog by ownership and persist each disk separately."""
    fallback = _norm_root_str(fallback_root) if fallback_root else ""
    groups: dict[str, list[dict]] = {}
    for item in videos:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        raw = (item.get("_lib_root") or item.get("root") or fallback or "").strip()
        if not raw:
            log(f"[索引] 跳过无磁盘归属条目: {item.get('rel') or item.get('id')}")
            continue
        try:
            root_s = _norm_root_str(raw)
        except Exception:
            root_s = raw
        groups.setdefault(root_s, []).append(item)

    saved: dict[str, int] = {}
    for root_s, items in groups.items():
        saved[root_s] = len(save_root_library(root_s, items))
    return saved


def save_library_item(
    item: dict,
    *,
    allow_insert: bool = False,
    bump_gen: bool = True,
) -> bool:
    """Persist one changed item without touching other disks.

    Existing records are updated by source id or relative path. Insertion is
    opt-in so a late metadata/thumbnail worker cannot resurrect an item that
    was deleted while that worker was running.

    bump_gen=False keeps the API response cache valid during bulk background
    probes; callers should advance lib_gen once when the batch finishes.
    """
    from vg.catalog_db import upsert_catalog_videos

    raw_root = (item.get("_lib_root") or item.get("root") or "").strip()
    if not raw_root or not item.get("id"):
        return False
    root_s = _norm_root_str(raw_root)
    cache = ensure_cache_dir(Path(root_s))
    with _libs_lock:
        n = upsert_catalog_videos(
            cache,
            root_s,
            [item],
            allow_insert=allow_insert,
        )
        if n <= 0:
            return False
        # Keep memory archive in sync without a full reload.
        existing = (STATE.get("disk_libs") or {}).get(root_s)
        source_id = (item.get("_thumb_id") or item.get("id") or "").strip()
        stamped = _disk_item(item, root_s, cache)
        from vg.catalog_db import catalog_mtime

        if existing and isinstance(existing.get("by_id"), dict) and source_id:
            by_id = existing["by_id"]
            if source_id in by_id:
                by_id[source_id] = stamped
            else:
                rel = (item.get("rel") or "").replace("\\", "/").strip("/").casefold()
                replaced = False
                for key, old in list(by_id.items()):
                    old_rel = (old.get("rel") or "").replace("\\", "/").strip("/").casefold()
                    if old.get("id") == source_id or (rel and old_rel == rel):
                        by_id.pop(key, None)
                        by_id[source_id] = stamped
                        replaced = True
                        break
                if not replaced and allow_insert:
                    by_id[source_id] = stamped
            existing["index_mtime"] = catalog_mtime(cache)
            existing["live"] = False
        elif allow_insert and source_id:
            _store_lib(root_s, cache, {source_id: stamped}, index_mtime=catalog_mtime(cache))
        if bump_gen:
            STATE["lib_gen"] = int(STATE.get("lib_gen") or 0) + 1
    return True


def save_library_items(
    items: list[dict],
    *,
    allow_insert: bool = False,
    bump_gen: bool = True,
) -> int:
    """Persist many items with one SQLite UPSERT transaction per owning root."""
    from vg.catalog_db import catalog_mtime, upsert_catalog_videos

    groups: dict[str, list[dict]] = {}
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        raw_root = (item.get("_lib_root") or item.get("root") or "").strip()
        if not raw_root:
            continue
        try:
            root_s = _norm_root_str(raw_root)
        except Exception:
            root_s = raw_root
        groups.setdefault(root_s, []).append(item)
    if not groups:
        return 0

    saved = 0
    for root_s, batch in groups.items():
        cache = ensure_cache_dir(Path(root_s))
        with _libs_lock:
            n = upsert_catalog_videos(
                cache,
                root_s,
                batch,
                allow_insert=allow_insert,
            )
            if n <= 0:
                continue
            saved += n
            existing = (STATE.get("disk_libs") or {}).get(root_s)
            if existing and isinstance(existing.get("by_id"), dict):
                by_id = existing["by_id"]
                for item in batch:
                    source_id = (item.get("_thumb_id") or item.get("id") or "").strip()
                    if not source_id:
                        continue
                    stamped = _disk_item(item, root_s, cache)
                    if source_id in by_id:
                        by_id[source_id] = stamped
                        continue
                    rel = (item.get("rel") or "").replace("\\", "/").strip("/").casefold()
                    replaced = False
                    for key, old in list(by_id.items()):
                        old_rel = (old.get("rel") or "").replace("\\", "/").strip("/").casefold()
                        if old.get("id") == source_id or (rel and old_rel == rel):
                            by_id.pop(key, None)
                            by_id[source_id] = stamped
                            replaced = True
                            break
                    if not replaced and allow_insert:
                        by_id[source_id] = stamped
                existing["index_mtime"] = catalog_mtime(cache)
                existing["live"] = False
            if bump_gen:
                STATE["lib_gen"] = int(STATE.get("lib_gen") or 0) + 1
    return saved


def archive_current_library() -> None:
    """Snapshot active library before switching disks.

    多盘统一片库时按 _lib_root 拆开归档，绝不能把所有盘的片都盖成当前盘。
    """
    videos = list(STATE.get("videos") or [])
    if not videos:
        by_id = STATE.get("by_id") or {}
        videos = list(by_id.values()) if by_id else []
    if not videos:
        return

    fallback_root = _norm_root_str(STATE.get("root")) if STATE.get("root") else ""
    fallback_cache = STATE.get("cache_dir")
    groups: dict[str, dict[str, dict]] = {}
    cache_by_root: dict[str, Path | str | None] = {}
    tagged_roots = {
        _norm_root_str(v.get("_lib_root"))
        for v in videos
        if (v.get("_lib_root") or "").strip()
    }

    for v in videos:
        if not v.get("id"):
            continue
        root_s = (v.get("_lib_root") or "").strip()
        if root_s:
            try:
                root_s = _norm_root_str(root_s)
            except Exception:
                pass
        else:
            if len(tagged_roots) > 1 or (
                tagged_roots and fallback_root and fallback_root not in tagged_roots
            ):
                log(f"[跨盘] 跳过无归属条目: {v.get('rel') or v.get('id')}")
                continue
            root_s = fallback_root
        if not root_s:
            continue
        source_id = v.get("_thumb_id") or v["id"]
        groups.setdefault(root_s, {})[source_id] = v
        if root_s not in cache_by_root:
            cache_by_root[root_s] = v.get("_lib_cache") or (
                fallback_cache if root_s == fallback_root else None
            )

    if not groups:
        return

    with _libs_lock:
        libs = STATE.setdefault("disk_libs", {})
        now = time.time()
        for root_s, by_id in groups.items():
            cache = cache_by_root.get(root_s)
            if not cache:
                try:
                    cache = ensure_cache_dir(Path(root_s))
                except OSError:
                    cache = None
            stamp_lib_meta(list(by_id.values()), root=root_s, cache=cache, overwrite=True)
            libs[root_s] = {
                "root": root_s,
                "cache_dir": str(cache) if cache else None,
                "by_id": dict(by_id),
                "updated": now,
            }
        if len(libs) > _MAX_DISK_LIBS:
            keep = set(groups.keys())
            if fallback_root:
                keep.add(fallback_root)
            ordered = sorted(
                ((k, v.get("updated") or 0) for k, v in libs.items() if k not in keep),
                key=lambda x: x[1],
            )
            for k, _ in ordered[: max(0, len(libs) - _MAX_DISK_LIBS)]:
                libs.pop(k, None)


def _store_lib(
    root_s: str,
    cache: Path | None,
    by_id: dict[str, dict],
    *,
    index_mtime: float | None = None,
) -> None:
    from vg.catalog_db import catalog_mtime

    stamp_lib_meta(list(by_id.values()), root=root_s, cache=cache)
    if index_mtime is None and cache:
        index_mtime = catalog_mtime(cache)
    with _libs_guard("disk_lib_store"):
        libs = STATE.setdefault("disk_libs", {})
        libs[root_s] = {
            "root": root_s,
            "cache_dir": str(cache) if cache else None,
            "by_id": by_id,
            "updated": time.time(),
            "index_mtime": float(index_mtime or 0),
            "live": False,
        }


def load_library_from_index(root: Path | str) -> bool:
    """Load a disk's saved catalog into disk_libs without switching the active UI root."""
    from vg.catalog_db import catalog_exists, catalog_mtime, load_catalog_videos

    try:
        root_p = Path(root).expanduser().resolve()
    except OSError as exc:
        from vg.diagnostics import error

        error("disk_library_root_resolve_failed", exc, root=root)
        return False
    if not root_p.is_dir():
        from vg.diagnostics import emit_rate_limited

        emit_rate_limited(
            "WARN",
            "disk_library_load_skipped",
            key=f"root_not_directory|{root_p}",
            interval=30.0,
            force=True,
            reason="root_not_directory",
            root=root_p,
        )
        return False
    root_s = str(root_p)
    # already the active root
    cur = STATE.get("root")
    if (
        cur
        and _norm_root_str(cur).lower() == root_s.lower()
        and (STATE.get("by_id") or STATE.get("videos"))
    ):
        archive_current_library()
        return True

    cache = ensure_cache_dir(root_p)
    if not catalog_exists(cache):
        from vg.diagnostics import emit_rate_limited

        emit_rate_limited(
            "WARN",
            "disk_library_load_skipped",
            key=f"catalog_missing|{root_s}",
            interval=30.0,
            force=True,
            reason="catalog_missing",
            root=root_s,
            cache=cache,
        )
        return False
    index_mtime = catalog_mtime(cache)
    with _libs_guard("disk_lib_load_state"):
        existing = (STATE.get("disk_libs") or {}).get(root_s)
        if (
            existing
            and existing.get("by_id")
            and float(existing.get("index_mtime") or 0) == index_mtime
        ):
            return True
        # Scan flushes catalog.sqlite often; reloading 778+ rows for every
        # /thumb or history lookup stalls the UI. Keep RAM copy briefly.
        if existing and existing.get("by_id"):
            loaded_at = float(existing.get("updated") or 0)
            if loaded_at and (time.time() - loaded_at) < 3.0:
                return True
        # Parallel /thumb lookups must not each deserialize the whole catalog.
        if existing and existing.get("loading"):
            from vg.diagnostics import emit

            emit(
                "INFO",
                "disk_library_load_coalesced",
                force=True,
                root=root_s,
                cache=cache,
                serving_previous=bool(existing.get("by_id")),
                previous_rows=len(existing.get("by_id") or {}),
            )
            return bool(existing.get("by_id"))
        if existing is None:
            STATE.setdefault("disk_libs", {})[root_s] = {
                "root": root_s,
                "cache_dir": str(cache),
                "by_id": {},
                "updated": time.time(),
                "index_mtime": 0.0,
                "live": False,
                "loading": True,
            }
        else:
            existing["loading"] = True
            # Keep serving the previous generation while we refresh.
            if existing.get("by_id"):
                pass
    try:
        videos = load_catalog_videos(cache, root_s)
        clean = []
        for raw in videos:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            if not item_belongs_to_root(raw, root_s):
                continue
            clean.append(_disk_item(raw, root_s, cache))
        by_id = {v["id"]: v for v in clean}
        if not by_id:
            from vg.diagnostics import emit

            emit(
                "WARN",
                "disk_library_catalog_empty",
                force=True,
                root=root_s,
                cache=cache,
                source_rows=len(videos),
                accepted_rows=len(clean),
            )
            with _libs_guard("disk_lib_empty_cleanup"):
                lib = (STATE.get("disk_libs") or {}).get(root_s)
                if lib is not None:
                    lib.pop("loading", None)
                    if not lib.get("by_id"):
                        (STATE.get("disk_libs") or {}).pop(root_s, None)
            return False
        final_mtime = catalog_mtime(cache)
        _store_lib(root_s, cache, by_id, index_mtime=final_mtime or index_mtime)
        now = time.time()
        last = float(_load_log_ts.get(root_s) or 0)
        if now - last >= 5.0:
            _load_log_ts[root_s] = now
            log(f"[跨盘] 已加载历史盘索引: {root_s}（{len(by_id)} 部）")
        return True
    finally:
        with _libs_guard("disk_lib_load_finalize"):
            lib = (STATE.get("disk_libs") or {}).get(root_s)
            if lib is not None:
                lib.pop("loading", None)


def ensure_cached_indexes_scanned() -> None:
    """One-time: pull catalogs from program preview_cache and (if enabled) disk caches."""
    global _scanned_caches
    if _scanned_caches:
        return
    with _libs_guard("disk_lib_cache_discovery"):
        if _scanned_caches:
            return
        _scanned_caches = True
    try:
        from vg.catalog_db import CATALOG_DB_NAME, catalog_exists, read_catalog_root
        from vg.config import THUMB_DIR_NAME
        from vg.privacy import cache_location

        seen_roots: set[str] = set()
        if VGDATA_DIR.is_dir():
            for db_path in VGDATA_DIR.glob(f"*/{CATALOG_DB_NAME}"):
                root_raw = read_catalog_root(db_path.parent)
                if not root_raw:
                    continue
                try:
                    root_p = Path(root_raw)
                    if not root_p.is_dir():
                        continue
                except OSError:
                    continue
                key = str(root_p.resolve()).lower()
                if key in seen_roots:
                    continue
                seen_roots.add(key)
                load_library_from_index(root_p)

        if cache_location() == "disk":
            for raw in list(STATE.get("mounted_roots") or []):
                try:
                    root_p = Path(raw).expanduser().resolve()
                    if not root_p.is_dir():
                        continue
                    if not catalog_exists(root_p / THUMB_DIR_NAME):
                        continue
                    key = str(root_p).lower()
                    if key in seen_roots:
                        continue
                    seen_roots.add(key)
                    load_library_from_index(root_p)
                except OSError:
                    continue
    except OSError as e:
        log(f"[跨盘] 扫描缓存索引失败: {e}")


def discover_indexed_roots() -> list[str]:
    """Discover online roots that already have a persisted catalog.

    This is the startup fallback when ``prefs.mounted_roots`` is incomplete.
    Program-cache catalogs contain their source root, so a previously scanned
    disk can be mounted again without rescanning it first.
    """
    from vg.catalog_db import CATALOG_DB_NAME, catalog_exists, read_catalog_root

    roots: list[str] = []
    seen: set[str] = set()

    def add(raw: str | Path | None) -> None:
        if not raw:
            return
        try:
            path = Path(raw).expanduser().resolve()
            if not path.is_dir():
                return
            value = str(path)
        except OSError:
            return
        key = value.lower()
        if key not in seen:
            seen.add(key)
            roots.append(value)

    try:
        if VGDATA_DIR.is_dir():
            for db_path in VGDATA_DIR.glob(f"*/{CATALOG_DB_NAME}"):
                add(read_catalog_root(db_path.parent))
    except OSError as e:
        log(f"[多盘] 发现缓存片库失败: {e}")

    try:
        from vg.config import THUMB_DIR_NAME
        from vg.drives import list_ready_drives

        for drive in list_ready_drives():
            cache = drive / THUMB_DIR_NAME
            if not catalog_exists(cache):
                continue
            root_raw = read_catalog_root(cache)
            add(root_raw if root_raw else drive)
    except OSError:
        pass

    return roots


def ensure_library(root: str | Path | None) -> bool:
    if not root:
        return False
    return load_library_from_index(root)


def find_in_disk_libs(vid: str, prefer_root: str | None = None) -> dict | None:
    """Lookup id in archived / cached disk libraries."""
    if not vid:
        return None
    prefer = _norm_root_str(prefer_root) if prefer_root else ""
    if prefer:
        ensure_library(prefer)
    with _libs_lock:
        libs = STATE.get("disk_libs") or {}
        if prefer:
            lib = libs.get(prefer)
            if lib:
                hit = (lib.get("by_id") or {}).get(vid)
                if hit is not None:
                    return hit
            # A root hint is an ownership constraint, not just ordering.
            return None
        for key, lib in libs.items():
            hit = (lib.get("by_id") or {}).get(vid)
            if hit is not None:
                return hit
    return None


def cache_dir_for_item(item: dict | None) -> Path | None:
    if not item:
        return None
    raw = (item.get("_lib_cache") or "").strip()
    if raw:
        p = Path(raw)
        if p.is_dir():
            return p
    root = (item.get("_lib_root") or item.get("root") or "").strip()
    if root:
        try:
            return ensure_cache_dir(Path(root))
        except OSError:
            pass
    cache = STATE.get("cache_dir")
    return Path(cache) if cache else None


def root_for_item(item: dict | None) -> Path | None:
    if not item:
        return None
    raw = (item.get("_lib_root") or "").strip()
    if raw:
        try:
            # Keep ownership even while a removable disk is offline. Returning
            # None would make callers fall back to the active disk and resolve
            # the same relative path against the wrong library.
            return Path(raw)
        except OSError:
            return None
    root = STATE.get("root")
    return Path(root) if root else None


def resolve_under_root_path(root: Path | None, rel: str) -> Path | None:
    """Resolve rel under an explicit root (path traversal safe)."""
    if root is None:
        return None
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return None
    try:
        root_r = root.resolve()
        full = (root_r / rel).resolve()
        full.relative_to(root_r)
        return full if full.is_file() else None
    except (ValueError, OSError):
        return None


def resolve_item_rel(item: dict | None, rel: str | None = None) -> Path | None:
    """Resolve a relative path for a video item (may belong to a non-active disk)."""
    if not item:
        return None
    use_rel = rel if rel is not None else (item.get("rel") or "")
    root = root_for_item(item)
    if root is None:
        # fall back to active root
        from vg.util import resolve_under_root

        return resolve_under_root(use_rel)
    return resolve_under_root_path(root, use_rel)


def offline_roots(roots: list[str]) -> list[str]:
    """Return roots that are not currently mountable."""
    out = []
    for r in roots:
        if not r:
            continue
        try:
            if not Path(r).expanduser().resolve().is_dir():
                out.append(r)
        except OSError:
            out.append(r)
    return out
