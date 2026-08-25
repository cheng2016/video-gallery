# -*- coding: utf-8 -*-
"""Multi-root library: mount several folders into one catalog."""
from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path

from collections import OrderedDict

from vg.cache import ensure_cache_dir
from vg.catalog import build_category_facets, build_tree, rebuild_indexes, video_category
from vg.disk_libs import (
    archive_current_library,
    ensure_library,
    load_library_from_index,
    read_root_library,
    stamp_lib_meta,
    _norm_root_str,
)
from vg.drives import save_prefs
from vg.state import STATE
from vg.util import log

_roots_lock = threading.RLock()

# Cache the expensive multi-root merge result so repeated API calls during a
# scan don't rebuild the same merged list every time.  Keyed by (lib_gen, …)
# which auto-invalidates whenever _publish_live() bumps the generation.
_scope_merge_cache: OrderedDict[tuple, list[dict]] = OrderedDict()
_scope_merge_cache_lock = threading.Lock()
_SCOPE_MERGE_CACHE_MAX = 4
_SCOPE_MERGE_CACHE_TTL = 2.0  # seconds – reuse even when lib_gen changes
_scope_merge_last_time: float = 0.0
_scope_merge_last_result: list[dict] = []


def _root_compare_key(root: Path | str | None) -> str:
    """Cheap comparison key for roots already stored as absolute paths.

    Do not call ``Path.resolve()`` for every catalog row on a foreground
    request: under ffmpeg/ffprobe disk load that turns an O(n) memory filter
    into seconds of filesystem I/O.
    """
    raw = str(root or "").strip()
    if not raw:
        return ""
    return os.path.normcase(os.path.normpath(raw))


def root_label(root: Path | str) -> str:
    """Short human label, e.g. D:电影 / E:动漫."""
    try:
        p = Path(root).resolve()
    except OSError:
        p = Path(str(root))
    drive = (p.drive or "").rstrip(":\\/")
    name = p.name or ""
    if drive and name and name.upper() != drive.upper() and name not in (f"{drive}:", f"{drive}:\\"):
        return f"{drive}:{name}"
    if drive:
        return f"{drive}:"
    return name or "disk"


def get_mounted_roots() -> list[str]:
    with _roots_lock:
        raw = list(STATE.get("mounted_roots") or [])
    out: list[str] = []
    seen: set[str] = set()
    for r in raw:
        try:
            key = _norm_root_str(r)
        except Exception:
            key = str(r).strip()
        if not key or key.lower() in seen:
            continue
        seen.add(key.lower())
        out.append(key)
    return out


def set_mounted_roots(
    roots: list[str],
    primary: str | None = None,
    *,
    drop_offline: bool = True,
) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for r in roots:
        try:
            p = Path(r).expanduser().resolve()
            if drop_offline and not p.is_dir():
                continue
            key = str(p)
        except OSError:
            if drop_offline:
                continue
            key = str(r).strip()
            if not key:
                continue
        low = key.lower()
        if low in seen:
            continue
        seen.add(low)
        cleaned.append(key)
    with _roots_lock:
        STATE["mounted_roots"] = cleaned
        if primary:
            try:
                pk = _norm_root_str(primary)
                if any(m.lower() == pk.lower() for m in cleaned):
                    pk = next(m for m in cleaned if m.lower() == pk.lower())
                    STATE["root"] = Path(pk)
                    try:
                        STATE["cache_dir"] = ensure_cache_dir(Path(pk))
                    except OSError:
                        pass
            except OSError:
                pass
        elif cleaned:
            try:
                STATE["root"] = Path(cleaned[0])
                STATE["cache_dir"] = ensure_cache_dir(Path(cleaned[0]))
            except OSError:
                pass
    save_prefs(mounted_roots=cleaned, last_root=str(STATE.get("root") or "") or None)
    return cleaned


def activate_mount(path: str | Path) -> list[str]:
    """Add/activate one root without dropping other mounts (even if offline).

    Rescan/join-library must never shrink the mounted set just because another
    disk briefly fails ``is_dir()`` — that made other disks vanish from the UI.
    """
    try:
        root = Path(path).expanduser().resolve()
    except OSError as e:
        raise ValueError(f"路径无效: {e}") from e
    if not root.is_dir():
        raise ValueError(f"目录不存在: {root}")
    root_s = str(root)
    current = get_mounted_roots()
    if root_s.lower() not in {m.lower() for m in current}:
        current = list(current) + [root_s]
    return set_mounted_roots(current, primary=root_s, drop_offline=False)


