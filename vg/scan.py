# -*- coding: utf-8 -*-
"""Video scanning, indexing, and thumbnail batch jobs."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from vg.cache import (
    ensure_cache_dir,
    list_thumb_ids,
    read_index_counts,
    save_index,
    thumb_path,
)
from vg.catalog import (
    build_tree,
    rebuild_indexes,
    video_category as _video_category,
    video_search_text as _video_search_text,
)
from vg.config import (
    PLAYLIST_EXTS,
    SEGMENT_EXTS,
    THUMB_EXT,
    VIDEO_EXTS,
)
from vg.disk_libs import (
    archive_current_library,
    stamp_lib_meta,
    store_live_library,
    sync_disk_lib_memory,
)
from vg.drives import save_prefs
from vg.genres import ensure_video_genres
from vg.segments import collapse_segment_sets
from vg import state as runtime_state
from vg.state import STATE, release_scan_lock, scan_lock_status, try_acquire_scan_lock
from vg.taxonomy import ensure_video_taxonomy
from vg.thumb_jobs import (
    THUMB_PRIORITY_BATCH,
    ensure_thumbnail_workers,
    submit_thumbnail_job,
    thumbnail_job_key,
)
from vg.thumbs import (
    adopt_thumbs_from_caches,
    folder_key,
    item_folder_key,
    mark_thumbnail_failure,
    missing_thumb_items,
)
from vg.util import (
    format_size,
    is_too_small_video,
    log,
    safe_rel,
    should_skip_dir,
    thumb_worker_count,
    video_id,
)

from vg.media import (
    _video_file_for_thumb,
    make_item_thumbnail,
    media_job_snapshot,
    start_metadata_enrichment,
)
from vg.signatures import file_fingerprint

# Backward-compatible import for callers/tests that used the former private
# helper location. Scanning itself defers signature sampling to the final
# duplicate-candidate stage.
_file_fingerprint = file_fingerprint


def _same_root(a: str | Path | None, b: str | Path | None) -> bool:
    if not a or not b:
        return False
    try:
        return str(Path(a).expanduser().resolve()).casefold() == str(
            Path(b).expanduser().resolve()
        ).casefold()
    except OSError:
        return str(a).replace("/", "\\").rstrip("\\").casefold() == str(b).replace(
            "/", "\\"
        ).rstrip("\\").casefold()


SCAN_WALK_JOB_CAP = 64
SCAN_WALK_SPLIT_MIN_CHILDREN = 8
# Fuse only — initial expand keeps splitting fat nodes until job_cap / balance,
# instead of stopping at depth 2.
SCAN_WALK_SPLIT_MAX_DEPTH = 32


def directory_walk_workers(cpus: int | None, *, top_dirs: int, background: bool) -> int:
    """Thread count for one directory walk.

    Full scans and quiet incremental checks both use logical CPUs minus 2.
    """
    logical = max(1, int(cpus or 1))
    if top_dirs <= 0:
        return 1
    return max(1, logical - 2)


def _scan_walk_child_dirs(path: Path) -> list[str]:
    """Immediate subdirectories that the scanner would actually enter."""
    try:
        names = os.listdir(path)
    except OSError:
        return []
    kids: list[str] = []
    for name in names:
        if should_skip_dir(name):
            continue
        child = path / name
        try:
            # Windows ``Users\\All Users`` is a junction into ProgramData.
            # Expanding it duplicates thousands of dirs and is never video.
            if child.is_symlink() or (hasattr(child, "is_junction") and child.is_junction()):
                continue
            if child.is_dir():
                kids.append(name)
        except OSError:
            continue
    return kids


def _is_linkish_dir(path: Path) -> bool:
    try:
        return path.is_symlink() or (
            hasattr(path, "is_junction") and path.is_junction()
        )
    except OSError:
        return True


def _scandir_files_and_dirs(
    path: Path,
    on_walk_error=None,
) -> tuple[list[str], list[Path], bool]:
    files: list[str] = []
    dirs: list[Path] = []
    try:
        with os.scandir(path) as it:
            for ent in it:
                name = ent.name
                try:
                    if ent.is_dir(follow_symlinks=False):
                        if should_skip_dir(name):
                            continue
                        child = Path(ent.path)
                        if _is_linkish_dir(child):
                            continue
                        dirs.append(child)
                    elif ent.is_file(follow_symlinks=False):
                        files.append(name)
                except OSError:
                    continue
    except OSError as err:
        if on_walk_error is not None:
            on_walk_error(err)
        return files, dirs, False
    dirs.sort(key=lambda p: p.name.casefold())
    return files, dirs, True


class _DirStealQueue:
    """Shared directory queue with idle wait so workers can steal subtrees."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._items: deque[Path] = deque()
        self._busy = 0
        self._waiting = 0

    def seed(self, paths: list[Path]) -> None:
        with self._cond:
            self._items.extend(paths)

    def queued(self) -> int:
        with self._cond:
            return len(self._items)

    def waiting(self) -> int:
        with self._cond:
            return self._waiting

    def push_many(self, paths: list[Path]) -> int:
        if not paths:
            return 0
        with self._cond:
            self._items.extend(paths)
            self._cond.notify(len(paths))
        return len(paths)

    def take(self) -> Path | None:
        with self._cond:
            while True:
                if self._items:
                    self._busy += 1
                    return self._items.popleft()
                if self._busy <= 0:
                    return None
                self._waiting += 1
                try:
                    self._cond.wait(timeout=0.25)
                finally:
                    self._waiting -= 1

    def done_one(self) -> None:
        with self._cond:
            self._busy -= 1
            if self._busy <= 0 and not self._items:
                self._cond.notify_all()


def _run_stolen_directory_walk(
    seeds: list[Path],
    *,
    workers: int,
    visit,
    on_walk_error=None,
    on_heartbeat=None,
    on_steal=None,
    on_plan=None,
    on_observed=None,
) -> int:
    """Walk ``seeds`` with work stealing. ``visit(dirpath, filenames)`` per dir.

    When a directory has 2+ children and the global queue is hungry, siblings
    are pushed for idle workers. Returns how many subtrees were stolen.
    """
    workers = max(1, int(workers or 1))
    steal_count = 0
    steal_lock = threading.Lock()
    q = _DirStealQueue()
    q.seed([Path(p) for p in seeds if p])

    def consume() -> None:
        nonlocal steal_count
        local_dirs = 0
        while True:
            start = q.take()
            if start is None:
                return
            try:
                stack = [Path(start)]
                while stack:
                    dirpath = stack.pop()
                    planned = on_plan(dirpath) if on_plan is not None else None
                    if planned is None:
                        filenames, child_dirs, listed_ok = _scandir_files_and_dirs(
                            dirpath,
                            on_walk_error,
                        )
                        if listed_ok and on_observed is not None:
                            on_observed(dirpath, filenames, child_dirs)
                    else:
                        filenames, child_dirs = planned
                    visit(dirpath, filenames)
                    local_dirs += 1
                    if on_heartbeat is not None and local_dirs % 256 == 0:
                        on_heartbeat(dirpath)
                    if not child_dirs:
                        continue
                    queued = q.queued()
                    idle = q.waiting()
                    can_steal = (
                        workers > 1
                        and len(child_dirs) >= 2
                        and queued < SCAN_WALK_JOB_CAP
                        and (idle > 0 or queued < workers)
                    )
                    if can_steal:
                        stolen, keep = child_dirs[:-1], child_dirs[-1]
                        n = q.push_many(stolen)
                        if n:
                            with steal_lock:
                                steal_count += n
                            if on_steal is not None:
                                on_steal(dirpath, n)
                        stack.append(keep)
                    else:
                        stack.extend(reversed(child_dirs))
                    if local_dirs % 128 == 0:
                        time.sleep(0)
            finally:
                q.done_one()

    if workers <= 1:
        consume()
        return steal_count
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(consume) for _ in range(workers)]
        for fut in as_completed(futs):
            fut.result()
    return steal_count


def expand_scan_walk_jobs(
    root: Path,
    top_kept: list[str],
    *,
    target_jobs: int,
    max_depth: int = SCAN_WALK_SPLIT_MAX_DEPTH,
    split_min_children: int = SCAN_WALK_SPLIT_MIN_CHILDREN,
    job_cap: int = SCAN_WALK_JOB_CAP,
) -> tuple[list[tuple[str, Path]], list[tuple[str, Path]]]:
    """Split fat trees so a C:\\Users-shaped disk can fill the worker pool.

    Top-level fan-out is useless when one child owns almost every directory.
    Keep expanding the fattest remaining job (by immediate child count) until
    there are enough tasks, ``job_cap`` is hit, or nothing left is worth
    splitting. Depth is only a fuse, not a strategy.

    Returns ``(shallow_parents, leaf_jobs)``. Shallow parents are walked
    without descending; leaves are stolen-walk subtrees.
    """
    leaves: list[dict] = [
        {"label": name, "path": root / name, "depth": 0} for name in top_kept
    ]
    shallow: list[tuple[str, Path]] = []
    target_jobs = max(2, int(target_jobs or 2))
    depth_fuse = max(1, int(max_depth or SCAN_WALK_SPLIT_MAX_DEPTH))
    while leaves:
        best_i = -1
        best_kids: list[str] = []
        for i, job in enumerate(leaves):
            if int(job["depth"]) >= depth_fuse:
                continue
            kids = _scan_walk_child_dirs(job["path"])
            if len(kids) > len(best_kids):
                best_kids = kids
                best_i = i
        if best_i < 0 or len(best_kids) < 2:
            break
        enough = len(leaves) >= target_jobs
        if enough and len(best_kids) < split_min_children:
            break
        parent = leaves.pop(best_i)
        shallow.append((str(parent["label"]), Path(parent["path"])))
        for kid in best_kids:
            leaves.append(
                {
                    "label": f"{parent['label']}/{kid}",
                    "path": Path(parent["path"]) / kid,
                    "depth": int(parent["depth"]) + 1,
                }
            )
        if len(leaves) >= job_cap:
            break
    return shallow, [(str(job["label"]), Path(job["path"])) for job in leaves]


def _catalog_item_on_disk(item: dict, root: Path) -> bool:
    """True when the catalog row still has a source file under root."""
    kind = str(item.get("kind") or "").lower()
    try:
        if kind == "ts_set":
            segs = item.get("segments") or []
            if not segs:
                return False
            for rel in segs:
                rel_s = str(rel or "").replace("\\", "/").strip("/")
                if rel_s and (root / rel_s).is_file():
                    return True
            return False
        rel = str(item.get("rel") or "").replace("\\", "/").strip("/")
        if not rel:
            return False
        path = root / rel
        return path.is_file() or path.is_dir()
    except OSError:
        return False


def _folder_hit_by_targets(folder: str, targets: set[str]) -> bool:
    """True if folder is a targeted dir or a descendant of one."""
    key = folder_key(folder)
    if key in targets:
        return True
    for target in targets:
        if target and key.startswith(target + "/"):
            return True
    return False


def prune_missing_catalog_items(
    videos: list[dict],
    root: Path,
    *,
    log_tag: str = "扫描",
) -> list[dict]:
    """Drop catalog rows whose source files were deleted on disk."""
    if not videos:
        return videos
    kept: list[dict] = []
    removed = 0
    samples: list[str] = []
    for item in videos:
        if _catalog_item_on_disk(item, root):
            kept.append(item)
            continue
        removed += 1
        if len(samples) < 5:
            samples.append(
                str(item.get("rel") or item.get("name") or item.get("id") or "?")
            )
    if removed:
        sample = "；".join(samples)
        log(
            f"[{log_tag}] 剔除已删除文件 {removed} 条"
            + (f"（例: {sample}）" if sample else "")
        )
        try:
            from vg.diagnostics import emit

            emit(
                "INFO",
                "catalog_prune_missing_files",
                force=True,
                root=root,
                removed=removed,
                kept=len(kept),
                sample=sample,
            )
        except Exception:
            pass
    return kept


