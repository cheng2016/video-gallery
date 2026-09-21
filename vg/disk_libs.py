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
# One SQLite catalog load per root at a time. Concurrent /thumb + /api/videos
# used to miss disk_libs together and reopen the same 800-row catalog.
_catalog_flight_lock = threading.Lock()
_catalog_flights: dict[str, threading.Event] = {}
_CATALOG_FLIGHT_WAIT_S = 60.0


def _catalog_flight_key(root_s: str) -> str:
    return os.path.normcase(os.path.normpath(str(root_s or "")))


def _begin_catalog_flight(root_s: str) -> tuple[bool, threading.Event, str]:
    key = _catalog_flight_key(root_s)
    with _catalog_flight_lock:
        existing = _catalog_flights.get(key)
        if existing is not None:
            return False, existing, key
        event = threading.Event()
        _catalog_flights[key] = event
        return True, event, key


def _end_catalog_flight(key: str, event: threading.Event) -> None:
    with _catalog_flight_lock:
        if _catalog_flights.get(key) is event:
            _catalog_flights.pop(key, None)
    event.set()


def _wait_catalog_flight(root_s: str, event: threading.Event, *, caller: str) -> None:
    from vg.diagnostics import emit

    emit(
        "INFO",
        "disk_library_load_wait",
        force=True,
        root=root_s,
        caller=caller,
        timeout_s=_CATALOG_FLIGHT_WAIT_S,
    )
    if not event.wait(timeout=_CATALOG_FLIGHT_WAIT_S):
        log(f"[跨盘] 等待目录库加载超时 {root_s} caller={caller}")


def _disk_lib_snapshot(root_s: str) -> list[dict] | None:
    with _libs_guard("disk_libs_snapshot"):
        existing = (STATE.get("disk_libs") or {}).get(root_s)
        if not existing:
            for k, val in (STATE.get("disk_libs") or {}).items():
                if str(k).lower() == root_s.lower():
                    existing = val
                    break
        if existing and existing.get("by_id"):
            return list(existing["by_id"].values())
    return None

# Short-term cooldown for roots whose catalog is missing, so every page
# refresh does not re-stat the same absent cache dir and re-emit WARN.
_catalog_missing_until: dict[str, float] = {}
_CATALOG_MISSING_COOLDOWN_S = 60.0
# Empty catalog.sqlite (0 accepted rows): same idea — do not reopen SQLite
# on every ensure_library / publish pass during the same startup burst.
# Keyed by cache path; invalidated when catalog mtime changes (scan wrote rows).
_catalog_empty_until: dict[str, float] = {}
_catalog_empty_mtime: dict[str, float] = {}
_CATALOG_EMPTY_COOLDOWN_S = 60.0


def _mark_catalog_empty(cache_s: str, mtime: float = 0.0) -> None:
    _catalog_empty_until[cache_s] = time.time() + _CATALOG_EMPTY_COOLDOWN_S
    _catalog_empty_mtime[cache_s] = float(mtime or 0)


def _catalog_empty_cooled(cache_s: str, mtime: float | None = None) -> bool:
    until = _catalog_empty_until.get(cache_s)
    if not until or until <= time.time():
        return False
    if mtime is not None and float(mtime or 0) != float(_catalog_empty_mtime.get(cache_s) or 0):
        _clear_catalog_empty(cache_s)
        return False
    return True


def _clear_catalog_empty(cache_s: str) -> None:
    _catalog_empty_until.pop(cache_s, None)
    _catalog_empty_mtime.pop(cache_s, None)


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


_norm_root_cache: dict[str, str] = {}


def _norm_root_str(root: str | Path | None) -> str:
    if not root:
        return ""
    key = root if isinstance(root, str) else str(root)
    cached = _norm_root_cache.get(key)
    if cached is not None:
        return cached
    try:
        resolved = str(Path(key).expanduser().resolve())
    except OSError:
        resolved = key.strip()
    _norm_root_cache[key] = resolved
    return resolved


def _disk_lib_entry(root: str | Path | None) -> dict | None:
    """Find an in-memory disk_libs slot without reloading SQLite."""
    if not root:
        return None
    libs = STATE.get("disk_libs") or {}
    if not libs:
        return None
    raw = str(root).strip()
    hit = libs.get(raw)
    if hit and hit.get("by_id"):
        return hit
    raw_l = raw.lower()
    for key, val in libs.items():
        if val and val.get("by_id") and str(key).lower() == raw_l:
            return val
    try:
        resolved = _norm_root_str(raw)
    except Exception:
        return None
    if resolved and resolved != raw:
        hit = libs.get(resolved)
        if hit and hit.get("by_id"):
            return hit
        resolved_l = resolved.lower()
        for key, val in libs.items():
            if val and val.get("by_id") and str(key).lower() == resolved_l:
                return val
    return None