def _restore_folder(item: dict, root_s: str, label: str) -> str:
    """Keep original folder; undo old label-prefix merge if present."""
    raw = item.get("_folder_raw")
    if isinstance(raw, str):
        return raw.replace("\\", "/").strip("/")
    folder = (item.get("folder") or "").replace("\\", "/").strip("/")
    # legacy: "D:电影/动作" or "D-电影/动作"
    for prefix in (label + "/", label.replace(":", "-") + "/", label.replace(":", "") + "/"):
        if folder.startswith(prefix):
            return folder[len(prefix) :]
    if folder in (label, label.replace(":", "-"), label.replace(":", "")):
        return ""
    return folder


def _videos_from_root(root_s: str) -> list[dict]:
    """Videos belonging to one root; live scan progress beats stale index."""
    root_key = _root_compare_key(root_s)
    if not root_key:
        return []

    # Scanning this disk: prefer live in-memory snapshot so the third disk
    # appears while the walk is still running.
    try:
        scan_root = STATE.get("scan_root") or ""
        if scan_root and _root_compare_key(scan_root) == root_key:
            live = STATE.get("scan_live")
            if isinstance(live, list) and live:
                return live
    except Exception:
        pass

    # Memory / disk index (read_root_library is now mtime-cached)
    from_disk = read_root_library(root_s)
    if from_disk is not None:
        return from_disk

    vids = list(STATE.get("videos") or [])
    scoped = [
        dict(v) for v in vids
        if _root_compare_key(v.get("_lib_root") or v.get("root")) == root_key
    ]
    if scoped:
        return scoped

    try:
        cur = STATE.get("root")
        if cur and _root_compare_key(cur) == root_key and vids:
            foreign = [
                v for v in vids
                if v.get("_lib_root")
                and _root_compare_key(v.get("_lib_root")) != root_key
            ]
            if not foreign:
                out = [dict(v) for v in vids]
                stamp_lib_meta(out, root=root_s, cache=STATE.get("cache_dir"), overwrite=True)
                return out
    except OSError:
        pass

    if load_library_from_index(root_s):
        lib = (STATE.get("disk_libs") or {}).get(root_s)
        if lib and lib.get("by_id"):
            return [dict(v) for v in lib["by_id"].values()]
    return []


def _dedupe_id(item: dict, root_s: str, used: dict[str, str]) -> dict:
    vid = item.get("id") or ""
    if not vid:
        rel = item.get("rel") or item.get("name") or "?"
        vid = hashlib.md5(f"{root_s}\0{rel}".encode("utf-8")).hexdigest()[:16]
        item["id"] = vid
    if vid in used and used[vid] != root_s:
        old = vid
        rel = item.get("rel") or ""
        vid = hashlib.md5(f"{root_s}\0{rel}\0{old}".encode("utf-8")).hexdigest()[:16]
        item["_thumb_id"] = item.get("_thumb_id") or old
        item["id"] = vid
    used[item["id"]] = root_s
    return item


def filter_videos_by_lib(videos: list[dict] | None, lib: str | None) -> list[dict]:
    """Filter by mounted root path; empty lib = all."""
    videos = list(videos or [])
    lib = (lib or "").strip()
    if not lib:
        return videos
    key_l = _root_compare_key(lib)
    out = []
    for v in videos:
        r = (v.get("_lib_root") or v.get("root") or "").strip()
        if not r:
            continue
        if _root_compare_key(r) == key_l:
            out.append(v)
    return out


def _snapshot_covers_archives(videos: list[dict], lib: str = "") -> bool:
    """Return whether the published snapshot is at least as complete as RAM archives.

    A stale ``meta_progress`` string can survive a test/restart while STATE is
    intentionally partial.  Only trust the snapshot for metadata writes when
    every known per-disk archive row is represented; otherwise the catalog
    remains the source of truth.
    """
    libs = STATE.get("disk_libs") or {}
    if not libs:
        return False
    roots = [lib] if lib else get_mounted_roots()
    if not roots:
        return False
    snapshot_counts: dict[str, int] = {}
    for video in videos:
        owner = video.get("_lib_root") or video.get("root") or ""
        owner_key = _root_compare_key(owner)
        if owner_key:
            snapshot_counts[owner_key] = snapshot_counts.get(owner_key, 0) + 1
    archives_by_root = {
        _root_compare_key(candidate): value or {}
        for candidate, value in libs.items()
        if _root_compare_key(candidate)
    }
    for root_s in roots:
        key = _root_compare_key(root_s)
        archive = archives_by_root.get(key) or {}
        expected = len((archive or {}).get("by_id") or {})
        if expected and snapshot_counts.get(key, 0) < expected:
            return False
    return True