def start_scan(
    root: Path,
    do_thumbs: bool = True,
    force: bool = False,
    replace_mounts: bool = True,
    reuse_preloaded_cache: bool = False,
) -> tuple[bool, str]:
    """切换根目录。force=False 优先读缓存（秒开）再后台按目录核个数；force=True 增量全盘扫描。
    replace_mounts=True：片库只保留这一根；False：保留已挂载目录（用于「加入片库」）。
    """
    requested_at = time.perf_counter()
    root = root.expanduser()
    if STATE["scanning"]:
        from vg.diagnostics import emit

        emit(
            "WARN",
            "scan_start_rejected",
            force=True,
            reason="state_scanning_true",
            requested_root=root,
            active_root=STATE.get("scan_root") or STATE.get("root"),
            thread=threading.current_thread().name,
        )
        return False, "正在扫描中，请稍候"
    if not try_acquire_scan_lock(f"start_scan:{root}"):
        from vg.diagnostics import emit

        lock = scan_lock_status()
        emit(
            "WARN",
            "scan_start_rejected",
            force=True,
            reason="scan_lock_busy",
            requested_root=root,
            active_root=STATE.get("scan_root") or STATE.get("root"),
            lock_holder=lock.get("holder") or "",
            lock_held_ms=lock.get("held_ms") or 0,
            thread=threading.current_thread().name,
        )
        holder = lock.get("holder") or "其他任务"
        return False, f"正在扫描中（{holder}），请稍候"

    root = root.resolve()
    if not root.is_dir():
        release_scan_lock()
        from vg.diagnostics import emit

        emit(
            "WARN",
            "scan_start_rejected",
            force=True,
            reason="root_not_directory",
            requested_root=root,
        )
        return False, f"目录不存在: {root}"

    from vg.diagnostics import emit

    # Manual non-force scans of an already published root used to reload its
    # SQLite catalog and rebuild that single-root index, immediately followed
    # by on_scan_finished() rebuilding the unified index again. Reuse the
    # in-memory published snapshot only when it already has rows for *this*
    # root. Do NOT treat "STATE.root matches + unified STATE has other disks'
    # videos" as coverage — that skipped first-time E:\ walks while C:/D:
    # rows stayed in the merged list (log 239: videos=88 but E catalog=0).
    if not force and not reuse_preloaded_cache:
        root_key = str(root).rstrip("\\/").casefold()
        state_videos = STATE.get("videos") or []
        preloaded_count = sum(
            1
            for video in state_videos
            if str(video.get("_lib_root") or video.get("root") or "")
            .rstrip("\\/")
            .casefold()
            == root_key
        )
        same_root = _same_root(STATE.get("root"), root)
        if preloaded_count > 0:
            reuse_preloaded_cache = True
            emit(
                "INFO",
                "scan_cache_reuse_auto",
                force=True,
                root=root,
                videos=preloaded_count,
                state_videos=len(state_videos),
                same_root=same_root,
                reason="published_snapshot_covers_root",
                load_skipped=True,
                rebuild_skipped=True,
            )
        elif same_root and state_videos:
            emit(
                "INFO",
                "scan_cache_reuse_rejected",
                force=True,
                root=root,
                videos_for_root=0,
                state_videos=len(state_videos),
                same_root=True,
                reason="unified_snapshot_has_no_rows_for_root",
            )

    emit(
        "INFO",
        "scan_lock_acquired",
        force=True,
        root=root,
        wait_ms=f"{(time.perf_counter() - requested_at) * 1000.0:.1f}",
        force_scan=force,
        replace_mounts=replace_mounts,
    )

    want_bg_verify = False

    def run():
        nonlocal want_bg_verify
        try:
            # 换盘前归档当前片库，局域网历史仍可播旧盘视频
            try:
                cur = STATE.get("root")
                if cur and Path(cur).resolve() != root.resolve():
                    archive_current_library()
            except OSError:
                archive_current_library()
            try:
                from vg.roots import activate_mount, get_mounted_roots, set_mounted_roots

                root_s = str(root.resolve())
                if replace_mounts:
                    set_mounted_roots([root_s], primary=root_s)
                else:
                    activate_mount(root_s)
            except Exception as e:
                log(f"[多根] 设置挂载失败: {e}")
            STATE["root"] = root
            STATE["cache_dir"] = ensure_cache_dir(root)
            save_prefs(last_root=str(root))
            # User-triggered scan (soft or force): drop validation cooldown so
            # deleted files are not skipped for up to 5 minutes.
            try:
                from vg.catalog_db import clear_catalog_validation_time

                clear_catalog_validation_time(ensure_cache_dir(root))
            except Exception as exc:
                from vg.diagnostics import emit as _emit

                _emit(
                    "WARN",
                    "catalog_validation_marker_clear_failed",
                    force=True,
                    root=root,
                    error=str(exc),
                )
            if force:
                STATE["scan_progress"] = f"正在增量扫描 {root} …"
                STATE["thumb_progress"] = ""
                try:
                    archive_current_library()
                except Exception:
                    pass
                scan_videos(root, do_thumbs=do_thumbs, incremental=True, quiet=False, burst_thumbs=True)
            else:
                STATE["scan_progress"] = f"正在加载 {root}（优先缓存）…"
                STATE["thumb_progress"] = ""
                STATE["scanning"] = True
                if reuse_preloaded_cache:
                    # main.py already restored this root while publishing the
                    # unified cached library.  Loading it again here caused a
                    # second catalog/index rebuild on every multi-root start.
                    reuse_started = time.perf_counter()
                    root_key = str(root.resolve()).casefold()
                    preloaded_count = sum(
                        1
                        for video in (STATE.get("videos") or [])
                        if str(video.get("_lib_root") or video.get("root") or "")
                        .rstrip("\\/")
                        .casefold()
                        == root_key.rstrip("\\/")
                    )
                    used_cache = True
                    # Keep the cheap folder-count verification that normally
                    # follows a cache load; only the duplicate catalog load
                    # itself is skipped.
                    want_bg_verify = True
                    emit(
                        "PERF",
                        "scan_cache_reused_preloaded",
                        force=True,
                        root=root,
                        videos=preloaded_count,
                        load_skipped=True,
                        rebuild_skipped=True,
                        elapsed_ms=(time.perf_counter() - reuse_started) * 1000.0,
                    )
                    STATE["scan_progress"] = f"已复用启动缓存，共 {preloaded_count} 个视频"
                    # Soft rescan after enabling duration/audio probe must still
                    # enqueue ffprobe. Previously this path returned without
                    # touching metadata, so cards stayed duration-less.
                    _start_metadata_after_scan(root)
                else:
                    used_cache = load_or_scan(root, do_thumbs=do_thumbs, force=False, background=False)
                STATE["scanning"] = False
                # 读到缓存后只后台核文件个数；个数一致则不再 walk 视频盘
                want_bg_verify = bool(used_cache)
        except Exception as e:
            STATE["scan_progress"] = f"扫描失败: {e}"
            from vg.diagnostics import error

            error("scan_thread_failed", e, root=root, force_scan=force)
            STATE["scanning"] = False
        finally:
            STATE["scanning"] = False
            try:
                release_scan_lock()
                emit(
                    "INFO",
                    "scan_lock_released",
                    force=True,
                    root=root,
                    thread=threading.current_thread().name,
                )
            except RuntimeError as exc:
                from vg.diagnostics import error

                error("scan_lock_release_failed", exc, root=root)
            if want_bg_verify:
                threading.Thread(
                    target=_bg_count_then_maybe_scan,
                    args=(root, do_thumbs),
                    daemon=True,
                ).start()

    STATE["scanning"] = True
    threading.Thread(target=run, daemon=True).start()
    mode = "增量全盘扫描" if force else "加载缓存"
    return True, f"开始{mode} {root}"


def _count_one_tree(
    root: Path,
    start: Path,
    *,
    children: list[str] | None = None,
    on_walk_error=None,
) -> tuple[dict[str, int], int, int]:
    """Count video filenames under ``start``. ``children=[]`` means this directory only."""
    local_counts: dict[str, int] = {}
    directories = 0
    files_seen = 0
    try:
        start_resolved = Path(start).resolve()
    except OSError:
        start_resolved = Path(start)
    allowed = set(children) if children is not None else None
    for dirpath, dirnames, filenames in os.walk(start, onerror=on_walk_error):
        directories += 1
        files_seen += len(filenames)
        kept: list[str] = []
        for name in dirnames:
            if should_skip_dir(name):
                continue
            child = Path(dirpath) / name
            try:
                if child.is_symlink() or (
                    hasattr(child, "is_junction") and child.is_junction()
                ):
                    continue
            except OSError:
                continue
            kept.append(name)
        dirnames[:] = kept
        if allowed is not None:
            try:
                at_start = Path(dirpath).resolve() == start_resolved
            except OSError:
                at_start = False
            if at_start:
                dirnames[:] = [d for d in dirnames if d in allowed]
        try:
            folder = folder_key(safe_rel(Path(dirpath), root))
        except (ValueError, OSError):
            continue
        n = 0
        for name in filenames:
            ext = Path(name).suffix.lower()
            if ext in VIDEO_EXTS or ext in PLAYLIST_EXTS:
                n += 1
        if n:
            local_counts[folder] = local_counts.get(folder, 0) + n
    return local_counts, directories, files_seen


def count_video_files_by_folder(
    root: Path,
    emit_diagnostics: bool = True,
) -> dict[str, int]:
    """Cheap walk: count video/playlist filenames per directory (no size/fingerprint).

    Drive-root validation uses the same top-level fan-out as a full scan so a
    C:\\Users-shaped disk can fill a worker pool instead of one serial os.walk.
    """
    started = time.perf_counter()
    root = Path(root)
    counts: dict[str, int] = {}
    directories = 0
    files_seen = 0
    walk_workers = 1
    walk_mode = "serial"
    job_n = 0

    def on_walk_error(err: OSError) -> None:
        if emit_diagnostics:
            log(f"[计数] 跳过无权限目录: {err}")

    def merge_tree(start: Path, *, children: list[str] | None = None) -> None:
        nonlocal directories, files_seen
        local, dirs_n, files_n = _count_one_tree(
            root,
            start,
            children=children,
            on_walk_error=on_walk_error,
        )
        directories += dirs_n
        files_seen += files_n
        for folder, n in local.items():
            counts[folder] = counts.get(folder, 0) + n

    top_kept: list[str] = []
    try:
        top_names = sorted(os.listdir(root))
        top_kept = [
            n for n in top_names if (root / n).is_dir() and not should_skip_dir(n)
        ]
    except OSError as err:
        on_walk_error(err if isinstance(err, OSError) else OSError(err))

    cpus = max(1, os.cpu_count() or 4)
    walk_workers = directory_walk_workers(
        cpus,
        top_dirs=len(top_kept),
        background=False,
    )
    target_jobs = min(SCAN_WALK_JOB_CAP, max(walk_workers, 8))
    shallow_jobs, leaf_jobs = expand_scan_walk_jobs(
        root,
        top_kept,
        target_jobs=target_jobs,
    )
    job_n = len(leaf_jobs)
    if walk_workers > 1:
        walk_mode = "steal_expanded" if shallow_jobs else "steal"
    steal_n = 0
    if emit_diagnostics:
        log(
            f"[计数] 目录遍历 {walk_workers} 线程"
            f"（顶层 {len(top_kept)} → 任务 {job_n}"
            f"{'，拆分 ' + ','.join(n for n, _p in shallow_jobs[:4]) if shallow_jobs else ''}"
            f"，逻辑核 {cpus}）"
        )
        try:
            from vg.diagnostics import emit as _count_emit

            _count_emit(
                "INFO",
                "catalog_folder_count_parallel",
                force=True,
                root=root,
                workers=walk_workers,
                top_dirs=len(top_kept),
                jobs=job_n,
                split_parents=",".join(name for name, _path in shallow_jobs[:12]),
                cpus=cpus,
                mode=walk_mode,
            )
        except Exception:
            pass

    heartbeat_lock = threading.Lock()
    last_heartbeat = started
    stats_lock = threading.Lock()

    def count_visit(dirpath: Path, filenames: list[str]) -> None:
        nonlocal directories, files_seen
        with stats_lock:
            directories += 1
            files_seen += len(filenames)
            try:
                folder = folder_key(safe_rel(Path(dirpath), root))
            except (ValueError, OSError):
                return
            n = 0
            for name in filenames:
                ext = Path(name).suffix.lower()
                if ext in VIDEO_EXTS or ext in PLAYLIST_EXTS:
                    n += 1
            if n:
                counts[folder] = counts.get(folder, 0) + n

    def count_heartbeat(active: Path) -> None:
        nonlocal last_heartbeat
        if not emit_diagnostics:
            return
        now = time.perf_counter()
        with heartbeat_lock:
            if now - last_heartbeat < 3.0:
                return
            last_heartbeat = now
        try:
            from vg.diagnostics import emit as _count_hb

            with stats_lock:
                dirs_so_far = directories
                files_so_far = sum(counts.values())
            _count_hb(
                "INFO",
                "catalog_folder_count_heartbeat",
                force=True,
                root=root,
                directories=dirs_so_far,
                video_files=files_so_far,
                workers=walk_workers,
                active_subtree=str(active),
                elapsed_ms=f"{(now - started) * 1000.0:.0f}",
            )
        except Exception:
            pass

    if walk_workers <= 1:
        merge_tree(root)
    else:
        merge_tree(root, children=[])
        for _label, parent in shallow_jobs:
            merge_tree(parent, children=[])
        steal_n = _run_stolen_directory_walk(
            [path for _label, path in leaf_jobs],
            workers=walk_workers,
            visit=count_visit,
            on_walk_error=on_walk_error,
            on_heartbeat=count_heartbeat,
        )
    try:
        if not emit_diagnostics:
            return counts
        from vg.diagnostics import perf

        perf(
            "catalog_folder_count_walk",
            (time.perf_counter() - started) * 1000.0,
            force=True,
            root=root,
            directories=directories,
            files_seen=files_seen,
            video_folders=len(counts),
            video_files=sum(counts.values()),
            workers=walk_workers,
            jobs=job_n,
            steals=steal_n,
            mode=walk_mode,
        )
    except Exception:
        pass
    return counts


