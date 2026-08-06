# -*- coding: utf-8 -*-
"""Keep recently opened disk indexes so history/stream still work after switching drives."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from vg.cache import ensure_cache_dir, save_index
from vg.config import INDEX_NAME, VGDATA_DIR
from vg.schema import RUNTIME_ONLY_FIELDS, serialize_video_item
from vg.state import STATE
from vg.util import log

_MAX_DISK_LIBS = 12
_libs_lock = threading.RLock()
_scanned_caches = False

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
    """Read one per-disk catalog; prefer in-memory cache to avoid re-parsing JSON."""
    root_s = _norm_root_str(root)
    cache = ensure_cache_dir(Path(root_s))
    index_path = cache / INDEX_NAME

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

    if not index_path.is_file():
        return None

    try:
        index_mtime = index_path.stat().st_mtime
    except OSError:
        index_mtime = 0

    with _libs_lock:
        existing = (STATE.get("disk_libs") or {}).get(root_s)
        if (
            existing
            and existing.get("by_id")
            and not existing.get("live")
            and float(existing.get("index_mtime") or 0) == float(index_mtime or 0)
        ):
            return list(existing["by_id"].values())

    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log(f"[索引] 读取失败 {index_path}: {e}")
        return None
    videos = payload.get("videos")
    if not isinstance(videos, list):
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
    """Publish in-progress scan results into disk_libs without writing index.json.

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
    """Refresh in-memory disk_libs after index.json was written (clears live flag)."""
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
        try:
            index_mtime = (Path(cache) / INDEX_NAME).stat().st_mtime
        except OSError:
            index_mtime = 0.0
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
            raise OSError(f"保存片库索引失败: {cache / INDEX_NAME}")
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


def save_library_item(item: dict, *, allow_insert: bool = False) -> bool:
    """Persist one changed item without touching other disks.

    Existing records are updated by source id or relative path. Insertion is
    opt-in so a late metadata/thumbnail worker cannot resurrect an item that
    was deleted while that worker was running.
    """
    raw_root = (item.get("_lib_root") or item.get("root") or "").strip()
    if not raw_root or not item.get("id"):
        return False
    root_s = _norm_root_str(raw_root)
    cache = ensure_cache_dir(Path(root_s))
    with _libs_lock:
        current: list[dict] = []
        index_path = cache / INDEX_NAME
        try:
            if index_path.is_file():
                payload = json.loads(index_path.read_text(encoding="utf-8"))
                if isinstance(payload.get("videos"), list):
                    current = [v for v in payload["videos"] if isinstance(v, dict)]
        except (OSError, json.JSONDecodeError) as e:
            log(f"[索引] 读取旧索引失败，未覆盖 {root_s}: {e}")
            return False

        source_id = (item.get("_thumb_id") or item.get("id") or "").strip()
        rel = (item.get("rel") or "").replace("\\", "/").strip("/").casefold()
        replaced = False
        for i, old in enumerate(current):
            old_rel = (old.get("rel") or "").replace("\\", "/").strip("/").casefold()
            if old.get("id") == source_id or (rel and old_rel == rel):
                current[i] = item
                replaced = True
                break
        if not replaced and allow_insert:
            current.append(item)
            replaced = True
        if not replaced:
            return False
        save_root_library(root_s, current)
    return True


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
    stamp_lib_meta(list(by_id.values()), root=root_s, cache=cache)
    if index_mtime is None and cache:
        try:
            index_mtime = (Path(cache) / INDEX_NAME).stat().st_mtime
        except OSError:
            index_mtime = 0
    with _libs_lock:
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
    """Load a disk's saved index into disk_libs without switching the active UI root."""
    try:
        root_p = Path(root).expanduser().resolve()
    except OSError:
        return False
    if not root_p.is_dir():
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
    index_path = cache / INDEX_NAME
    if not index_path.is_file():
        return False
    try:
        index_mtime = index_path.stat().st_mtime
    except OSError:
        index_mtime = 0
    with _libs_lock:
        existing = (STATE.get("disk_libs") or {}).get(root_s)
        if (
            existing
            and existing.get("by_id")
            and float(existing.get("index_mtime") or 0) == index_mtime
        ):
            return True
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log(f"[跨盘] 读索引失败 {index_path}: {e}")
        return False
    videos = data.get("videos")
    if not isinstance(videos, list):
        return False
    # Normalize legacy records on every load; actual mounted path is canonical.
    clean = []
    for raw in videos:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        if not item_belongs_to_root(raw, root_s):
            log(f"[跨盘] 忽略索引中的外盘条目: {raw.get('rel') or raw.get('id')}")
            continue
        clean.append(_disk_item(raw, root_s, cache))
    by_id = {v["id"]: v for v in clean}
    if not by_id:
        return False
    try:
        final_mtime = index_path.stat().st_mtime
    except OSError:
        final_mtime = index_mtime
    if final_mtime != index_mtime:
        # Atomic writer replaced the file while it was being read. Do not tag
        # the old content with the new mtime; the next lookup will reload it.
        return False
    _store_lib(root_s, cache, by_id, index_mtime=final_mtime)
    log(f"[跨盘] 已加载历史盘索引: {root_s}（{len(by_id)} 部）")
    return True


def ensure_cached_indexes_scanned() -> None:
    """One-time: pull indexes from program preview_cache and (if enabled) disk caches."""
    global _scanned_caches
    if _scanned_caches:
        return
    with _libs_lock:
        if _scanned_caches:
            return
        _scanned_caches = True
    try:
        from vg.config import THUMB_DIR_NAME
        from vg.privacy import cache_location

        seen_roots: set[str] = set()
        if VGDATA_DIR.is_dir():
            for index_path in VGDATA_DIR.glob(f"*/{INDEX_NAME}"):
                try:
                    data = json.loads(index_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                root_raw = (data.get("root") or "").strip()
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

        # 盘内缓存模式：挂载列表里的盘也可能只有 .video_gallery_cache
        if cache_location() == "disk":
            for raw in list(STATE.get("mounted_roots") or []):
                try:
                    root_p = Path(raw).expanduser().resolve()
                    if not root_p.is_dir():
                        continue
                    if not (root_p / THUMB_DIR_NAME / INDEX_NAME).is_file():
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
    Program-cache indexes contain their source root, so a previously scanned
    disk can be mounted again without rescanning it first.
    """
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
            for index_path in VGDATA_DIR.glob(f"*/{INDEX_NAME}"):
                try:
                    payload = json.loads(index_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(payload, dict):
                    add(payload.get("root"))
    except OSError as e:
        log(f"[多盘] 发现缓存片库失败: {e}")

    # In disk-cache mode the program cache may not contain the index. Checking
    # drive roots is cheap and avoids recursively scanning whole disks.
    try:
        from vg.config import THUMB_DIR_NAME
        from vg.drives import list_ready_drives

        for drive in list_ready_drives():
            index_path = drive / THUMB_DIR_NAME / INDEX_NAME
            if not index_path.is_file():
                continue
            try:
                payload = json.loads(index_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            add(payload.get("root") if isinstance(payload, dict) else drive)
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