def videos_for_scope(lib: str | None = None) -> list[dict]:
    """
    API 用片源：
    - 选中某一盘：该盘完整目录（STATE / index / disk_libs）
    - 未选盘 + 多盘：按盘合并，数量与侧栏「全部视频」合计一致
      （不能只用可能漏盘的 STATE.videos）
    - 未选盘 + 单盘：STATE.videos
    """
    global _scope_merge_last_time, _scope_merge_last_result
    lib = (lib or "").strip()
    state_vids = list(STATE.get("videos") or [])

    # During a scan/update the disk catalog may be temporarily unavailable or
    # locked by the writer.  Foreground tag clicks should use the published
    # in-memory snapshot instead of reopening every mounted catalog; the next
    # generation invalidation will refresh it after the scan completes.
    # Metadata enrichment writes SQLite in small batches.  Its mtime changes
    # must not invalidate the runtime snapshot for every foreground request;
    # the enriched dicts are updated in-place and a final index rebuild bumps
    # the generation when the worker completes.
    snapshot_busy = bool(
        STATE.get("scanning")
        or STATE.get("updating")
        or (
            STATE.get("meta_progress")
            and _snapshot_covers_archives(state_vids, lib)
        )
    )
    if state_vids and snapshot_busy:
        if lib:
            try:
                key = _norm_root_str(lib)
            except Exception:
                key = lib
            scoped = filter_videos_by_lib(state_vids, key)
            if scoped:
                return scoped
        else:
            return state_vids

    if lib:
        try:
            key = _norm_root_str(lib)
        except Exception:
            key = lib
        disk = _videos_from_root(key)
        if disk:
            return disk
        return filter_videos_by_lib(STATE.get("videos") or [], key)

    roots = get_mounted_roots()
    if len(roots) <= 1:
        return state_vids

    per_lists: list[tuple[str, list[dict]]] = [(r, _videos_from_root(r)) for r in roots]
    expected = sum(len(items) for _, items in per_lists)

    # STATE 已覆盖各盘则直接用（快）
    # Single-pass bucketization of state_vids by root key replaces the prior
    # per-root filter_videos_by_lib calls (O(roots × N) → O(N)).
    if state_vids and expected > 0:
        state_counts: dict[str, int] = {}
        for v in state_vids:
            k = _root_compare_key(v.get("_lib_root") or v.get("root"))
            if not k:
                continue
            state_counts[k] = state_counts.get(k, 0) + 1
        covered = True
        for root_s, subset in per_lists:
            if not subset:
                continue
            key = _root_compare_key(root_s)
            if state_counts.get(key, 0) < len(subset):
                covered = False
                break
        if covered and len(state_vids) >= expected:
            return state_vids

    # 按盘合并，与侧栏「全部视频」合计一致（不在此写回 STATE，避免与扫描抢写）
    # Cache the expensive merge so repeated API calls during a scan reuse the
    # same result until lib_gen changes (bumped by _publish_live).
    cache_key = (
        int(STATE.get("lib_gen") or 0),
        tuple(len(items) for _, items in per_lists),
    )
    with _scope_merge_cache_lock:
        cached = _scope_merge_cache.get(cache_key)
        if cached is not None:
            _scope_merge_cache.move_to_end(cache_key)
            return cached
        # TTL fallback: lib_gen changed but the previous merge is still fresh.
        # Avoids rebuilding the same merged list every ~1 s during a scan.
        now_m = time.monotonic()
        if _scope_merge_last_result and (now_m - _scope_merge_last_time) < _SCOPE_MERGE_CACHE_TTL:
            return _scope_merge_last_result
    used_ids: dict[str, str] = {}
    merged: list[dict] = []
    for root_s, subset in per_lists:
        cache = None
        try:
            cache = ensure_cache_dir(Path(root_s))
        except OSError:
            pass
        label = root_label(root_s)
        for raw in subset:
            item = dict(raw)
            if not (item.get("_lib_root") or "").strip():
                stamp_lib_meta([item], root=root_s, cache=cache, overwrite=True)
            else:
                stamp_lib_meta([item], root=root_s, cache=cache, overwrite=False)
            folder = _restore_folder(item, root_s, label)
            item["_folder_raw"] = item.get("_folder_raw") or folder
            item["folder"] = folder
            item["_lib_label"] = item.get("_lib_label") or label
            item["lib_label"] = item.get("lib_label") or label
            item["root"] = item.get("root") or root_s
            _dedupe_id(item, root_s, used_ids)
            merged.append(item)
    result = merged or state_vids
    if merged:
        with _scope_merge_cache_lock:
            _scope_merge_cache[cache_key] = result
            _scope_merge_cache.move_to_end(cache_key)
            while len(_scope_merge_cache) > _SCOPE_MERGE_CACHE_MAX:
                _scope_merge_cache.popitem(last=False)
            _scope_merge_last_time = time.monotonic()
            _scope_merge_last_result = result
    return result