def _count_video_files_for_validation(root: Path) -> dict[str, int]:
    """Count validation files without running the Python walk in waitress' process.

    Drive-root validation can traverse tens of thousands of directories.  A
    spawned worker keeps that Python/GIL work out of the server process, while
    the direct fallback preserves compatibility with frozen builds and Unix.
    """
    try:
        parts = Path(root).resolve().parts
    except Exception:
        parts = ()
    is_drive_root = len(parts) <= 1
    if not is_drive_root or getattr(sys, "frozen", False):
        return count_video_files_by_folder(root)
    started = time.perf_counter()
    try:
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor

        ctx = mp.get_context("spawn")
        with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as pool:
            counts = pool.submit(
                count_video_files_by_folder,
                Path(root),
                False,
            ).result()
        from vg.diagnostics import perf

        perf(
            "catalog_folder_count_worker",
            (time.perf_counter() - started) * 1000.0,
            force=True,
            root=root,
            mode="spawn_process",
            video_files=sum(counts.values()),
            video_folders=len(counts),
        )
        return counts
    except Exception as exc:
        from vg.diagnostics import emit

        emit(
            "WARN",
            "catalog_folder_count_worker_fallback",
            force=True,
            root=root,
            mode="in_process",
            error=type(exc).__name__,
        )
        return count_video_files_by_folder(root)


def changed_folder_keys(stored: dict[str, int] | None, live: dict[str, int]) -> set[str]:
    old = {folder_key(k): int(v) for k, v in (stored or {}).items() if int(v) > 0}
    new = {folder_key(k): int(v) for k, v in live.items() if int(v) > 0}
    return {key for key in set(old) | set(new) if old.get(key, 0) != new.get(key, 0)}


def _lower_thread_priority() -> None:
    """Background catalog checks should not compete with the UI thread."""
    if os.name != "nt":
        try:
            os.nice(10)
        except (AttributeError, OSError):
            pass
        return
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentThread.restype = ctypes.c_void_p
        # THREAD_PRIORITY_BELOW_NORMAL
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), -1)
    except Exception:
        pass


def _bg_count_then_maybe_scan(root: Path, do_thumbs: bool) -> None:
    """Reopen: count files per folder; scan only folders whose counts changed.

    The expensive disk count must not hold the global scan lock — otherwise a
    soft click on another drive is rejected for ~10–30s and feels hung. Only
    the optional incremental ``scan_videos`` path acquires the lock.
    """
    from vg.diagnostics import emit, error, perf

    # Drive roots (D:\, C:\) are huge walks — defer so first paint / L1 warm win.
    # Temp/test library paths stay immediate.
    defer_s = 0.0
    try:
        parts = Path(root).resolve().parts
        if len(parts) <= 1:
            defer_s = 2.5
    except Exception:
        defer_s = 0.0
    if defer_s > 0:
        emit(
            "INFO",
            "background_catalog_validation_deferred",
            force=True,
            root=root,
            delay_ms=int(defer_s * 1000),
        )
        time.sleep(defer_s)

    started = time.perf_counter()
    outcome = "not_started"
    changed_count = 0
    updating_started = False
    lock_held = False
    try:
        _lower_thread_priority()
        if STATE.get("scanning"):
            outcome = "skipped_scanning"
            emit(
                "WARN",
                "background_catalog_validation_skipped",
                force=True,
                root=root,
                reason=outcome,
                **scan_lock_status(),
            )
            return
        if STATE.get("root") and Path(STATE["root"]).resolve() != root.resolve():
            log("[计数] 跳过：根目录已切换")
            outcome = "root_changed"
            emit(
                "WARN",
                "background_catalog_validation_skipped",
                force=True,
                root=root,
                current_root=STATE.get("root"),
                reason=outcome,
            )
            return
        cache = ensure_cache_dir(root)
        STATE["cache_dir"] = cache
        # A drive-root walk can take tens of seconds. Persist a short
        # validation TTL so restarting or switching the same disk repeatedly
        # does not immediately launch another full walk. Explicit force/rescan
        # remains available because it does not schedule this verifier.
        validation_cooldown_s = 300.0
        try:
            from vg.catalog_db import read_catalog_validation_time

            last_validation = read_catalog_validation_time(cache)
        except Exception:
            last_validation = 0.0
        age_s = time.time() - last_validation if last_validation else None
        if age_s is not None and 0.0 <= age_s < validation_cooldown_s:
            outcome = "skipped_recent_validation"
            emit(
                "INFO",
                "background_catalog_validation_skipped",
                force=True,
                root=root,
                reason="recent_validation",
                age_s=f"{age_s:.1f}",
                cooldown_s=int(validation_cooldown_s),
            )
            # The expensive disk walk is suppressed, but thumbnail filling
            # is independent of folder validation and must still run. Reuse
            # the persisted counts only as bookkeeping inputs.
            if do_thumbs:
                from vg.disk_libs import item_belongs_to_root

                stored_n, stored_counts = read_index_counts(cache)
                scoped = [
                    v
                    for v in (STATE.get("videos") or [])
                    if item_belongs_to_root(v, root)
                ]
                if not scoped:
                    scoped = _load_index_videos(cache, root)
                fill_thumbs_for_videos(
                    scoped,
                    burst=False,
                    cache=cache,
                    root=root,
                    file_count=stored_n,
                    folder_counts=stored_counts,
                )
            _start_metadata_after_scan(root)
            return
        from vg.dir_snapshot import DirSnapshot

        dir_snap = DirSnapshot.load(cache)
        if dir_snap.dirs and dir_snap.volume_usn_unchanged(root):
            outcome = "usn_unchanged"
            emit(
                "INFO",
                "background_catalog_validation_skipped",
                force=True,
                root=root,
                reason=outcome,
            )
            try:
                from vg.catalog_db import write_catalog_validation_time

                write_catalog_validation_time(cache)
            except Exception:
                pass
            return
        if dir_snap.dirs:
            # Cached directory mtimes already know which subtrees changed.
            # Verify on this low-priority thread instead of counting every file.
            if not try_acquire_scan_lock(f"bg_snapshot_verify:{root}"):
                outcome = "skipped_scan_lock_busy"
                emit(
                    "WARN",
                    "background_catalog_validation_skipped",
                    force=True,
                    root=root,
                    reason=outcome,
                    **scan_lock_status(),
                )
                return
            lock_held = True
            try:
                scan_videos(root, do_thumbs=False, incremental=True, quiet=True)
                outcome = "snapshot_incremental"
            finally:
                release_scan_lock()
                lock_held = False
            try:
                from vg.catalog_db import write_catalog_validation_time

                write_catalog_validation_time(cache)
            except Exception:
                pass
            if do_thumbs:
                from vg.disk_libs import item_belongs_to_root

                stored_n, stored_counts = read_index_counts(cache)
                scoped = [
                    v
                    for v in (STATE.get("videos") or [])
                    if item_belongs_to_root(v, root)
                ]
                fill_thumbs_for_videos(
                    scoped,
                    burst=False,
                    cache=cache,
                    root=root,
                    file_count=stored_n,
                    folder_counts=stored_counts,
                )
            _start_metadata_after_scan(root)
            emit(
                "INFO",
                "background_catalog_validation_complete",
                force=True,
                root=root,
                outcome=outcome,
                elapsed_ms=f"{(time.perf_counter() - started) * 1000.0:.1f}",
            )
            return
        count_started = time.perf_counter()
        emit(
            "INFO",
            "background_catalog_validation_count_start",
            force=True,
            root=root,
            holds_scan_lock=False,
        )
        live = _count_video_files_for_validation(root)
        emit(
            "INFO",
            "background_catalog_validation_count_complete",
            force=True,
            root=root,
            video_files=sum(live.values()),
            video_folders=len(live),
            elapsed_ms=f"{(time.perf_counter() - count_started) * 1000.0:.1f}",
        )
        live_n = sum(live.values())
        _stored_n, stored = read_index_counts(cache)
        if stored is None:
            videos = _load_index_videos(cache, root)
            # Empty catalog + live files on disk = first scan never ran
            # (e.g. false cache-reuse). Writing counts alone would mark the
            # disk "validated" and leave the library empty forever.
            if not videos and live_n > 0:
                emit(
                    "WARN",
                    "background_catalog_empty_but_disk_has_files",
                    force=True,
                    root=root,
                    live_files=live_n,
                    live_folders=len(live),
                    catalog_videos=0,
                    action="full_scan",
                )
                if not try_acquire_scan_lock(f"bg_validation_empty_catalog:{root}"):
                    outcome = "skipped_scan_lock_busy"
                    emit(
                        "WARN",
                        "background_catalog_validation_skipped",
                        force=True,
                        root=root,
                        reason="scan_lock_busy_before_empty_catalog_scan",
                        live_files=live_n,
                        **scan_lock_status(),
                    )
                    return
                lock_held = True
                STATE["scan_progress"] = f"首次扫描 {root}（磁盘约 {live_n} 个视频）…"
                STATE["updating"] = True
                updating_started = True
                outcome = "empty_catalog_full_scan"
                emit(
                    "INFO",
                    "library_updating_started",
                    force=True,
                    root=root,
                    changed_folders=len(live),
                    live_files=live_n,
                    reason="empty_catalog_disk_has_files",
                )
                try:
                    scan_videos(
                        root,
                        do_thumbs=do_thumbs,
                        incremental=True,
                        quiet=True,
                        only_folders=None,
                        folder_counts=live,
                        burst_thumbs=False,
                    )
                    do_thumbs = False
                finally:
                    STATE["updating"] = False
                    emit(
                        "INFO",
                        "library_updating_finished",
                        force=True,
                        root=root,
                        changed_folders=len(live),
                        reason="empty_catalog_disk_has_files",
                    )
            else:
                saved = save_index(cache, root, videos, file_count=live_n, folder_counts=live)
                if not saved:
                    emit(
                        "WARN",
                        "background_catalog_counts_save_failed",
                        force=True,
                        root=root,
                        cache=cache,
                        live_files=live_n,
                        videos=len(videos),
                    )
                log(f"[缓存] 已写入目录文件计数 {live_n}，跳过全盘扫描")
                STATE["scan_progress"] = f"已加载缓存，共 {len(videos)} 个视频"
                outcome = "counts_initialized"
                emit(
                    "INFO",
                    "background_catalog_counts_initialized",
                    force=True,
                    root=root,
                    live_files=live_n,
                    catalog_videos=len(videos),
                    skip_scan=True,
                )
        else:
            changed = changed_folder_keys(stored, live)
            changed_count = len(changed)
            catalog_n = _catalog_row_count(cache)
            # Poisoned empty catalog: folder counts were written without a
            # real walk (log 239 counts_initialized rows=0). Counts match
            # disk so changed=[] — still must scan when the catalog is empty.
            if not changed and catalog_n == 0 and live_n > 0:
                emit(
                    "WARN",
                    "background_catalog_counts_without_videos",
                    force=True,
                    root=root,
                    live_files=live_n,
                    stored_files=_stored_n,
                    catalog_videos=0,
                    action="full_scan",
                )
                changed = set(live.keys()) or {""}
                changed_count = len(changed)
            if not changed:
                log(f"[缓存] 文件个数一致（{live_n}），跳过扫描")
                STATE["scan_progress"] = f"已加载缓存，共 {len(STATE.get('videos') or [])} 个视频"
                outcome = "stable_no_changes"
            else:
                log(f"[增量] {len(changed)} 个目录文件数变化，定向扫描")
                if not try_acquire_scan_lock(f"bg_validation_scan:{root}"):
                    outcome = "skipped_scan_lock_busy"
                    emit(
                        "WARN",
                        "background_catalog_validation_skipped",
                        force=True,
                        root=root,
                        reason="scan_lock_busy_before_incremental",
                        changed_folders=changed_count,
                        **scan_lock_status(),
                    )
                    return
                lock_held = True
                STATE["scan_progress"] = f"后台更新 {len(changed)} 个目录…"
                STATE["updating"] = True
                updating_started = True
                outcome = "incremental_scan"
                emit(
                    "INFO",
                    "library_updating_started",
                    force=True,
                    root=root,
                    changed_folders=changed_count,
                    live_files=live_n,
                )
                try:
                    scan_videos(
                        root,
                        do_thumbs=do_thumbs,
                        incremental=True,
                        quiet=True,
                        only_folders=changed,
                        folder_counts=live,
                        burst_thumbs=False,
                    )
                    do_thumbs = False
                finally:
                    STATE["updating"] = False
                    emit(
                        "INFO",
                        "library_updating_finished",
                        force=True,
                        root=root,
                        changed_folders=changed_count,
                    )
        try:
            from vg.catalog_db import (
                CATALOG_VALIDATION_MARKER_NAME,
                catalog_mtime,
                write_catalog_validation_time,
            )

            catalog_mtime_before = catalog_mtime(cache)
            validation_saved = write_catalog_validation_time(cache)
            catalog_mtime_after = catalog_mtime(cache)
            marker_sidecar = Path(cache) / CATALOG_VALIDATION_MARKER_NAME
            emit(
                "INFO",
                "background_catalog_validation_marker",
                force=True,
                root=root,
                saved=validation_saved,
                marker_storage="sidecar" if marker_sidecar.is_file() else "sqlite_fallback",
                catalog_mtime_unchanged=(catalog_mtime_before == catalog_mtime_after),
                catalog_mtime_before=f"{catalog_mtime_before:.6f}",
                catalog_mtime_after=f"{catalog_mtime_after:.6f}",
            )
        except Exception as exc:
            emit(
                "WARN",
                "background_catalog_validation_marker_failed",
                force=True,
                root=root,
                error=type(exc).__name__,
            )
        if do_thumbs:
            from vg.disk_libs import item_belongs_to_root

            scoped = [
                v
                for v in (STATE.get("videos") or [])
                if item_belongs_to_root(v, root)
            ]
            if not scoped:
                scoped = _load_index_videos(cache, root)
            fill_thumbs_for_videos(
                scoped,
                burst=False,
                cache=cache,
                root=root,
                file_count=live_n,
                folder_counts=live,
            )
        # Cache-stable reopen used to fill thumbs only. Kick duration/audio
        # enrichment too so enabling probes + soft rescan actually works.
        if outcome in (
            "stable_no_changes",
            "counts_initialized",
            "incremental_scan",
            "empty_catalog_full_scan",
        ):
            _start_metadata_after_scan(root)
    except Exception as e:
        outcome = "failed"
        log(f"[计数] 失败: {e}")
        error(
            "background_catalog_validation_failed",
            e,
            root=root,
            changed_folders=changed_count,
            updating_started=updating_started,
        )
    finally:
        STATE["updating"] = False
        perf(
            "background_catalog_validation",
            (time.perf_counter() - started) * 1000.0,
            force=True,
            root=root,
            outcome=outcome,
            changed_folders=changed_count,
            updating_started=updating_started,
            library_stable=not bool(STATE.get("scanning")) and not bool(STATE.get("updating")),
        )
        if lock_held:
            try:
                release_scan_lock()
                emit(
                    "INFO",
                    "scan_lock_released",
                    force=True,
                    root=root,
                    thread=threading.current_thread().name,
                    reason="bg_validation",
                )
            except RuntimeError as exc:
                error("background_catalog_validation_lock_release_failed", exc, root=root)

