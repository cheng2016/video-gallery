# -*- coding: utf-8 -*-
"""Keep recently opened disk indexes so history/stream still work after switching drives."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from vg.cache import ensure_cache_dir
from vg.config import INDEX_NAME, VGDATA_DIR
from vg.state import STATE
from vg.util import log

_MAX_DISK_LIBS = 12
_libs_lock = threading.RLock()
_scanned_caches = False


def _root_key(root: Path | str) -> str:
    try:
        return str(Path(root).expanduser().resolve())
    except OSError:
        return str(Path(root)).replace("/", "\\").rstrip("\\").lower()


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
            root_s = fallback_root
        if not root_s:
            continue
        groups.setdefault(root_s, {})[v["id"]] = v
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


def _store_lib(root_s: str, cache: Path | None, by_id: dict[str, dict]) -> None:
    stamp_lib_meta(list(by_id.values()), root=root_s, cache=cache)
    with _libs_lock:
        libs = STATE.setdefault("disk_libs", {})
        libs[root_s] = {
            "root": root_s,
            "cache_dir": str(cache) if cache else None,
            "by_id": by_id,
            "updated": time.time(),
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
    with _libs_lock:
        existing = (STATE.get("disk_libs") or {}).get(root_s)
        if existing and existing.get("by_id"):
            return True
    # already the active root
    cur = STATE.get("root")
    if cur and _norm_root_str(cur) == root_s and (STATE.get("by_id") or STATE.get("videos")):
        archive_current_library()
        return True

    cache = ensure_cache_dir(root_p)
    index_path = cache / INDEX_NAME
    if not index_path.is_file():
        return False
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log(f"[跨盘] 读索引失败 {index_path}: {e}")
        return False
    videos = data.get("videos")
    if not isinstance(videos, list):
        return False
    # root in index may differ slightly; prefer actual path
    by_id = {v["id"]: v for v in videos if isinstance(v, dict) and v.get("id")}
    if not by_id:
        return False
    _store_lib(root_s, cache, by_id)
    log(f"[跨盘] 已加载历史盘索引: {root_s}（{len(by_id)} 部）")
    return True


def ensure_cached_indexes_scanned() -> None:
    """One-time: pull all preview_cache/*/index.json whose root folder still exists."""
    global _scanned_caches
    if _scanned_caches:
        return
    with _libs_lock:
        if _scanned_caches:
            return
        _scanned_caches = True
    try:
        if not VGDATA_DIR.is_dir():
            return
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
            load_library_from_index(root_p)
    except OSError as e:
        log(f"[跨盘] 扫描缓存索引失败: {e}")


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
        for key, lib in libs.items():
            if prefer and key == prefer:
                continue
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
    root = (item.get("_lib_root") or "").strip()
    if root:
        try:
            rp = Path(root)
            if rp.is_dir():
                return ensure_cache_dir(rp)
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
            p = Path(raw)
            return p if p.is_dir() else None
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