def roots_summary(videos: list[dict] | None = None) -> list[dict]:
    """Each mount with count + per-disk channel categories.

    每盘频道独立统计，不依赖「合并后可能被冲坏」的 STATE.videos 归属。
    """
    started = time.perf_counter()
    roots = get_mounted_roots()
    # During scan/update, ``videos`` is the already published unified
    # snapshot.  Reopening every per-disk SQLite catalog here contends with
    # the scanner and makes a harmless tree refresh take seconds.  Keep root
    # counts/facets consistent with the snapshot until the next publish.
    snapshot = list(videos or [])
    use_snapshot = bool(snapshot) and bool(
        STATE.get("scanning")
        or STATE.get("updating")
        or (
            STATE.get("meta_progress")
            and _snapshot_covers_archives(snapshot)
        )
    )

    # Pre-bucket any in-memory list by root key in a single O(N) pass,
    # replacing the previous O(roots × N) per-root filter() calls that
    # produced the 9.9s roots_summary_slow stalls on 4-disk libraries.
    def _bucketize(items: list[dict]) -> dict[str, list[dict]]:
        buckets: dict[str, list[dict]] = {}
        for v in items:
            k = _root_compare_key(v.get("_lib_root") or v.get("root"))
            if not k:
                continue
            lst = buckets.get(k)
            if lst is None:
                lst = []
                buckets[k] = lst
            lst.append(v)
        return buckets

    snap_buckets: dict[str, list[dict]] | None = _bucketize(snapshot) if use_snapshot or (videos is not None and not use_snapshot) else None
    # When not using snapshot, avoid filtering STATE videos for every root;
    # build the STATE fallback bucket map lazily only if we actually need it.
    state_buckets: dict[str, list[dict]] | None = None
    state_videos_raw = STATE.get("videos") if not use_snapshot else None
    if videos is not None and not use_snapshot:
        # The explicit videos arg acts as the STATE-layer fallback in this call.
        pass

    out = []
    root_timings: list[str] = []
    # 诊断：把每一盘"数据源选择 + 计数 + 分类"打到日志，避免前台看到 0
    # categories 但后端到底走了 snapshot bucket / disk SQLite / STATE 哪个
    # 分支无从反查。关键观察点：G 盘扫完 STATE.videos 里 1822 条
    # _lib_root 是否与 mounted G:\\ 归一化 key 一致。
    diag_snippets: list[str] = []
    for r in roots:
        root_started = time.perf_counter()
        r_key = _root_compare_key(r)
        # 优先按盘取片；调用方显式传了 videos 时优先用内存 bucketize，
        # 避免和 SQLite 写入竞争；之前的「先查 SQLite，SQLite 非空就不走
        # bucketize」顺序会导致 /api/roots 即使传了已发布的 2785 条
        # STATE["videos"] 仍退化为全表扫描，触发 roots_summary_slow。
        subset: list[dict]
        source_used = "unknown"
        snapshot_len = 0
        sql_len = 0
        state_len = 0
        if use_snapshot:
            subset = list(snap_buckets.get(r_key, [])) if snap_buckets is not None else []
            source_used = "snapshot"
            snapshot_len = len(subset)
        elif videos is not None:
            # Explicit snapshot passed by the caller: bucketize once and
            # consult per-disk SQLite only if this root is absent from the
            # snapshot (handles freshly-mounted / not-yet-published roots).
            if snap_buckets is None:
                snap_buckets = _bucketize(list(videos or []))
            subset_cand = list(snap_buckets.get(r_key, []))
            snapshot_len = len(subset_cand)
            if subset_cand:
                subset = subset_cand
                source_used = "snapshot_explicit"
            else:
                sql_cand = _videos_from_root(r)
                sql_len = len(sql_cand)
                subset = sql_cand
                source_used = "sql_fallback(explicit_snap_empty)"
        else:
            # No snapshot at all: trust the per-disk catalog as source of
            # truth, with STATE videos as a fallback.
            sql_cand = _videos_from_root(r)
            sql_len = len(sql_cand)
            if sql_cand:
                subset = sql_cand
                source_used = "sql(no_snap)"
            elif state_videos_raw:
                if state_buckets is None:
                    state_buckets = _bucketize(list(state_videos_raw or []))
                subset = list(state_buckets.get(r_key, []))
                source_used = "state_bucket(no_snap)"
                state_len = len(subset)
            else:
                subset = []
                source_used = "empty(no_snap_no_state)"
        cat_counts: dict[str, int] = {}
        for v in subset:
            cat = video_category(v)
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        # 每个盘采样 2 条 _lib_root，用来核对"归一化 key 对不上"的情况。
        samples = []
        for v in subset[:2]:
            samples.append(
                f"{v.get('_lib_root')!r}/{v.get('root')!r}"
            )
        diag_snippets.append(
            f"{r}[{r_key}]:src={source_used} "
            f"snap={snapshot_len} sql={sql_len} state={state_len} "
            f"count={len(subset)} cats={sorted(cat_counts.items())[:5]} "
            f"samples=[{';'.join(samples)}]"
        )
        out.append({
            "path": r,
            "label": root_label(r),
            "count": len(subset),
            "categories": build_category_facets(cat_counts),
        })
        root_timings.append(
            f"{r}:{(time.perf_counter() - root_started) * 1000.0:.1f}ms/{len(subset)}"
        )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    # 不管快慢都打一条 roots_summary_detail，方便"某盘显示 0"的诊断。
    try:
        from vg.diagnostics import info as _diag_info
        snap_lib_root_dist: dict[str, int] = {}
        for v in list(videos or [])[:300]:
            k = _root_compare_key(v.get("_lib_root") or v.get("root"))
            snap_lib_root_dist[k] = snap_lib_root_dist.get(k, 0) + 1
        state_lib_root_dist: dict[str, int] = {}
        for v in list(STATE.get("videos") or [])[:300]:
            k = _root_compare_key(v.get("_lib_root") or v.get("root"))
            state_lib_root_dist[k] = state_lib_root_dist.get(k, 0) + 1
        _diag_info(
            "roots_summary_detail",
            force=True,
            elapsed_ms=f"{elapsed_ms:.1f}",
            roots=len(roots),
            scanning=bool(STATE.get("scanning")),
            updating=bool(STATE.get("updating")),
            use_snapshot=use_snapshot,
            explicit_videos=videos is not None,
            videos_len=len(list(videos or [])),
            state_videos_len=len(list(STATE.get("videos") or [])),
            per_root=" | ".join(diag_snippets),
            per_root_times="|".join(root_timings),
            snap_lib_dist_sample=str(snap_lib_root_dist),
            state_lib_dist_sample=str(state_lib_root_dist),
        )
    except Exception as _ex:
        try:
            from vg.diagnostics import error as _diag_err
            _diag_err("roots_summary_detail_emit_failed", _ex)
        except Exception:
            pass
    if elapsed_ms >= 200.0:
        from vg.diagnostics import perf

        perf(
            "roots_summary_slow",
            elapsed_ms,
            force=True,
            roots=len(roots),
            scanning=bool(STATE.get("scanning")),
            updating=bool(STATE.get("updating")),
            use_snapshot=use_snapshot,
            explicit_videos=videos is not None,
            videos_len=len(list(videos or [])),
            state_videos_len=len(list(STATE.get("videos") or [])),
            per_root="|".join(root_timings),
        )
    return out