def _load_old_video_map(cache: Path, root: Path) -> dict[str, dict]:
    """从目录库建立 rel → 条目，供增量复用（TS 合集拆成段后不进 map，行走时重收）。"""
    from vg.catalog_db import catalog_exists, load_catalog_by_rel, read_catalog_root

    if not catalog_exists(cache):
        log(f"[增量] 无目录库，无法复用: {cache}")
        return {}
    stored_root = read_catalog_root(cache)
    if stored_root and not _same_root(stored_root, root):
        log(f"[增量] 目录库根路径不匹配，跳过复用: stored={stored_root!r} root={root!r}")
        return {}
    try:
        mapping = load_catalog_by_rel(cache, restore_search_cache=True)
        if not mapping:
            log(f"[增量] 目录库为空，无法复用: {cache}")
        return mapping
    except Exception as e:
        log(f"[增量] 读取旧索引失败: {e}")
        return {}


def _probe_cache_still_valid(prev: dict, item: dict) -> bool:
    """True when prev's probe fields still describe this file.

    A replaced file keeps the same path id. Size, mtime, or file_sig differing
    means the old bitrate/resolution must not be copied forward. Missing stats
    on either side do not count as a change, so rebuilt containers can still
    inherit.
    """
    try:
        prev_size = int(prev.get("size") or 0)
        item_size = int(item.get("size") or 0)
    except (TypeError, ValueError):
        prev_size, item_size = 0, 0
    if prev_size and item_size and prev_size != item_size:
        return False
    try:
        prev_mtime = float(prev.get("mtime") or 0)
        item_mtime = float(item.get("mtime") or 0)
    except (TypeError, ValueError):
        prev_mtime, item_mtime = 0.0, 0.0
    if prev_mtime and item_mtime and prev_mtime != item_mtime:
        return False
    prev_sig = str(prev.get("file_sig") or "").strip()
    item_sig = str(item.get("file_sig") or "").strip()
    if prev_sig and item_sig and prev_sig != item_sig:
        return False
    return True


_PROBE_FIELDS_INHERITED_ON_RESCAN = (
    "duration",
    "duration_h",
    "has_audio",
    "audio_codec",
    "audio_channels",
    "sample_rate",
    "audio_hard",
    "width",
    "height",
    "fps",
    "bitrate",
    "video_codec",
    "probe_video_meta_done",
    "bad",
    "bad_reason",
)


def _inherit_probe_fields_if_same_file(item: dict, prev: dict) -> bool:
    if not _probe_cache_still_valid(prev, item):
        return False
    changed = False
    for key in _PROBE_FIELDS_INHERITED_ON_RESCAN:
        if item.get(key) in (None, "", []) and prev.get(key) not in (None, "", []):
            item[key] = prev.get(key)
            changed = True
    return changed


def generate_thumbs_parallel(
    missing: list[dict],
    cached_n: int = 0,
    label: str = "新建",
    *,
    burst: bool = False,
    cache: Path | None = None,
) -> tuple[int, int]:
    """通过共享队列生成预览图。返回 (成功数, 失败数)。"""
    started = time.perf_counter()
    ffmpeg = STATE.get("ffmpeg")
    cache = cache or STATE.get("cache_dir")
    if not missing or not ffmpeg or not cache:
        from vg.diagnostics import perf as diagnostic_perf

        diagnostic_perf(
            "generate_thumbs_parallel",
            (time.perf_counter() - started) * 1000.0,
            total=len(missing or []),
            cached=cached_n,
            workers=0,
            burst=burst,
            skipped=True,
            reason="no_missing_or_ffmpeg_or_cache",
        )
        return 0, 0
    total = len(missing)
    # Large deferred batches are already off the scan thread. Use the same
    # CPU-aware burst pool as first-scan work (reserve two logical CPUs for
    # waitress/UI) instead of leaving hundreds of jobs behind a 2-worker
    # queue. Visible thumbnail requests retain higher queue priority.
    worker_burst = bool(burst or total >= 200)
    workers = thumb_worker_count(total, burst=worker_burst)
    ensure_thumbnail_workers(workers)
    mode = "满核" if worker_burst else "后台"
    STATE["thumb_progress"] = (
        f"预览图缓存 {cached_n} 个，需{label} {total} 个"
        f"（{mode} {workers} 路）…"
    )
    log(f"[预览图] {label} {total} 个，{mode} {workers} 路生成")
    from vg.diagnostics import call as diagnostic_call

    diagnostic_call(
        "generate_thumbs_parallel",
        total=total,
        cached=cached_n,
        workers=workers,
        burst=worker_burst,
        label=label,
    )
    ok_n = 0
    fail_n = 0
    skipped_n = 0
    done = 0
    last_log_i = 0
    def one(item: dict) -> bool | None:
        try:
            # ffprobe already proved that audio-only/corrupt containers cannot
            # yield a frame.  Do not enqueue another ffmpeg attempt during the
            # bulk scan; otherwise each retry burns time and pollutes the log.
            bad_reason = str(item.get("bad_reason") or "").casefold()
            if item.get("bad") and ("无视频流" in bad_reason or "no video" in bad_reason):
                return False
            return make_item_thumbnail(item, ffmpeg, cache, burst=burst)
        except Exception:
            return False

    future_items: dict = {}
    for item in missing:
        key = thumbnail_job_key(cache, item.get("_thumb_id") or item["id"], item=item)
        future = submit_thumbnail_job(
            key,
            lambda item=item: one(item),
            priority=THUMB_PRIORITY_BATCH,
        )
        future_items.setdefault(future, []).append(item)

    for future in as_completed(future_items):
        try:
            result = future.result()
            ok = bool(result)
        except Exception:
            result = False
            ok = False
        for item in future_items[future]:
            name = item.get("name") or item.get("rel") or item.get("id") or "?"
            try:
                done += 1
                i = done
                if ok:
                    ok_n += 1
                elif result is None:
                    skipped_n += 1
                else:
                    fail_n += 1
                    mark_thumbnail_failure(
                        item,
                        reason=item.get("bad_reason") or "thumbnail_generation_failed",
                    )
                    item["has_thumb"] = False
                    item["thumb_v"] = 0
                    log(f"[预览图] ({i}/{total}) 失败  {name}")
                # Success lines are aggregated; failures always print.
                if (i == total) or (i - last_log_i >= 40):
                    last_log_i = i
                    log(
                        f"[预览图] 进度 {i}/{total}（成功 {ok_n}，失败 {fail_n}，跳过 {skipped_n}）"
                    )
                STATE["thumb_progress"] = (
                    f"{label}加密预览图 {i}/{total}（已有缓存 {cached_n}，成功 {ok_n}）…"
                )
            except Exception as exc:
                fail_n += 1
                log(f"[预览图] 更新状态失败 {name}: {exc}")
    from vg.diagnostics import perf as diagnostic_perf

    diagnostic_perf(
        "generate_thumbs_parallel",
        (time.perf_counter() - started) * 1000.0,
        total=total,
        cached=cached_n,
        workers=workers,
        burst=worker_burst,
        ok=ok_n,
        failed=fail_n,
        failure_markers_updated=fail_n,
        discarded=skipped_n,
    )
    if fail_n:
        from vg.diagnostics import emit

        emit(
            "INFO",
            "thumbnail_failure_markers_updated",
            force=True,
            marked=fail_n,
            total=total,
            label=label,
        )
    return ok_n, fail_n


_thumb_bulk_requests: dict[str, dict] = {}


def fill_thumbs_for_videos(
    videos: list[dict],
    *,
    burst: bool,
    cache: Path | None,
    root: Path | None,
    file_count: int | None = None,
    folder_counts: dict[str, int] | None = None,
) -> tuple[int, int]:
    if not videos or not cache or not STATE.get("ffmpeg"):
        return 0, 0
    owner = root or cache
    key = runtime_state._runtime_root_key(owner)
    request = dict(videos=[dict(item) for item in videos], burst=burst, cache=cache, root=root,
                   file_count=file_count, folder_counts=folder_counts)
    with runtime_state._thumb_bulk_lock:
        _thumb_bulk_requests[key] = request
        if not runtime_state.register_thumb_bulk(owner):
            return 0, 0
    STATE["thumb_progress"] = "预览图已转后台补全，目录扫描无需等待"

    def run() -> None:
        while True:
            with runtime_state._thumb_bulk_lock:
                pending = _thumb_bulk_requests.pop(key, None)
                if pending is None:
                    runtime_state.unregister_thumb_bulk(owner)
                    return
            try:
                _fill_thumbs_background(**pending)
            except Exception as exc:
                from vg.util import log_error
                log_error("thumbnail_bulk_background_failed", exc, root=root)
                STATE["thumb_progress"] = f"预览图后台生成失败: {exc}"

    threading.Thread(target=run, daemon=True, name="thumb-bulk-background").start()
    return 0, 0