def memory_catalog_ready(root: str | Path | None) -> bool:
    """True when this root already has a usable in-memory catalog.

    ``ensure_library`` used to archive the whole active library (Path.resolve
    per row) on every /api/videos-by-ids even when disk_libs was populated.
    """
    return bool(_disk_lib_entry(root))


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


def _adopt_catalog_item(
    item: dict,
    root_s: str,
    cache: Path,
    *,
    rename_acc: list[str] | None = None,
) -> dict:
    """Hydrate a SQLite catalog row in place (no serialize copy)."""
    item["root"] = root_s
    item["_lib_root"] = root_s
    item["_lib_cache"] = str(cache)
    if "_folder_raw" not in item:
        item["_folder_raw"] = (item.get("folder") or "").replace("\\", "/").strip("/")
    from vg.segments import apply_hls_display_name

    old_name = (item.get("name") or "").strip()
    try:
        if apply_hls_display_name(item):
            if rename_acc is not None:
                rename_acc.append(f"{old_name}→{item.get('name')}")
    except Exception as exc:
        log(f"[HLS标题] 改写失败 id={item.get('id') or '-'} rel={item.get('rel') or '-'}: {exc}")
    return item


def _disk_item(
    item: dict,
    root_s: str,
    cache: Path,
    *,
    rename_acc: list[str] | None = None,
) -> dict:
    """Return a canonical per-disk record from a possibly merged runtime item."""
    out = serialize_video_item(item, root=root_s, cache=cache)
    # Per-disk RAM archives are not persisted JSON responses. Keep the exact
    # search cache restored from SQLite so unified startup does not immediately
    # recompute pinyin/actor text when it merges the disk libraries.
    if isinstance(item.get("_q"), str):
        out["_q"] = item["_q"]
    # Persisted catalogs may still title HLS rows as "index"; fix on load.
    from vg.segments import apply_hls_display_name

    old_name = (out.get("name") or "").strip()
    try:
        if apply_hls_display_name(out):
            if rename_acc is not None:
                rename_acc.append(f"{old_name}→{out.get('name')}")
    except Exception as exc:
        log(f"[HLS标题] 改写失败 id={out.get('id') or '-'} rel={out.get('rel') or '-'}: {exc}")
    return out