def _stamp_tree_lib(node: dict, lib: str) -> dict:
    node["lib"] = lib
    for c in node.get("children") or []:
        _stamp_tree_lib(c, lib)
    return node


def tree_for_scope(lib: str | None = None) -> dict:
    """
    Tree for UI:
    - single root / lib selected → normal folder tree for that root
    - multi + 全部 → 每个盘作为一级节点，下面是该盘真实文件夹

    片源必须与 videos_for_scope() 一致，否则侧栏树数量与右侧列表会对不上。
    """
    roots = get_mounted_roots()
    lib = (lib or "").strip()

    if lib:
        try:
            lib = _norm_root_str(lib)
        except Exception:
            pass
        scoped = videos_for_scope(lib)
        try:
            tree = build_tree(Path(lib), scoped)
        except OSError:
            tree = {"name": root_label(lib), "path": "", "count": len(scoped), "children": [], "videos": []}
        tree["name"] = root_label(lib)
        tree["path"] = ""
        tree["count"] = len(scoped)
        return _stamp_tree_lib(tree, lib)

    if len(roots) <= 1:
        scoped = videos_for_scope(None)
        if roots:
            try:
                tree = build_tree(Path(roots[0]), scoped)
                tree["count"] = len(scoped)
                return _stamp_tree_lib(tree, roots[0])
            except OSError:
                pass
        return {
            "name": "全部",
            "path": "",
            "count": len(scoped),
            "children": [],
            "videos": [],
        }

    # 多根「全部」：盘 → 该盘文件夹（可展开选择）
    children = []
    total = 0
    for r in roots:
        subset = videos_for_scope(r)
        n = len(subset)
        total += n
        try:
            sub = build_tree(Path(r), subset)
        except OSError:
            sub = {"name": root_label(r), "path": "", "count": n, "children": [], "videos": []}
        sub["count"] = n
        _stamp_tree_lib(sub, r)
        children.append({
            "name": root_label(r),
            "path": "",
            "lib": r,
            "count": n,
            "children": sub.get("children") or [],
            "videos": [],
        })
    return {
        "name": "全部盘",
        "path": "",
        "count": total,
        "children": children,
        "videos": [],
        "multi_overview": True,
    }