def _fill_thumbs_background(
    videos: list[dict],
    *,
    burst: bool,
    cache: Path | None,
    root: Path | None,
    file_count: int | None = None,
    folder_counts: dict[str, int] | None = None,
) -> tuple[int, int]:
    """Fill missing .vgt files only: reuse other caches, then ffmpeg the rest."""
    started = time.perf_counter()
    from vg.disk_libs import _disk_lib_snapshot
    from vg.schema import THUMB_RESULT_FIELDS, media_item_signature

    current = _disk_lib_snapshot(str(Path(root).resolve())) if root else None
    if current is not None:
        by_signature = {media_item_signature(item): item for item in current}
        for item in videos:
            latest = by_signature.get(media_item_signature(item))
            if latest is not None:
                for field in THUMB_RESULT_FIELDS:
                    if field in latest:
                        item[field] = latest[field]
                    else:
                        item.pop(field, None)
    snapshot_ms = (time.perf_counter() - started) * 1000.0
    ffmpeg = STATE.get("ffmpeg")
    if not videos or not cache:
        from vg.diagnostics import perf as diagnostic_perf

        diagnostic_perf(
            "fill_thumbs_for_videos",
            (time.perf_counter() - started) * 1000.0,
            videos=len(videos or []),
            skipped=True,
            reason="no_videos_or_cache",
        )
        return 0, 0
    if not ffmpeg:
        STATE["thumb_progress"] = "未找到 ffmpeg，已跳过预览图（安装后重启可生成）"
        log("[预览图] 未找到 ffmpeg，已跳过")
        from vg.diagnostics import perf as diagnostic_perf

        diagnostic_perf(
            "fill_thumbs_for_videos",
            (time.perf_counter() - started) * 1000.0,
            videos=len(videos),
            skipped=True,
            reason="no_ffmpeg",
        )
        return 0, 0
    stage_started = time.perf_counter()
    candidates = missing_thumb_items(videos, cache)
    missing_scan_ms = (time.perf_counter() - stage_started) * 1000.0
    stage_started = time.perf_counter()
    missing = [snapshot for item in candidates
               if (snapshot := media_job_snapshot(item, _video_file_for_thumb(item))) is not None]
    source_stat_ms = (time.perf_counter() - stage_started) * 1000.0
    # Never schedule known audio-only/corrupt files.  They are valid catalog
    # entries, but a thumbnail is impossible and ffmpeg would just fail.
    skipped_no_video = [
        item for item in missing
        if item.get("bad") and (
            "无视频流" in str(item.get("bad_reason") or "").casefold()
            or "no video" in str(item.get("bad_reason") or "").casefold()
        )
    ]
    failure_marked = 0
    for item in skipped_no_video:
        if not item.get("thumb_failed"):
            mark_thumbnail_failure(item, reason=item.get("bad_reason") or "no_video_stream")
            failure_marked += 1
        item["has_thumb"] = False
        item["thumb_v"] = 0
    if skipped_no_video:
        missing = [item for item in missing if item not in skipped_no_video]
    missing_before_reuse = len(missing)
    local_hits = len(videos) - missing_before_reuse
    missing_sample_ids = "|".join(
        str(item.get("id") or "") for item in missing[:5]
    )
    cache_vgt_files: int | None = None
    if missing_before_reuse >= 50:
        # A whole-library miss is suspicious (wrong cache path, cleanup race,
        # or an ID migration). Capture enough state to identify which one on
        # the next occurrence without logging every thumbnail.
        cache_vgt_files = len(list_thumb_ids(cache))
    stage_started = time.perf_counter()
    missing, reused = adopt_thumbs_from_caches(missing, cache)
    reuse_ms = (time.perf_counter() - stage_started) * 1000.0
    if missing_before_reuse >= 50:
        from vg.diagnostics import emit

        emit(
            "WARN",
            "thumbnail_cache_bulk_miss",
            force=True,
            cache=cache,
            root=root,
            videos=len(videos),
            local_hits=local_hits,
            missing_before_reuse=missing_before_reuse,
            reused_from_other_caches=reused,
            missing_after_reuse=len(missing),
            cache_vgt_files=cache_vgt_files,
            sample_ids=missing_sample_ids,
        )
    cached_n = len(videos) - len(missing) - len(skipped_no_video)
    ok_n = fail_n = 0
    generate_ms = 0.0
    if missing:
        stage_started = time.perf_counter()
        label = "新建" if burst else "补全"
        ok_n, fail_n = generate_thumbs_parallel(
            missing, cached_n=cached_n, label=label, burst=burst, cache=cache
        )
        generate_ms = (time.perf_counter() - stage_started) * 1000.0
        STATE["thumb_progress"] = (
            f"预览图完成（缓存 {cached_n} + 新建 {ok_n}，失败 {fail_n}）"
        )
        log(f"[预览图] 完成：成功 {ok_n}，失败 {fail_n}，已有 {cached_n}（复用 {reused}）")
    else:
        STATE["thumb_progress"] = f"预览图全部来自缓存（{cached_n} 个），无需重建"
        log(f"[预览图] 全部命中缓存（{cached_n}），无需重建")
    stage_started = time.perf_counter()
    if failure_marked:
        from vg.disk_libs import patch_library_media_items
        from vg.schema import THUMB_RESULT_FIELDS

        patch_library_media_items(skipped_no_video, fields=THUMB_RESULT_FIELDS)
    patch_ms = (time.perf_counter() - stage_started) * 1000.0
    from vg.diagnostics import perf as diagnostic_perf

    total_ms = (time.perf_counter() - started) * 1000.0
    diagnostic_perf(
        "fill_thumbs_for_videos",
        total_ms,
        force=bool(fail_n or skipped_no_video),
        root=root,
        cache=cache,
        snapshot_ms=round(snapshot_ms, 1),
        missing_scan_ms=round(missing_scan_ms, 1),
        source_stat_ms=round(source_stat_ms, 1),
        reuse_ms=round(reuse_ms, 1),
        generate_ms=round(generate_ms, 1),
        patch_ms=round(patch_ms, 1),
        other_ms=round(max(0.0, total_ms - snapshot_ms - missing_scan_ms - source_stat_ms - reuse_ms - generate_ms - patch_ms), 1),
        videos=len(videos),
        missing_before_reuse=missing_before_reuse,
        skipped_no_video=len(skipped_no_video),
        reused=reused,
        remaining=len(missing),
        cached=cached_n,
        ok=ok_n,
        failed=fail_n,
        burst=burst,
    )
    return ok_n, fail_n


def _load_index_videos(cache: Path, root: Path) -> list[dict]:
    from vg.catalog_db import catalog_exists, load_catalog_videos, read_catalog_root

    if not catalog_exists(cache):
        return []
    stored_root = read_catalog_root(cache)
    if stored_root and not _same_root(stored_root, root):
        return []
    try:
        return [dict(v) for v in load_catalog_videos(cache, root) if isinstance(v, dict)]
    except Exception as e:
        log(f"[增量] 读取旧索引失败: {e}")
        return []


def _catalog_row_count(cache: Path) -> int:
    """Cheap videos-table COUNT(*) for empty-catalog detection."""
    from vg.catalog_db import catalog_db_path, catalog_exists

    if not catalog_exists(cache):
        return 0
    path = catalog_db_path(cache)
    if path is None:
        return 0
    try:
        import sqlite3

        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            row = conn.execute("SELECT COUNT(*) FROM videos").fetchone()
            return int(row[0] or 0) if row else 0
        finally:
            conn.close()
    except Exception:
        return 0


def _final_scan_change_counts(
    videos: list[dict],
    old_by_id: dict[str, dict],
    *,
    incremental: bool,
) -> tuple[int, int]:
    """Return final-output (reused, changed) counts after TS/HLS collapsing."""
    if not incremental:
        return 0, len(videos)

    changed = 0
    for item in videos:
        vid = str(item.get("id") or "").strip()
        previous = old_by_id.get(vid) if vid else None
        if previous is None:
            changed += 1
            continue
        try:
            size_changed = int(item.get("size") or 0) != int(previous.get("size") or 0)
        except (TypeError, ValueError):
            size_changed = item.get("size") != previous.get("size")
        try:
            mtime_changed = float(item.get("mtime") or 0) != float(previous.get("mtime") or 0)
        except (TypeError, ValueError):
            mtime_changed = item.get("mtime") != previous.get("mtime")
        previous_fingerprint = str(previous.get("file_sig") or "")
        fingerprint_changed = bool(
            previous_fingerprint
            and str(item.get("file_sig") or "") != previous_fingerprint
        )
        if size_changed or mtime_changed or fingerprint_changed:
            changed += 1
    return len(videos) - changed, changed