def _log_hls_title_rewrites(root_s: str, samples: list[str]) -> None:
    if not samples:
        return
    preview = "；".join(samples[:3])
    log(
        f"[HLS标题] 根={root_s} 改写 {len(samples)} 条 index/playlist→父目录"
        + (f"（例: {preview}）" if preview else "")
    )


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
    with _libs_guard("disk_libs_read_live"):
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

    cache_s = str(cache)
    index_mtime = catalog_mtime(cache)
    if _catalog_empty_cooled(cache_s, index_mtime):
        return None

    with _libs_guard("disk_libs_read_cached"):
        existing = (STATE.get("disk_libs") or {}).get(root_s)
        if (
            existing
            and existing.get("by_id")
            and not existing.get("live")
            and float(existing.get("index_mtime") or 0) == float(index_mtime or 0)
        ):
            return list(existing["by_id"].values())

    leader, flight, flight_key = _begin_catalog_flight(root_s)
    if not leader:
        _wait_catalog_flight(root_s, flight, caller="read_root_library")
        return _disk_lib_snapshot(root_s)
    try:
        cached = _disk_lib_snapshot(root_s)
        if cached:
            return cached
        videos = load_catalog_videos(cache, root_s, restore_search_cache=True)
        if not videos:
            _mark_catalog_empty(cache_s, index_mtime or 0)
            return None
        renames: list[str] = []
        clean = [
            _adopt_catalog_item(v, root_s, cache, rename_acc=renames)
            for v in videos
            if isinstance(v, dict) and v.get("id") and item_belongs_to_root(v, root_s)
        ]
        by_id = {v["id"]: v for v in clean}
        if not by_id:
            _mark_catalog_empty(cache_s, index_mtime or 0)
            return None
        _clear_catalog_empty(cache_s)
        _log_hls_title_rewrites(root_s, renames)
        _store_lib(root_s, cache, by_id, index_mtime=index_mtime)
        return list(by_id.values())
    finally:
        _end_catalog_flight(flight_key, flight)


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
        from vg.segments import apply_hls_display_name

        try:
            apply_hls_display_name(stamped)
        except Exception as exc:
            log(f"[HLS标题] live改写失败 id={stamped.get('id') or '-'}: {exc}")
        source_id = stamped.get("_thumb_id") or stamped["id"]
        by_id[source_id] = stamped
    with _libs_guard("disk_libs_store_live"):
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
    # Persist SQLite outside the global disk_libs lock so mid-scan
    # store_live / HTTP readers are not blocked for seconds.
    if not save_index(cache, Path(root_s), list(by_id.values())):
        raise OSError(f"保存片库索引失败: {cache}")
    with _libs_guard("disk_libs_save_index"):
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
    # SQLite I/O must not hold the global disk_libs lock — mid-scan
    # ``store_live_library`` waited 1–3s on this (bench lock_waiting).
    n = upsert_catalog_videos(
        cache,
        root_s,
        [item],
        allow_insert=allow_insert,
    )
    if n <= 0:
        return False
    with _libs_guard("disk_libs_upsert_one"):
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
        # Keep SQLite off the global disk_libs lock (see save_library_item).
        n = upsert_catalog_videos(
            cache,
            root_s,
            batch,
            allow_insert=allow_insert,
        )
        if n <= 0:
            continue
        saved += n
        with _libs_guard("disk_libs_upsert_batch"):
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
    if saved:
        try:
            from vg.diagnostics import emit_rate_limited

            emit_rate_limited(
                "INFO",
                "catalog_upsert_batch",
                key=f"upsert|{saved}|{int(bump_gen)}|{int(allow_insert)}",
                interval=5.0,
                force=True,
                saved=saved,
                roots=len(groups),
                allow_insert=bool(allow_insert),
                bump_gen=bool(bump_gen),
                thread=threading.current_thread().name,
            )
        except Exception:
            pass
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

    with _libs_guard("disk_libs_merge_groups"):
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
    if cache and by_id:
        _clear_catalog_empty(str(cache))
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

    # Hot path: by-ids / thumb already have this disk in RAM. Do not archive
    # thousands of rows or reopen SQLite on every request.
    if memory_catalog_ready(root):
        return True
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
    if memory_catalog_ready(root_s):
        return True
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
    cache_s = str(cache)
    # A sibling mount often probes this root before its first scan writes
    # catalog.sqlite. If a later scan created the file, ignore the cooldown.
    if catalog_exists(cache):
        _catalog_missing_until.pop(cache_s, None)
    else:
        missing_until = _catalog_missing_until.get(cache_s)
        if missing_until and missing_until > time.time():
            return False
        _catalog_missing_until[cache_s] = time.time() + _CATALOG_MISSING_COOLDOWN_S
        from vg.catalog_db import catalog_db_path
        from vg.diagnostics import emit_rate_limited

        catalog_path = catalog_db_path(cache)
        catalog_is_file = False
        catalog_size = -1
        catalog_mtime_s = 0.0
        catalog_stat_error = ""
        if catalog_path is not None:
            try:
                st = catalog_path.stat()
                catalog_is_file = catalog_path.is_file()
                catalog_size = int(st.st_size)
                catalog_mtime_s = float(st.st_mtime)
            except FileNotFoundError:
                catalog_is_file = False
                catalog_size = -1
                catalog_mtime_s = 0.0
            except OSError as exc:
                catalog_stat_error = f"{type(exc).__name__}:{exc}"
        emit_rate_limited(
            "WARN",
            "disk_library_load_skipped",
            key=f"catalog_missing|{root_s}",
            interval=30.0,
            force=True,
            reason="catalog_missing",
            root=root_s,
            cache=cache,
            catalog_path=str(catalog_path) if catalog_path else "",
            catalog_is_file=catalog_is_file,
            catalog_size_bytes=catalog_size,
            catalog_mtime=catalog_mtime_s,
            catalog_stat_error=catalog_stat_error,
        )
        return False
    _catalog_missing_until.pop(cache_s, None)
    index_mtime = catalog_mtime(cache)
    if _catalog_empty_cooled(cache_s, index_mtime):
        return False
    with _libs_guard("disk_lib_load_state"):
        existing = (STATE.get("disk_libs") or {}).get(root_s)
        if (
            existing
            and existing.get("by_id")
            and float(existing.get("index_mtime") or 0) == index_mtime
        ):
            return True
        # Metadata enrichment UPSERTs bump catalog mtime every few seconds.
        # Reloading the whole SQLite catalog for every /api/videos-by-ids while
        # that writer holds the same lock freezes waitress threads (and the UI
        # looks like tag clicks do nothing). Keep the in-memory copy.
        if (
            existing
            and existing.get("by_id")
            and (
                STATE.get("meta_progress")
                or STATE.get("scanning")
                or STATE.get("updating")
            )
        ):
            try:
                from vg.diagnostics import emit_rate_limited

                emit_rate_limited(
                    "WARN",
                    "disk_lib_reload_skipped_writer_busy",
                    key=f"reload-skip|{root_s}",
                    interval=5.0,
                    force=True,
                    root=root_s,
                    cached_rows=len(existing.get("by_id") or {}),
                    cached_mtime=float(existing.get("index_mtime") or 0),
                    disk_mtime=float(index_mtime or 0),
                    scanning=bool(STATE.get("scanning")),
                    updating=bool(STATE.get("updating")),
                    meta_progress=str(STATE.get("meta_progress") or "")[:120],
                    reason="avoid_catalog_lock_deadlock",
                )
            except Exception:
                pass
            return True
        # Scan flushes catalog.sqlite often; reloading 778+ rows for every
        # /thumb or history lookup stalls the UI. Keep RAM copy briefly.
        if existing and existing.get("by_id"):
            loaded_at = float(existing.get("updated") or 0)
            if loaded_at and (time.time() - loaded_at) < 3.0:
                return True
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
    leader, flight, flight_key = _begin_catalog_flight(root_s)
    if not leader:
        _wait_catalog_flight(root_s, flight, caller="load_library_from_index")
        return bool(_disk_lib_snapshot(root_s))
    try:
        if _disk_lib_snapshot(root_s):
            return True
        stage_started = time.perf_counter()
        videos = load_catalog_videos(cache, root_s, restore_search_cache=True)
        sql_ms = (time.perf_counter() - stage_started) * 1000.0
        clean = []
        renames: list[str] = []
        stage_started = time.perf_counter()
        for raw in videos:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            if not item_belongs_to_root(raw, root_s):
                continue
            clean.append(_adopt_catalog_item(raw, root_s, cache, rename_acc=renames))
        adopt_ms = (time.perf_counter() - stage_started) * 1000.0
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
            _mark_catalog_empty(cache_s, index_mtime or 0)
            with _libs_guard("disk_lib_empty_cleanup"):
                lib = (STATE.get("disk_libs") or {}).get(root_s)
                if lib is not None:
                    lib.pop("loading", None)
                    if not lib.get("by_id"):
                        (STATE.get("disk_libs") or {}).pop(root_s, None)
            return False
        _clear_catalog_empty(cache_s)
        final_mtime = catalog_mtime(cache)
        stage_started = time.perf_counter()
        _store_lib(root_s, cache, by_id, index_mtime=final_mtime or index_mtime)
        store_ms = (time.perf_counter() - stage_started) * 1000.0
        stage_started = time.perf_counter()
        _log_hls_title_rewrites(root_s, renames)
        now = time.time()
        last = float(_load_log_ts.get(root_s) or 0)
        if now - last >= 5.0:
            _load_log_ts[root_s] = now
            log(f"[跨盘] 已加载历史盘索引: {root_s}（{len(by_id)} 部）")
        log_ms = (time.perf_counter() - stage_started) * 1000.0
        try:
            from vg.diagnostics import emit

            emit(
                "PERF",
                "disk_library_hydrate_breakdown",
                force=True,
                root=root_s,
                rows=len(by_id),
                sql_wall_ms=f"{sql_ms:.1f}",
                adopt_ms=f"{adopt_ms:.1f}",
                store_ms=f"{store_ms:.1f}",
                log_ms=f"{log_ms:.1f}",
            )
        except Exception:
            pass
        return True
    finally:
        _end_catalog_flight(flight_key, flight)
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
    catalog_writer_busy = bool(
        STATE.get("scanning")
        or STATE.get("updating")
        or STATE.get("meta_progress")
    )
    if prefer and catalog_writer_busy:
        try:
            from vg.diagnostics import emit_rate_limited

            emit_rate_limited(
                "WARN",
                "disk_libs_ensure_skipped_writer_busy",
                key=f"ensure-skip|{prefer}",
                interval=5.0,
                force=True,
                root=prefer,
                video_id=str(vid)[:24],
                scanning=bool(STATE.get("scanning")),
                updating=bool(STATE.get("updating")),
                meta_progress=str(STATE.get("meta_progress") or "")[:120],
                reason="avoid_catalog_lock_deadlock",
            )
        except Exception:
            pass
    elif prefer:
        if not memory_catalog_ready(prefer):
            ensure_library(prefer)
    with _libs_guard("disk_libs_lookup_by_id"):
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