def publish_unified_library() -> int:
    """Merge all mounted roots into STATE videos/indexes. Folders stay original (no fake prefix)."""
    roots = get_mounted_roots()
    if not roots:
        return len(STATE.get("videos") or [])

    used_ids: dict[str, str] = {}
    merged: list[dict] = []

    for root_s in roots:
        ensure_library(root_s)
        cache = None
        try:
            cache = ensure_cache_dir(Path(root_s))
        except OSError:
            pass
        label = root_label(root_s)
        for raw in _videos_from_root(root_s):
            item = dict(raw)
            stamp_lib_meta([item], root=root_s, cache=cache)
            folder = _restore_folder(item, root_s, label)
            item["_folder_raw"] = folder
            item["folder"] = folder
            item["_lib_label"] = label
            item["lib_label"] = label
            item["root"] = root_s
            _dedupe_id(item, root_s, used_ids)
            merged.append(item)

    # 保留刚扫描/当前正在看的盘为 primary，不要总踢回第一块盘
    primary_s = roots[0]
    try:
        cur = STATE.get("root")
        if cur:
            cur_s = _norm_root_str(cur)
            if any(m.lower() == cur_s.lower() for m in roots):
                primary_s = next(m for m in roots if m.lower() == cur_s.lower())
    except Exception:
        pass
    try:
        STATE["root"] = Path(primary_s)
        STATE["cache_dir"] = ensure_cache_dir(Path(primary_s))
    except OSError:
        pass

    # Pre-classify taxonomy (themes/backgrounds) AND genres in parallel so the
    # first tree_build after publish does not pay the full cold-classification
    # cost.  Previously taxonomy alone was ~875ms; after taxonomy was fixed,
    # genres became the bottleneck at ~971ms (2526/2785 videos had empty
    # genres and ``ensure_video_genres`` re-ran detect_genres on every call):
    #     [PERF] tree_build_facets_breakdown ...
    #         genre_ms=971.6 genre_hits=259 genre_misses=2526 taxonomy_ms=3.7
    # Both classifiers are pure functions (regex + unicodedata, release GIL
    # in C calls), so the thread pool achieves real parallelism here.
    try:
        from concurrent.futures import ThreadPoolExecutor
        from vg.genres import ensure_video_genres, GENRES_VERSION
        from vg.taxonomy import ensure_video_taxonomy, TAXONOMY_VERSION
        from vg.util import meta_worker_count

        def _classify_one(v):
            try:
                if int(v.get("taxonomy_ver") or 0) != TAXONOMY_VERSION:
                    ensure_video_taxonomy(v)
                if int(v.get("genres_ver") or 0) != GENRES_VERSION:
                    ensure_video_genres(v)
            except Exception:
                pass

        if merged:
            workers = meta_worker_count(len(merged))
            if workers > 1 and len(merged) > 64:
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    list(ex.map(_classify_one, merged, chunksize=16))
            else:
                for v in merged:
                    _classify_one(v)
    except Exception:
        pass

    STATE["videos"] = merged
    STATE["tree"] = tree_for_scope(None)
    # heavy=False: skip mark_duplicates (reads entire video files from disk).
    # The dup/dup_n/dup_reason fields are already persisted in the SQLite
    # catalog and survive the restore.  Full-file hash re-detection belongs
    # in the scan path, not in a cached startup.
    rebuild_indexes(merged, heavy=False)
    # Diagnostic: show how many restored videos carry dup badges from cache,
    # so it's clear whether duplicate detection is effective or needs a
    # --rescan to refresh.
    try:
        dup_count = sum(1 for v in merged if v.get("dup"))
        no_sig_count = sum(1 for v in merged if not str(v.get("file_sig") or "").strip())
        from vg.diagnostics import emit
        emit(
            "INFO",
            "publish_unified_library_rebuild",
            force=True,
            heavy=False,
            merged_count=len(merged),
            dup_cached=dup_count,
            missing_file_sig=no_sig_count,
        )
    except Exception:
        pass
    STATE["lib_gen"] = int(STATE.get("lib_gen") or 0) + 1
    # Warm on-disk cache for facets + per-scope folder trees.
    # ``rebuild_indexes`` already ran ``save_facets_disk_cache`` via
    # apply_catalog_to_state, so here we only persist the folder trees:
    #   (a) the lib="all" merged multi-disk tree (STATE["tree"] above),
    #   (b) one tree per individual lib so single-disk selections on the
    #       sidebar also skip the O(N) tree build.
    try:
        from vg.catalog_cache import (
            emit_save_log,
            save_tree_disk_cache,
        )
        import time as _cache_t

        _warm_started = _cache_t.perf_counter()
        # (a) all
        all_tree = STATE.get("tree") or {}
        merged_count = len(merged)
        warm_stats_list: list[dict] = []
        if all_tree and merged_count:
            st = save_tree_disk_cache("", all_tree, merged_count, only_if_missing=False)
            warm_stats_list.append(st)
        # (b) per lib (take unique libs from merged videos to cover only the
        # disks that actually contributed videos this publish)
        seen_libs: set[str] = set()
        for v in merged:
            s = str(v.get("_lib_root") or v.get("root") or "").rstrip("\\/")
            if s:
                seen_libs.add(s)
        for lib_s in seen_libs:
            try:
                from vg.web import videos_for_scope  # type: ignore
            except Exception:
                videos_for_scope = None  # type: ignore
            try:
                scoped_vids = videos_for_scope(lib_s) if videos_for_scope else [
                    x for x in merged
                    if str(x.get("_lib_root") or x.get("root") or "").rstrip("\\/") == lib_s
                ]
            except Exception:
                scoped_vids = [
                    x for x in merged
                    if str(x.get("_lib_root") or x.get("root") or "").rstrip("\\/") == lib_s
                ]
            if not scoped_vids:
                continue
            try:
                per_lib_tree = tree_for_scope(lib_s)
            except Exception:
                per_lib_tree = None
            if per_lib_tree:
                st = save_tree_disk_cache(
                    lib_s, per_lib_tree, len(scoped_vids), only_if_missing=False
                )
                warm_stats_list.append(st)
        overall_ms = (_cache_t.perf_counter() - _warm_started) * 1000.0
        # Aggregate stats into a single PERF line so log scanning tools can
        # answer "did the cache layer save work on this restart?" with one
        # search token.
        written = sum(1 for s in warm_stats_list if s.get("bytes_written"))
        skipped = sum(1 for s in warm_stats_list if s.get("skip_reason"))
        emit_save_log(
            "PERF",
            "publish_tree_disk_cache_warm",
            force=True,
            scope_count=len(warm_stats_list),
            written=written,
            skipped=skipped,
            total_ms=f"{overall_ms:.1f}",
            merged_count=merged_count,
            libs=sorted(seen_libs),
        )
        # One detailed line per scope — not force=True to avoid spam.  Still
        # useful for targeted debug if full logging is on.
        for st in warm_stats_list:
            ev = st.pop("event", "tree_disk_cache_save")
            if st.get("bytes_written"):
                emit_save_log("PERF", ev, force=True, **st)
            elif st.get("skip_reason"):
                emit_save_log("INFO", ev, **st)
    except Exception as exc:
        from vg.diagnostics import error

        error("publish_tree_disk_cache_warm_unexpected_exception", exc)
    # Clear live scan snapshot once the unified catalog is published.
    if STATE.get("scan_live") is not None:
        STATE["scan_live"] = None
    if STATE.get("scan_root"):
        STATE["scan_root"] = ""
    log(f"[多根] 统一片库 {len(roots)} 个目录，共 {len(merged)} 部")
    return len(merged)