def scan_videos(
    root: Path,
    do_thumbs: bool = True,
    incremental: bool = True,
    quiet: bool = False,
    only_folders: set[str] | None = None,
    folder_counts: dict[str, int] | None = None,
    burst_thumbs: bool | None = None,
) -> None:
    scan_started = time.perf_counter()
    from vg.diagnostics import call as diagnostic_call

    diagnostic_call(
        "scan_videos",
        root=root,
        incremental=incremental,
        do_thumbs=do_thumbs,
        quiet=quiet,
    )
    if not quiet:
        STATE["scanning"] = True
    STATE["scan_progress"] = "正在增量扫描…" if incremental else "正在扫描…"
    if not quiet:
        STATE["thumb_progress"] = ""
    ffmpeg = STATE["ffmpeg"]
    # Always bind cache to *this* scan root. Reusing STATE["cache_dir"] from a
    # previous disk on the same volume overwrites the other root's catalog
    # (two D:\ folders hashed apart, but the second scan wrote into the first).
    cache = ensure_cache_dir(root)
    STATE["cache_dir"] = cache
    try:
        root_s = str(Path(root).resolve())
    except OSError:
        root_s = str(root)
    STATE["scan_root"] = root_s
    STATE["scan_live"] = []
    mode = "增量" if incremental else "全量"
    log(f"[扫描] {mode}开始: {root}" + ("（后台）" if quiet else ""))
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        cache = ensure_cache_dir(root)
        STATE["cache_dir"] = cache

    t0 = time.perf_counter()
    old_map = _load_old_video_map(cache, root) if incremental else {}
    _load_map_ms = (time.perf_counter() - t0) * 1000.0
    log(f"[计时] _load_old_video_map: {_load_map_ms:.1f}ms, 条目={len(old_map)}")
    previous_catalog_n = len(old_map)
    if previous_catalog_n <= 0:
        try:
            previous_catalog_n = len(_load_index_videos(cache, root))
        except Exception:
            previous_catalog_n = 0
    previous_catalog_snapshot = (
        _load_index_videos(cache, root) if previous_catalog_n > 0 else []
    )
    old_by_id: dict[str, dict] = {}
    if old_map:
        log(f"[增量] 可复用旧条目 {len(old_map)} 个")
        for item in old_map.values():
            vid = (item.get("id") or "").strip()
            if vid:
                old_by_id[vid] = item
    # Also keep ts_set / m3u8 rows skipped by load_catalog_by_rel.
    if incremental:
        try:
            from vg.catalog_db import catalog_exists, load_catalog_videos

            if catalog_exists(cache):
                # Use the catalog repository's guarded connection instead of
                # opening SQLite directly while another scan may be flushing
                # the same cache.  Direct reads could observe a half-created
                # schema and hide the error in a broad ``except``.
                for item in load_catalog_videos(cache, root):
                    if not isinstance(item, dict):
                        continue
                    if item.get("kind") not in {"ts_set", "m3u8"}:
                        continue
                    vid = (item.get("id") or "").strip()
                    if vid and vid not in old_by_id:
                        old_by_id[vid] = item
        except Exception as exc:
            from vg.diagnostics import error

            error("scan_catalog_special_rows_failed", exc, cache=cache, root=root)
    if burst_thumbs is None:
        burst_thumbs = not quiet

    target_folders = {folder_key(k) for k in only_folders} if only_folders is not None else None
    walk_counts: dict[str, int] = (
        {folder_key(k): int(v) for k, v in folder_counts.items() if int(v) > 0}
        if folder_counts is not None
        else {}
    )
    accumulate_counts = folder_counts is None

    errors: list[str] = []
    found: list[dict] = []
    reused = added = 0
    walk_lock = threading.RLock()
    walk_workers = 1
    last_progress_ts = 0.0
    last_tree_n = 0
    scan_counts: dict[str, int] = {}
    scan_samples: dict[str, list[str]] = {}
    # --- 计时：各阶段累计耗时 ---
    _cum_fp_ms = 0.0      # 指纹计算累计
    _cum_publish_ms = 0.0  # _publish_live 累计
    _cum_store_live_ms = 0.0  # store_live_library 累计
    _n_fp_calls = 0         # 指纹调用次数

    def count_scan(reason: str, path: Path | str | None = None, detail: str = "") -> None:
        with walk_lock:
            scan_counts[reason] = scan_counts.get(reason, 0) + 1
            if path is not None and len(scan_samples.setdefault(reason, [])) < 3:
                sample = str(path)
                if detail:
                    sample += f" ({detail})"
                scan_samples[reason].append(sample)

    def _publish_live(force_tree: bool = False) -> None:
        """Push mid-scan catalog without wiping other disks from STATE."""
        nonlocal last_progress_ts, last_tree_n, _cum_publish_ms, _cum_store_live_ms
        import time as _time

        _pub_t0 = _time.perf_counter()
        now = _time.time()
        # Items are stamped as they are found; just expose the live list.
        STATE["scan_live"] = found
        STATE["scan_progress"] = f"已发现 {len(found)} 个视频…"
        STATE["lib_gen"] = int(STATE.get("lib_gen") or 0) + 1
        # Sync disk_libs / tree infrequently — scan_live already covers API reads.
        if force_tree or (len(found) - last_tree_n >= 400) or (now - last_progress_ts >= 3.0):
            _store_t0 = _time.perf_counter()
            try:
                store_live_library(root_s, found)
            except Exception as e:
                log(f"[扫描] 实时入库失败: {e}")
            _cum_store_live_ms += (_time.perf_counter() - _store_t0) * 1000.0
            # Keep STATE["videos"] in sync so videos_for_scope() returns the
            # latest snapshot instead of a stale pre-scan list.  Merge with
            # videos from other (non-scanning) roots to preserve multi-root data.
            try:
                from vg.roots import _root_compare_key, get_mounted_roots

                scan_key = _root_compare_key(root_s)
                other = [
                    v for v in (STATE.get("videos") or [])
                    if _root_compare_key(v.get("_lib_root") or v.get("root") or "") != scan_key
                ]
                STATE["videos"] = list(found) + other
            except Exception as _exc:
                log(f"[扫描] STATE[videos] 同步失败（仅保留当前根）: {_exc}")
                STATE["videos"] = list(found)
            try:
                from vg.diagnostics import catalog_plane_snapshot

                catalog_plane_snapshot(
                    "scan_publish_live",
                    found=len(found),
                    force_tree=bool(force_tree),
                )
            except Exception:
                pass
            if not quiet:
                try:
                    from vg.roots import get_mounted_roots, tree_for_scope

                    if len(get_mounted_roots()) > 1:
                        STATE["tree"] = tree_for_scope(None)
                    else:
                        STATE["tree"] = build_tree(root, found)
                except Exception as _exc:
                    log(f"[扫描] tree_for_scope 失败，回退 build_tree: {_exc}")
                    try:
                        STATE["tree"] = build_tree(root, found)
                    except Exception as _exc2:
                        log(f"[扫描] build_tree 也失败，目录树将为空: {_exc2}")
                        STATE["tree"] = {"name": root.name or str(root), "path": "", "count": 0, "children": [], "videos": []}
            last_tree_n = len(found)
            last_progress_ts = now
        _cum_publish_ms += (_time.perf_counter() - _pub_t0) * 1000.0

    def on_walk_error(err: OSError) -> None:
        count_scan("directory_error", detail=str(err))
        with walk_lock:
            try:
                err_path = Path(getattr(err, "filename", None) or ".")
                rel_parts = err_path.resolve().relative_to(root.resolve()).parts
                top = rel_parts[0] if rel_parts else "(root)"
                top_level_stats.setdefault(top, {"dirs": 0, "videos": 0, "errors": 0})
                top_level_stats[top]["errors"] += 1
            except Exception:
                pass
            if len(errors) < 5:
                errors.append(str(err))
                log(f"[扫描] 跳过无权限目录: {err}")
        STATE["scan_progress"] = f"已发现 {len(found)} 个视频…（部分目录无权限已跳过）"

    def _note_top_level(dirpath: Path | str, *, video: bool = False) -> None:
        try:
            rel_parts = Path(dirpath).resolve().relative_to(root.resolve()).parts
        except Exception:
            rel_parts = ()
        top = rel_parts[0] if rel_parts else "(root)"
        with walk_lock:
            stats = top_level_stats.setdefault(top, {"dirs": 0, "videos": 0, "errors": 0})
            if video:
                stats["videos"] += 1
            else:
                stats["dirs"] += 1

    top_level_stats: dict[str, dict[str, int]] = {}
    top_kept: list[str] = []
    top_skipped: list[str] = []
    try:
        top_names = sorted(os.listdir(root))
        top_kept = [n for n in top_names if (root / n).is_dir() and not should_skip_dir(n)]
        top_skipped = [n for n in top_names if (root / n).is_dir() and should_skip_dir(n)]
        from vg.diagnostics import emit as _diag_emit

        _diag_emit(
            "INFO",
            "scan_walk_root_listing",
            force=True,
            root=root,
            top_dirs=len(top_kept),
            top_skipped=len(top_skipped),
            kept_sample=",".join(top_kept[:12]),
            skipped_sample=",".join(top_skipped[:12]),
            previous_catalog_n=previous_catalog_n,
        )
    except Exception as exc:
        from vg.diagnostics import emit as _diag_emit

        _diag_emit(
            "WARN",
            "scan_walk_root_listing_failed",
            force=True,
            root=root,
            error=str(exc),
        )

    def ingest_file(full: Path, name: str, ext: str, *, count_folder: bool) -> dict | None:
        nonlocal reused, added, _cum_fp_ms, _n_fp_calls
        try:
            rel = safe_rel(full, root)
            st = full.stat()
        except (ValueError, OSError) as exc:
            count_scan("file_stat_failed", full, str(exc))
            return None
        folder = folder_key(str(Path(rel).parent) if Path(rel).parent != Path(".") else "")
        with walk_lock:
            if count_folder and accumulate_counts:
                walk_counts[folder] = walk_counts.get(folder, 0) + 1
        if is_too_small_video(ext, st.st_size):
            count_scan("video_too_small", full, f"ext={ext} size={st.st_size}")
            return None
        # Reject non-MPEG .ts (e.g. TypeScript) before ffmpeg ever sees them.
        if ext in SEGMENT_EXTS:
            try:
                with full.open("rb") as stream:
                    head = stream.read(1)
                if head != b"\x47":
                    count_scan(
                        "ts_sync_byte_invalid",
                        full,
                        f"first_byte={head.hex() if head else 'empty'} size={st.st_size}",
                    )
                    return None
            except OSError as exc:
                count_scan("ts_header_read_failed", full, str(exc))
                return None
        old = old_map.get(rel)
        metadata_match = bool(
            old
            and int(old.get("size") or -1) == st.st_size
            and float(old.get("mtime") or 0) == st.st_mtime
        )
        old_sig = str(old.get("file_sig") or "") if old else ""
        # Content samples are deferred to the final duplicate stage.  The
        # regular scan only uses size + mtime to reuse catalog rows.
        signature_match = True
        current_sig = old_sig if metadata_match else ""
        if metadata_match and signature_match:
            item = dict(old)
            item["id"] = item.get("id") or video_id(rel)
            item["size"] = st.st_size
            item["size_h"] = format_size(st.st_size)
            item["mtime"] = st.st_mtime
            item["mtime_h"] = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
            item["file_sig"] = current_sig or old_sig
            item["thumb"] = f"{item['id']}{THUMB_EXT}"
            item["ext"] = ext
            if ext in PLAYLIST_EXTS:
                item["kind"] = "m3u8"
            ensure_video_genres(item)
            with walk_lock:
                reused += 1
        else:
            if old is None:
                count_scan("new_video", full)
            elif int(old.get("size") or -1) != st.st_size:
                count_scan(
                    "video_size_changed",
                    full,
                    f"old={old.get('size')} new={st.st_size}",
                )
            elif float(old.get("mtime") or 0) != st.st_mtime:
                count_scan(
                    "video_mtime_changed",
                    full,
                    f"old={old.get('mtime')} new={st.st_mtime}",
                )
            elif not signature_match:
                count_scan("video_fingerprint_changed", full)
            vid = video_id(rel)
            # New or changed files get a signature only if the final
            # duplicate stage proves that they share an exact size.
            file_sig = ""
            item = {
                "id": vid,
                "name": full.stem,
                "filename": name,
                "rel": rel,
                "folder": folder,
                "ext": ext,
                "size": st.st_size,
                "size_h": format_size(st.st_size),
                "mtime": st.st_mtime,
                "mtime_h": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "file_sig": file_sig,
                "duration": None,
                "duration_h": "",
                "thumb": f"{vid}{THUMB_EXT}",
                "has_thumb": False,
            }
            if ext in PLAYLIST_EXTS:
                item["kind"] = "m3u8"
            with walk_lock:
                added += 1
        item["root"] = root_s
        item["_lib_root"] = root_s
        ensure_video_taxonomy(item)
        ensure_video_genres(item)
        if cache:
            item["_lib_cache"] = str(cache)
        item["_folder_raw"] = folder
        return item

    def note_found() -> None:
        n = len(found)
        # Incremental: the UI already has the cached library. Publishing every
        # 100 files rewrites disk_libs and dominates a no-op rescan. Progress
        # text only; one publish happens after the walk.
        if incremental:
            if n == 25 or n % 500 == 0:
                STATE["scan_progress"] = f"已发现 {n} 个视频…"
            if n % 500 == 0:
                log(
                    f"[扫描] 已发现 {n} 个…"
                    f"（候选复用 {reused} / 候选需重建 {added}）"
                )
            return
        if n % 100 == 0:
            _publish_live(force_tree=(n % 500 == 0))
            if n % 200 == 0:
                log(
                    f"[扫描] 已发现 {n} 个…"
                    f"（候选复用 {reused} / 候选需重建 {added}）"
                )
        elif n == 25:
            _publish_live(force_tree=True)

    scanned: list[dict] = []
    dir_snap = None
    usn_hit = False
    if target_folders is not None:
        prior_index = _load_index_videos(cache, root)
        kept = [
            v
            for v in prior_index
            if not _folder_hit_by_targets(item_folder_key(v), target_folders)
        ]
        log(
            f"[增量] 定向扫描 {len(target_folders)} 个目录："
            f"保留未变 {len(kept)} / 原目录 {len(prior_index)}，"
            f"目标例={','.join(sorted(target_folders)[:6])}"
        )
        for folder in sorted(target_folders):
            dirpath = root / folder if folder else root
            try:
                if not dirpath.is_dir():
                    count_scan("target_folder_missing", dirpath)
                    continue
            except OSError as err:
                on_walk_error(err if isinstance(err, OSError) else OSError(err))
                continue
            # Recursively walk the changed folder so nested videos are rebuilt
            # after we dropped all descendants from ``kept``.
            for walk_dir, dirnames, filenames in os.walk(dirpath, onerror=on_walk_error):
                count_scan("directories_scanned")
                kept_dirs: list[str] = []
                for name in dirnames:
                    if should_skip_dir(name):
                        continue
                    child = Path(walk_dir) / name
                    if _is_linkish_dir(child):
                        continue
                    kept_dirs.append(name)
                dirnames[:] = kept_dirs
                for name in filenames:
                    ext = Path(name).suffix.lower()
                    if ext not in VIDEO_EXTS and ext not in PLAYLIST_EXTS:
                        continue
                    count_scan("candidate_files")
                    full = Path(walk_dir) / name
                    item = ingest_file(full, name, ext, count_folder=False)
                    if item:
                        scanned.append(item)
                        found.append(item)
                        note_found()
        scanned = collapse_segment_sets(scanned)
        found = kept + scanned
    else:
        from vg.dir_snapshot import DirSnapshot

        dir_snap = DirSnapshot.load(cache)
        usn_hit = bool(
            incremental
            and previous_catalog_snapshot
            and dir_snap.volume_usn_unchanged(root)
        )
        if usn_hit:
            found = [dict(v) for v in previous_catalog_snapshot]
            log(
                f"[扫描] 卷 USN 未变化，跳过目录遍历，沿用目录 {len(found)} 条"
            )
            from vg.diagnostics import emit as _usn_emit

            _usn_emit(
                "INFO",
                "scan_usn_unchanged_skip_walk",
                force=True,
                root=root,
                videos=len(found),
            )
        walk_heartbeat_lock = threading.Lock()
        walk_last_heartbeat = time.perf_counter()

        def _plan_dir(dirpath: Path):
            if not incremental or usn_hit:
                return None
            reused_dir = dir_snap.try_reuse(root, dirpath)
            if reused_dir is None:
                return None
            files, kids = reused_dir
            count_scan("dir_snapshot_hit")
            return files, [Path(dirpath) / kid for kid in kids]

        def _record_dir(dirpath: Path, filenames: list[str], child_dirs: list[Path]) -> None:
            dir_snap.observe(root, dirpath, list(filenames), child_dirs)

        def _walk_tree(start: Path, *, children: list[str] | None = None) -> None:
            nonlocal walk_last_heartbeat
            start_resolved = Path(start).resolve()
            local_dirs = 0
            for dirpath, dirnames, filenames in os.walk(start, onerror=on_walk_error):
                count_scan("directories_scanned")
                local_dirs += 1
                _note_top_level(dirpath)
                kept: list[str] = []
                for name in dirnames:
                    if should_skip_dir(name):
                        continue
                    child = Path(dirpath) / name
                    if _is_linkish_dir(child):
                        continue
                    kept.append(name)
                dirnames[:] = kept
                _record_dir(Path(dirpath), list(filenames), [Path(dirpath) / name for name in dirnames])
                if children is not None:
                    try:
                        at_start = Path(dirpath).resolve() == start_resolved
                    except OSError:
                        at_start = False
                    if at_start:
                        allowed = set(children)
                        dirnames[:] = [d for d in dirnames if d in allowed]
                for name in filenames:
                    ext = Path(name).suffix.lower()
                    if ext not in VIDEO_EXTS and ext not in PLAYLIST_EXTS:
                        continue
                    count_scan("candidate_files")
                    full = Path(dirpath) / name
                    item = ingest_file(full, name, ext, count_folder=True)
                    if item:
                        _note_top_level(dirpath, video=True)
                        with walk_lock:
                            found.append(item)
                            note_found()
                if local_dirs % 256 == 0:
                    now = time.perf_counter()
                    with walk_heartbeat_lock:
                        if now - walk_last_heartbeat >= 3.0:
                            walk_last_heartbeat = now
                            dirs_so_far = int(scan_counts.get("directories_scanned", 0) or 0)
                            found_n = len(found)
                            STATE["scan_progress"] = (
                                f"遍历中… 已扫 {dirs_so_far} 目录，发现 {found_n} 个"
                            )
                            from vg.diagnostics import emit as _hb_emit

                            _hb_emit(
                                "INFO",
                                "scan_walk_heartbeat",
                                force=True,
                                root=root,
                                directories=dirs_so_far,
                                found=found_n,
                                workers=walk_workers,
                                active_subtree=str(start),
                                elapsed_ms=f"{(now - scan_started) * 1000.0:.0f}",
                            )

        cpus = max(1, os.cpu_count() or 4)
        foreground_workers = max(1, cpus - 2)
        target_jobs = min(SCAN_WALK_JOB_CAP, max(foreground_workers, 8))
        if usn_hit or incremental:
            shallow_jobs, leaf_jobs = [], []
        else:
            shallow_jobs, leaf_jobs = expand_scan_walk_jobs(
                root,
                top_kept,
                target_jobs=target_jobs,
            )
        walk_workers = directory_walk_workers(
            cpus,
            top_dirs=len(top_kept),
            background=quiet and incremental,
        )
        walk_mode = "serial"
        if walk_workers > 1:
            walk_mode = "steal_expanded" if shallow_jobs else "steal"
        steal_n = 0
        from vg.diagnostics import emit as _diag_emit

        _diag_emit(
            "INFO",
            "scan_walk_parallel",
            force=True,
            root=root,
            workers=walk_workers,
            top_dirs=len(top_kept),
            jobs=len(leaf_jobs),
            split_parents=",".join(name for name, _path in shallow_jobs[:12]),
            job_sample=",".join(name for name, _path in leaf_jobs[:12]),
            cpus=cpus,
            mode=walk_mode,
        )
        log(
            f"[扫描] 目录遍历 {walk_workers} 线程"
            f"（顶层 {len(top_kept)} → 任务 {len(leaf_jobs)}"
            f"{'，拆分 ' + ','.join(n for n, _p in shallow_jobs[:4]) if shallow_jobs else ''}"
            f"，逻辑核 {cpus}）"
        )

        def _scan_visit(dirpath: Path, filenames: list[str]) -> None:
            count_scan("directories_scanned")
            _note_top_level(dirpath)
            for name in filenames:
                ext = Path(name).suffix.lower()
                if ext not in VIDEO_EXTS and ext not in PLAYLIST_EXTS:
                    continue
                count_scan("candidate_files")
                full = Path(dirpath) / name
                item = ingest_file(full, name, ext, count_folder=True)
                if item:
                    _note_top_level(dirpath, video=True)
                    with walk_lock:
                        found.append(item)
                        note_found()

        def _scan_heartbeat(active: Path) -> None:
            nonlocal walk_last_heartbeat
            now = time.perf_counter()
            with walk_heartbeat_lock:
                if now - walk_last_heartbeat < 3.0:
                    return
                walk_last_heartbeat = now
                dirs_so_far = int(scan_counts.get("directories_scanned", 0) or 0)
                found_n = len(found)
                STATE["scan_progress"] = (
                    f"遍历中… 已扫 {dirs_so_far} 目录，发现 {found_n} 个"
                )
            from vg.diagnostics import emit as _hb_emit

            _hb_emit(
                "INFO",
                "scan_walk_heartbeat",
                force=True,
                root=root,
                directories=dirs_so_far,
                found=found_n,
                workers=walk_workers,
                active_subtree=str(active),
                elapsed_ms=f"{(now - scan_started) * 1000.0:.0f}",
            )

        last_steal_log = [0.0]

        def _scan_steal(parent: Path, n: int) -> None:
            now = time.perf_counter()
            if now - last_steal_log[0] < 3.0:
                return
            last_steal_log[0] = now
            from vg.diagnostics import emit as _steal_emit

            _steal_emit(
                "INFO",
                "scan_walk_steal",
                force=True,
                root=root,
                parent=str(parent),
                stolen=n,
                queued_hint=n,
                workers=walk_workers,
            )

        if usn_hit:
            pass
        elif incremental:
            pool_started = time.perf_counter()
            from vg.diagnostics import emit as _pool_emit

            _pool_emit(
                "INFO",
                "scan_walk_pool_start",
                force=True,
                root=root,
                workers=walk_workers,
                jobs=1,
                thread=threading.current_thread().name,
                mode="dir_snapshot",
            )
            steal_n = _run_stolen_directory_walk(
                [root],
                workers=walk_workers,
                visit=_scan_visit,
                on_walk_error=on_walk_error,
                on_heartbeat=_scan_heartbeat,
                on_steal=_scan_steal,
                on_plan=_plan_dir,
                on_observed=_record_dir,
            )
            _pool_emit(
                "INFO",
                "scan_walk_pool_done",
                force=True,
                root=root,
                workers=walk_workers,
                jobs=1,
                steals=steal_n,
                elapsed_ms=f"{(time.perf_counter() - pool_started) * 1000.0:.0f}",
                directories=int(scan_counts.get("directories_scanned", 0) or 0),
                snapshot_hits=int(scan_counts.get("dir_snapshot_hit", 0) or 0),
                found=len(found),
            )
        elif walk_workers <= 1:
            _walk_tree(root)
        else:
            _walk_tree(root, children=[])
            for _label, parent in shallow_jobs:
                _walk_tree(parent, children=[])
            pool_started = time.perf_counter()
            from vg.diagnostics import emit as _pool_emit

            _pool_emit(
                "INFO",
                "scan_walk_pool_start",
                force=True,
                root=root,
                workers=walk_workers,
                jobs=len(leaf_jobs),
                thread=threading.current_thread().name,
            )
            steal_n = _run_stolen_directory_walk(
                [path for _label, path in leaf_jobs],
                workers=walk_workers,
                visit=_scan_visit,
                on_walk_error=on_walk_error,
                on_heartbeat=_scan_heartbeat,
                on_steal=_scan_steal,
                on_observed=_record_dir,
            )
            _pool_emit(
                "INFO",
                "scan_walk_pool_done",
                force=True,
                root=root,
                workers=walk_workers,
                jobs=len(leaf_jobs),
                steals=steal_n,
                elapsed_ms=f"{(time.perf_counter() - pool_started) * 1000.0:.0f}",
                directories=int(scan_counts.get("directories_scanned", 0) or 0),
                found=len(found),
            )
        if not usn_hit:
            found = collapse_segment_sets(found)

    # --- 计时：主循环结束 ---
    _walk_end_ms = (time.perf_counter() - scan_started) * 1000.0
    log(f"[计时] 主遍历完成: 总耗时={_walk_end_ms:.0f}ms, 目录={scan_counts.get('directories_scanned',0)}, "
        f"候选文件={scan_counts.get('candidate_files',0)}, 线程={walk_workers}, 指纹调用={_n_fp_calls}, "
        f"指纹累计={_cum_fp_ms:.0f}ms, publish累计={_cum_publish_ms:.0f}ms, "
        f"store_live累计={_cum_store_live_ms:.0f}ms, 找到={len(found)}")
    try:
        from vg.diagnostics import emit as _diag_emit

        top_summary = " | ".join(
            f"{name}:dirs={stats.get('dirs', 0)},videos={stats.get('videos', 0)},errors={stats.get('errors', 0)}"
            for name, stats in sorted(top_level_stats.items(), key=lambda kv: (-kv[1].get("videos", 0), kv[0].lower()))[:16]
        )
        _diag_emit(
            "INFO",
            "scan_walk_top_level_summary",
            force=True,
            root=root,
            directories=scan_counts.get("directories_scanned", 0),
            candidates=scan_counts.get("candidate_files", 0),
            found=len(found),
            previous_catalog_n=previous_catalog_n,
            top_keys=len(top_level_stats),
            workers=walk_workers,
            summary=top_summary,
        )
        dirs_scanned = int(scan_counts.get("directories_scanned", 0) or 0)
        try:
            is_drive_root = len(Path(root).resolve().parts) <= 1
        except Exception:
            is_drive_root = False
        if (
            target_folders is None
            and is_drive_root
            and dirs_scanned < 200
            and previous_catalog_n >= 10
            and len(found) < max(3, int(previous_catalog_n * 0.25))
        ):
            _diag_emit(
                "ERROR",
                "scan_walk_suspiciously_shallow",
                force=True,
                root=root,
                directories=dirs_scanned,
                found=len(found),
                previous_catalog_n=previous_catalog_n,
                summary=top_summary,
            )
    except Exception:
        pass

    if old_by_id:
        adopted = 0
        for item in found:
            vid = (item.get("id") or "").strip()
            prev = old_by_id.get(vid)
            if not prev:
                continue
            changed_meta = _inherit_probe_fields_if_same_file(item, prev)
            if prev.get("has_thumb") and not item.get("has_thumb") and _probe_cache_still_valid(prev, item):
                item["has_thumb"] = True
                item["thumb"] = prev.get("thumb") or item.get("thumb")
                item["thumb_v"] = prev.get("thumb_v") or item.get("thumb_v") or 1
                changed_meta = True
            if changed_meta:
                adopted += 1
        if adopted:
            log(f"[增量] 合集/条目继承旧元数据 {adopted} 个")

    # Safety net: drop rows whose files vanished (folder-key mismatches, etc.).
    if not usn_hit:
        found = prune_missing_catalog_items(found, root, log_tag="扫描")

    output_reused, output_changed = _final_scan_change_counts(
        found,
        old_by_id,
        incremental=incremental,
    )
    catalog_unchanged = bool(
        incremental
        and not errors
        and target_folders is None
        and previous_catalog_n > 0
        and len(found) == previous_catalog_n
        and output_changed == 0
    )

    def _memory_matches_found() -> bool:
        if not found:
            return False
        root_key = root_s.casefold().rstrip("\\/")
        n = 0
        for video in STATE.get("videos") or []:
            tagged = str(video.get("_lib_root") or video.get("root") or "")
            tagged = tagged.rstrip("\\/").casefold()
            if tagged == root_key:
                n += 1
        return n == len(found)

    skip_republish = catalog_unchanged and _memory_matches_found()
    if dir_snap is not None and getattr(dir_snap, "dirty", False) and not errors:
        dir_snap.save(cache, root)
        log(
            f"[扫描] 目录快照已更新  dirs={len(dir_snap.dirs)}  hits={dir_snap.hits}"
        )
    elif dir_snap is not None and dir_snap.hits:
        log(f"[扫描] 目录快照命中 {dir_snap.hits} 个目录，未改写快照")

    _post_t0 = time.perf_counter()
    found.sort(key=lambda x: (x.get("rel") or "").lower())
    _sort_ms = (time.perf_counter() - _post_t0) * 1000.0

    _stamp_t0 = time.perf_counter()
    stamp_lib_meta(found, root=root_s, cache=cache, overwrite=True)
    _stamp_ms = (time.perf_counter() - _stamp_t0) * 1000.0
    for v in found:
        v["root"] = root_s
        if "_folder_raw" not in v:
            v["_folder_raw"] = (v.get("folder") or "").replace("\\", "/").strip("/")

    from vg.diagnostics import emit

    emit(
        "INFO",
        "file_sig_backfill_deferred",
        force=True,
        source="scan_complete",
        root=root_s,
        videos=len(found),
        reason="deferred_until_duplicate_final_stage",
        full_hash_disabled=True,
    )
    STATE["scan_live"] = found

    _store_final_ms = 0.0
    _tree_ms = 0.0
    if skip_republish:
        log("[扫描] 目录与内存片库一致，跳过实时发布与统一合并")
    else:
        _store_t0 = time.perf_counter()
        try:
            store_live_library(root_s, found)
        except Exception as _exc:
            log(f"[扫描] 最终 store_live_library 失败: {_exc}")
        _store_final_ms = (time.perf_counter() - _store_t0) * 1000.0

        _tree_t0 = time.perf_counter()
        try:
            from vg.roots import get_mounted_roots, tree_for_scope

            if len(get_mounted_roots()) > 1:
                STATE["tree"] = tree_for_scope(None)
            else:
                STATE["tree"] = build_tree(root, found)
        except Exception as _exc:
            log(f"[扫描] tree_for_scope(最终) 失败，回退 build_tree: {_exc}")
            STATE["tree"] = build_tree(root, found)
        _tree_ms = (time.perf_counter() - _tree_t0) * 1000.0
    log(f"[计时] 后处理: sort={_sort_ms:.1f}ms, stamp_lib_meta={_stamp_ms:.1f}ms, "
        f"store_live={_store_final_ms:.1f}ms, build_tree={_tree_ms:.1f}ms")

    # Never replace the whole multi-disk STATE with one disk mid-flight.
    multi = False
    try:
        from vg.roots import get_mounted_roots

        multi = len(get_mounted_roots()) > 1
    except Exception as _exc:
        log(f"[扫描] 检测多根失败，按单根处理: {_exc}")
        multi = False

    if skip_republish:
        pass
    elif multi:
        # Leave other disks intact; unified publish happens after save_index.
        pass
    else:
        _rebuild_t0 = time.perf_counter()
        try:
            rebuild_indexes(found)
        except Exception as _exc:
            log(f"[扫描] rebuild_indexes 失败，STATE 可能不一致: {_exc}")
        _rebuild_ms = (time.perf_counter() - _rebuild_t0) * 1000.0
        log(f"[计时] rebuild_indexes: {_rebuild_ms:.1f}ms")

    extra = f"（{len(errors)} 个目录跳过）" if errors else ""
    tip = (
        f"，复用 {output_reused}，新建/变更 {output_changed}"
        if incremental
        else ""
    )
    STATE["scan_progress"] = f"扫描完成，共 {len(found)} 个视频{tip}{extra}"
    log(f"[扫描] 完成，共 {len(found)} 个{tip}{extra}")
    shrink_blocked = False
    shrink_threshold = max(3, int(previous_catalog_n * 0.25)) if previous_catalog_n >= 10 else 0
    if previous_catalog_n >= 10 and len(found) < shrink_threshold and previous_catalog_snapshot:
        shrink_blocked = True
        from vg.diagnostics import emit as _diag_emit

        _diag_emit(
            "ERROR",
            "catalog_shrink_blocked",
            force=True,
            root=root,
            previous_catalog_n=previous_catalog_n,
            new_found=len(found),
            threshold=shrink_threshold,
            directories=scan_counts.get("directories_scanned", 0),
            candidates=scan_counts.get("candidate_files", 0),
            detail="refusing to overwrite catalog with a much smaller scan result",
        )
        log(
            f"[扫描] 拒绝覆盖目录库：原有 {previous_catalog_n} 条，本次仅找到 {len(found)} 条"
            f"（阈值 {shrink_threshold}）。保留旧索引。"
        )
        found = [dict(v) for v in previous_catalog_snapshot]
        STATE["scan_progress"] = (
            f"扫描结果异常偏少（原{previous_catalog_n}→本次找到偏少），已保留旧目录库"
        )
        _stored_n, _stored_counts = read_index_counts(cache)
        saved_counts = _stored_counts if isinstance(_stored_counts, dict) else {}
        saved_n = int(_stored_n or previous_catalog_n)
    else:
        saved_counts = walk_counts
        saved_n = sum(saved_counts.values())
    _save_t0 = time.perf_counter()
    if shrink_blocked:
        _save_ok = True
        log(f"[计时] save_index: skipped_shrink_guard, 视频={len(found)}, ok=True")
    elif catalog_unchanged:
        _save_ok = True
        log(f"[计时] save_index: skipped_unchanged, 视频={len(found)}, ok=True")
        emit(
            "INFO",
            "scan_catalog_write_skipped",
            force=True,
            root=root,
            videos=len(found),
            reason="no_add_delete_or_mtime_change",
        )
    else:
        _save_ok = save_index(cache, root, found, file_count=saved_n, folder_counts=saved_counts)
        _save_ms = (time.perf_counter() - _save_t0) * 1000.0
        if not _save_ok:
            log(f"[扫描] save_index 返回失败！缓存可能未持久化，下次启动将重新扫描")
        log(f"[计时] save_index: {_save_ms:.1f}ms, 视频={len(found)}, ok={_save_ok}")
    if skip_republish:
        log("[扫描] 无变化，跳过统一片库发布")
    else:
        save_prefs(last_root=str(root))
        try:
            from vg.roots import on_scan_finished

            on_scan_finished(root, found)
            log(
                f"[扫描] 目录索引已发布到网页：{root_s}，{len(found)} 个；"
                "预览图和元数据后台处理不阻塞目录浏览"
            )
        except Exception as e:
            log(f"[多根] 扫描收尾失败: {e}")
            if not multi:
                rebuild_indexes(found)

    if do_thumbs and ffmpeg and found:
        fill_thumbs_for_videos(
            found,
            burst=bool(burst_thumbs),
            cache=cache,
            root=root,
            file_count=saved_n,
            folder_counts=saved_counts,
        )
    elif not ffmpeg:
        STATE["thumb_progress"] = "未找到 ffmpeg，已跳过预览图（安装后重启可生成）"
        log("[预览图] 未找到 ffmpeg，已跳过")
        if not multi:
            STATE["tree"] = build_tree(root, found)
    elif not quiet:
        STATE["thumb_progress"] = ""
        if not multi:
            STATE["tree"] = build_tree(root, found)

    try:
        from vg.cache import cleanup_thumb_files
        from vg.catalog_db import checkpoint_catalog
        from vg.roots import thumb_id_for_item

        keep_ids = {
            thumb_id_for_item(item)
            for item in found
            if thumb_id_for_item(item)
        }
        removed, freed = cleanup_thumb_files(cache, keep_ids)
        if removed:
            log(f"[缓存] 清理预览图 {removed} 个，释放 {format_size(freed)}")
        checkpoint_catalog(cache)
    except Exception as e:
        from vg.util import log_error

        log_error("scan_cache_maintenance_failed", e, root=root)

    STATE["scan_live"] = None
    STATE["scan_root"] = ""
    if not quiet:
        STATE["scanning"] = False
    log("[扫描] 全部结束，可在浏览器浏览")
    from vg.diagnostics import emit as diagnostic_emit, perf as diagnostic_perf

    diagnostic_emit(
        "INFO",
        "scan_decision_summary",
        force=True,
        root=root_s,
        mode=mode,
        counts=json.dumps(scan_counts, ensure_ascii=False, separators=(",", ":")),
        samples=json.dumps(scan_samples, ensure_ascii=False, separators=(",", ":")),
        output_videos=len(found),
        reused=output_reused,
        changed=output_changed,
        raw_reused_candidates=reused,
        raw_changed_candidates=added,
    )

    diagnostic_perf(
        "scan_videos",
        (time.perf_counter() - scan_started) * 1000.0,
        force=True,
        mode=mode,
        videos=len(found),
        reused=output_reused,
        changed=output_changed,
        raw_reused_candidates=reused,
        raw_changed_candidates=added,
        skipped_dirs=len(errors),
        root=root_s,
    )
    _start_metadata_after_scan(root)


def load_or_scan(root: Path, do_thumbs: bool, force: bool = False, background: bool = True) -> bool:
    """加载缓存或扫描。返回 True 表示成功使用了缓存。"""
    cache = ensure_cache_dir(root)
    STATE["root"] = root
    STATE["cache_dir"] = cache
    from vg.catalog_db import catalog_exists, load_catalog_videos, read_catalog_root

    if not force and catalog_exists(cache):
        try:
            stored_root = read_catalog_root(cache)
            videos_raw = load_catalog_videos(cache, root, restore_search_cache=True)
            if (not stored_root or _same_root(stored_root, root)) and videos_raw:
                videos = []
                for raw in videos_raw:
                    if not isinstance(raw, dict):
                        continue
                    v = dict(raw)
                    rel = v.get("rel") or ""
                    vid = v.get("id") or video_id(rel)
                    v["id"] = vid
                    v["thumb"] = f"{vid}{THUMB_EXT}"
                    v["has_thumb"] = bool(v.get("has_thumb"))
                    ensure_video_genres(v)
                    videos.append(v)
                try:
                    root_s = str(Path(root).resolve())
                except OSError:
                    root_s = str(root)
                stamp_lib_meta(videos, root=root_s, cache=cache, overwrite=True)
                for v in videos:
                    v["root"] = root_s
                    if "_folder_raw" not in v:
                        v["_folder_raw"] = (v.get("folder") or "").replace("\\", "/").strip("/")
                before_n = len(videos)
                videos = prune_missing_catalog_items(videos, root, log_tag="缓存")
                if len(videos) != before_n:
                    # Persist pruned catalog so soft scan does not keep ghosts.
                    stored_n, stored_counts = read_index_counts(cache)
                    save_index(
                        cache,
                        root,
                        videos,
                        file_count=stored_n,
                        folder_counts=stored_counts if isinstance(stored_counts, dict) else None,
                    )
                    try:
                        sync_disk_lib_memory(root_s, videos)
                    except Exception as e:
                        log(f"[缓存] 同步剔除后的内存索引失败: {e}")
                from vg.diagnostics import emit

                emit(
                    "INFO",
                    "file_sig_backfill_deferred",
                    force=True,
                    source="cache_restore",
                    root=root_s,
                    videos=len(videos),
                    reason="deferred_until_duplicate_final_stage",
                    full_hash_disabled=True,
                )
                STATE["tree"] = build_tree(root, videos)
                rebuild_indexes(videos)
                STATE["scan_progress"] = f"已加载缓存，共 {len(videos)} 个视频"
                log(f"[缓存] 已加载 {len(videos)} 个视频 ← {cache}")
                save_prefs(last_root=str(root))
                try:
                    from vg.roots import on_scan_finished

                    on_scan_finished(root, videos)
                except Exception as e:
                    log(f"[多根] 缓存收尾失败: {e}")
                missing = sum(1 for v in videos if not v.get("has_thumb"))
                if missing:
                    log(f"[预览图] 索引缺图约 {missing} 个，后台按缓存补全")
                else:
                    log("[预览图] 索引标记齐全")
                start_metadata_enrichment()
                return True
        except Exception as e:
            log(f"[缓存] 加载失败，将重新扫描: {e}")

    if background:
        STATE["scanning"] = True
        STATE["scan_progress"] = "正在扫描…"
        STATE["videos"] = []
        STATE["tree"] = {"name": root.name or str(root), "path": "", "count": 0, "children": [], "videos": []}
        threading.Thread(
            target=scan_videos,
            args=(root,),
            kwargs={"do_thumbs": do_thumbs, "incremental": True, "quiet": False, "burst_thumbs": True},
            daemon=True,
        ).start()
    else:
        scan_videos(root, do_thumbs=do_thumbs, incremental=True, quiet=False, burst_thumbs=True)
    return False


def _fill_missing_thumbs(missing: list[dict]) -> None:
    cache = STATE.get("cache_dir")
    root = STATE.get("root")
    fill_thumbs_for_videos(missing, burst=False, cache=cache, root=root)


def _start_metadata_after_scan(root: Path) -> None:
    start_metadata_enrichment()


def find_video_by_id(vid: str, prefer_root: str | None = None) -> dict | None:
    """Compatibility wrapper; lookup ownership lives in catalog_repository."""
    from vg.catalog_repository import find_video_by_id as repository_lookup

    return repository_lookup(vid, prefer_root)