def on_scan_finished(root: Path | str) -> None:
    """After a root finishes loading/scanning: archive + maybe re-merge."""
    try:
        root_s = _norm_root_str(root)
    except Exception:
        return
    mounts = get_mounted_roots()
    # Windows 路径大小写不一致时不能当成「未挂载」，否则会冲掉其它盘
    if not any(m.lower() == root_s.lower() for m in mounts):
        try:
            activate_mount(root_s)
        except ValueError:
            set_mounted_roots([root_s], primary=root_s)
        mounts = get_mounted_roots()
    if len(mounts) == 1:
        # Still merge if other disks remain archived in memory.
        other_libs = [
            key
            for key in (STATE.get("disk_libs") or {})
            if str(key).lower() != root_s.lower()
            and ((STATE.get("disk_libs") or {}).get(key) or {}).get("by_id")
        ]
        if other_libs:
            # Restore mounts that were dropped, then publish the full catalog.
            restored = list(mounts)
            for key in other_libs:
                if key.lower() not in {m.lower() for m in restored}:
                    restored.append(key)
            set_mounted_roots(restored, primary=root_s, drop_offline=False)
            archive_current_library()
            publish_unified_library()
            return
        stamp_lib_meta(STATE.get("videos") or [], root=root_s, cache=STATE.get("cache_dir"))
        for v in STATE.get("videos") or []:
            v["lib_label"] = root_label(root_s)
            v["root"] = root_s
            if "_folder_raw" not in v:
                v["_folder_raw"] = (v.get("folder") or "").replace("\\", "/").strip("/")
        archive_current_library()
        return
    archive_current_library()
    publish_unified_library()


def add_mount(path: str | Path, scan_if_needed: bool = True) -> tuple[bool, str]:
    """Add a folder to the unified library (keep existing mounts)."""
    try:
        root = Path(path).expanduser().resolve()
    except OSError as e:
        return False, f"路径无效: {e}"
    if not root.is_dir():
        return False, f"目录不存在: {root}"
    root_s = str(root)
    try:
        archive_current_library()
    except Exception:
        pass
    try:
        activate_mount(root_s)
    except ValueError as e:
        return False, str(e)

    if ensure_library(root_s):
        publish_unified_library()
        return True, f"已加入片库: {root_label(root)}（共 {len(get_mounted_roots())} 个目录）"
    if not scan_if_needed:
        return True, f"已加入，需扫描: {root_s}"
    from vg.scan import start_scan

    ok, msg = start_scan(root, do_thumbs=True, force=False, replace_mounts=False)
    if not ok:
        return False, msg
    return True, f"已加入并开始加载: {root_label(root)}"


def remove_mount(path: str | Path) -> tuple[bool, str]:
    try:
        root_s = _norm_root_str(path)
    except Exception:
        return False, "路径无效"
    mounts = [m for m in get_mounted_roots() if m.lower() != root_s.lower()]
    set_mounted_roots(mounts)
    if not mounts:
        STATE["videos"] = []
        STATE["tree"] = {"name": "全部", "path": "", "children": [], "videos": [], "count": 0}
        rebuild_indexes([])
        return True, "已移除，片库为空"
    publish_unified_library()
    return True, f"已移除 {root_label(root_s)}"


def thumb_id_for_item(item: dict | None) -> str:
    if not item:
        return ""
    return (item.get("_thumb_id") or item.get("id") or "").strip()
