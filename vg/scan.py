# -*- coding: utf-8 -*-
"""Video scanning, indexing, and thumbnail batch jobs."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from vg.cache import (
    ensure_cache_dir,
    list_thumb_ids,
    read_index_counts,
    save_index,
    thumb_cache_invalidate,
    thumb_path,
    thumb_version,
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
from vg.genres import detect_genres, ensure_video_genres
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
    clear_thumbnail_failure,
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
    make_thumbnail,
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


def expand_scan_walk_jobs(
    root: Path,
    top_kept: list[str],
    *,
    target_jobs: int,
    max_depth: int = 2,
    split_min_children: int = SCAN_WALK_SPLIT_MIN_CHILDREN,
    job_cap: int = SCAN_WALK_JOB_CAP,
) -> tuple[list[tuple[str, Path]], list[tuple[str, Path]]]:
    """Split fat trees so a C:\\Users-shaped disk can fill the worker pool.

    Top-level fan-out is useless when one child owns almost every directory.
    Expand the fattest remaining job one extra level (up to ``max_depth``)
    until there is enough work, or until a still-huge folder (``Users``,
    a user profile, ``my_home``) can be split even after the pool is full.

    Returns ``(shallow_parents, leaf_jobs)``. Shallow parents are walked
    without descending; leaves are full ``os.walk`` subtrees.
    """
    leaves: list[dict] = [
        {"label": name, "path": root / name, "depth": 0} for name in top_kept
    ]
    shallow: list[tuple[str, Path]] = []
    target_jobs = max(2, int(target_jobs or 2))
    while leaves:
        best_i = -1
        best_kids: list[str] = []
        for i, job in enumerate(leaves):
            if int(job["depth"]) >= max_depth:
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
    if (
        runtime_state.thumb_bulk_running(root)
        or runtime_state.metadata_running_for(root)
    ):
        from vg.diagnostics import emit

        emit(
            "WARN",
            "scan_start_rejected",
            force=True,
            reason="same_root_background_media_busy",
            requested_root=root,
            meta_running=runtime_state.metadata_running_for(root),
            thumb_bulk_running=runtime_state.thumb_bulk_running(root),
            thread=threading.current_thread().name,
        )
        return False, "该盘缩略图/元数据后台处理中，请稍后再重新扫描"
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
    # in-memory published snapshot when it already covers this root; the
    # background folder-count verifier still checks the disk afterwards.
    if not force and not reuse_preloaded_cache:
        root_key = str(root).rstrip("\\/").casefold()
        preloaded_count = sum(
            1
            for video in (STATE.get("videos") or [])
            if str(video.get("_lib_root") or video.get("root") or "")
            .rstrip("\\/")
            .casefold()
            == root_key
        )
        if preloaded_count or (_same_root(STATE.get("root"), root) and STATE.get("videos")):
            reuse_preloaded_cache = True
            emit(
                "INFO",
                "scan_cache_reuse_auto",
                force=True,
                root=root,
                videos=preloaded_count or len(STATE.get("videos") or []),
                reason="published_snapshot_covers_root",
                load_skipped=True,
                rebuild_skipped=True,
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
            if force:
                STATE["scan_progress"] = f"正在增量扫描 {root} …"
                STATE["thumb_progress"] = ""
                # Drop validation cooldown so a soft re-scan after a bad force
                # walk can recount folders instead of trusting a stale marker.
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
                # 多盘时不要把其它盘的片从内存清空到「整库变空」；
                # 其它盘频道仍可从各盘 index 读出；扫完 on_scan_finished 会再合并。
                try:
                    archive_current_library()
                except Exception:
                    pass
                try:
                    from vg.roots import get_mounted_roots, tree_for_scope
                    from vg.disk_libs import _norm_root_str

                    mounts = get_mounted_roots()
                    root_s = str(root.resolve())
                    if len(mounts) > 1:
                        keep = []
                        for v in list(STATE.get("videos") or []):
                            lr = (v.get("_lib_root") or "").strip()
                            if not lr:
                                continue
                            try:
                                if _norm_root_str(lr).lower() == root_s.lower():
                                    continue
                            except Exception:
                                continue
                            keep.append(v)
                        rebuild_indexes(keep)
                        try:
                            STATE["tree"] = tree_for_scope(None)
                        except Exception:
                            STATE["tree"] = {
                                "name": "全部盘",
                                "path": "",
                                "count": len(keep),
                                "children": [],
                                "videos": [],
                            }
                    else:
                        STATE["videos"] = []
                        STATE["tree"] = {
                            "name": root.name or str(root),
                            "path": "",
                            "count": 0,
                            "children": [],
                            "videos": [],
                        }
                except Exception:
                    STATE["videos"] = []
                    STATE["tree"] = {
                        "name": root.name or str(root),
                        "path": "",
                        "count": 0,
                        "children": [],
                        "videos": [],
                    }
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


def count_video_files_by_folder(
    root: Path,
    emit_diagnostics: bool = True,
) -> dict[str, int]:
    """Cheap walk: count video/playlist filenames per directory (no size/fingerprint)."""
    started = time.perf_counter()
    counts: dict[str, int] = {}
    directories = 0
    files_seen = 0

    def on_walk_error(err: OSError) -> None:
        if emit_diagnostics:
            log(f"[计数] 跳过无权限目录: {err}")

    last_heartbeat = started
    for dirpath, dirnames, filenames in os.walk(root, onerror=on_walk_error):
        directories += 1
        files_seen += len(filenames)
        dirnames[:] = [d for d in dirnames if not should_skip_dir(d)]
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
            counts[folder] = n
        # os.walk is I/O-heavy but its Python-side filename loop can still
        # monopolize the GIL on a drive root. Yield periodically so waitress
        # remains responsive while validation is in progress.
        if directories % 128 == 0:
            time.sleep(0)
            if emit_diagnostics:
                now = time.perf_counter()
                if now - last_heartbeat >= 3.0:
                    last_heartbeat = now
                    try:
                        from vg.diagnostics import emit as _count_hb

                        _count_hb(
                            "INFO",
                            "catalog_folder_count_heartbeat",
                            force=True,
                            root=root,
                            directories=directories,
                            files_seen=files_seen,
                            video_folders=len(counts),
                            video_files=sum(counts.values()),
                            elapsed_ms=f"{(now - started) * 1000.0:.0f}",
                        )
                    except Exception:
                        pass
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
        else:
            changed = changed_folder_keys(stored, live)
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


_PROBE_FIELDS_PRESERVED_BY_THUMB_SAVE = (
    "duration",
    "duration_h",
    "has_audio",
    "audio_codec",
    "audio_channels",
    "sample_rate",
    "audio_hard",
    "probe_ver",
    "probe_duration_done",
    "probe_audio_done",
    "bad",
    "bad_reason",
)


def _preserve_catalog_probe_fields_for_thumb_save(
    videos: list[dict],
    *,
    cache: Path,
    root: Path,
) -> int:
    """Keep metadata already written by ffprobe when thumbnails save the catalog."""
    if not videos or not cache or not root:
        return 0
    try:
        from vg.catalog_db import load_catalog_videos

        persisted = load_catalog_videos(cache, root)
    except Exception as exc:
        log(f"[预览图] 保存前读取目录库元数据失败: {exc}")
        return 0
    by_id = {
        str(row.get("id") or "").strip(): row
        for row in persisted
        if isinstance(row, dict) and str(row.get("id") or "").strip()
    }
    by_rel = {
        str(row.get("rel") or "").replace("\\", "/").strip("/").casefold(): row
        for row in persisted
        if isinstance(row, dict) and str(row.get("rel") or "").strip()
    }
    merged_rows = 0
    for item in videos:
        if not isinstance(item, dict):
            continue
        old = by_id.get(str(item.get("id") or "").strip())
        if old is None:
            rel = str(item.get("rel") or "").replace("\\", "/").strip("/").casefold()
            old = by_rel.get(rel)
        if old is None:
            continue
        old_sig = str(old.get("file_sig") or "").strip()
        item_sig = str(item.get("file_sig") or "").strip()
        same_file = bool(old_sig and item_sig and old_sig == item_sig)
        if not same_file:
            try:
                same_file = (
                    int(old.get("size") or -1) == int(item.get("size") or -2)
                    and abs(float(old.get("mtime") or 0) - float(item.get("mtime") or 0)) < 1.0
                )
            except (TypeError, ValueError):
                same_file = False
        if not same_file:
            continue
        copied = False
        for key in _PROBE_FIELDS_PRESERVED_BY_THUMB_SAVE:
            if key not in old:
                continue
            if key not in item or item.get(key) in (None, "", []):
                item[key] = old.get(key)
                copied = True
        if copied:
            merged_rows += 1
    if merged_rows:
        log(
            f"[预览图] 整库保存前合并已持久化元数据：{merged_rows}/{len(videos)} 条，"
            "避免缩略图收尾覆盖 duration/audio"
        )
    return merged_rows


def generate_thumbs_parallel(
    missing: list[dict],
    cached_n: int = 0,
    label: str = "新建",
    *,
    burst: bool = False,
) -> tuple[int, int]:
    """通过共享队列生成预览图。返回 (成功数, 失败数)。"""
    started = time.perf_counter()
    ffmpeg = STATE.get("ffmpeg")
    cache = STATE.get("cache_dir")
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
    done = 0
    last_log_i = 0
    def one(item: dict) -> bool:
        try:
            # ffprobe already proved that audio-only/corrupt containers cannot
            # yield a frame.  Do not enqueue another ffmpeg attempt during the
            # bulk scan; otherwise each retry burns time and pollutes the log.
            bad_reason = str(item.get("bad_reason") or "").casefold()
            if item.get("bad") and ("无视频流" in bad_reason or "no video" in bad_reason):
                return False
            out = thumb_path(cache, item["id"])
            src = _video_file_for_thumb(item)
            ok = bool(src and make_thumbnail(ffmpeg, src, out, background=True, burst=burst))
            if ok:
                thumb_cache_invalidate(item["id"])
            return ok
        except Exception:
            return False

    future_items: dict = {}
    for item in missing:
        key = thumbnail_job_key(cache, item["id"])
        future = submit_thumbnail_job(
            key,
            lambda item=item: one(item),
            priority=THUMB_PRIORITY_BATCH,
        )
        future_items.setdefault(future, []).append(item)

    for future in as_completed(future_items):
        try:
            ok = bool(future.result())
        except Exception:
            ok = False
        for item in future_items[future]:
            name = item.get("name") or item.get("rel") or item.get("id") or "?"
            try:
                done += 1
                i = done
                if ok:
                    ok_n += 1
                    clear_thumbnail_failure(item)
                    item["has_thumb"] = True
                    item["thumb"] = f"{item['id']}{THUMB_EXT}"
                    item["thumb_v"] = thumb_version(cache, item["id"])
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
                        f"[预览图] 进度 {i}/{total}（成功 {ok_n}，失败 {fail_n}）"
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


def fill_thumbs_for_videos(
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
    missing = missing_thumb_items(videos, cache)
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
    missing, reused = adopt_thumbs_from_caches(missing, cache)
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
    if burst and len(missing) >= 200:
        # Publish the catalog first; large ffmpeg batches must not hold the
        # scan thread (and therefore the UI) for tens of seconds.
        deferred_items = list(missing)
        STATE["thumb_progress"] = (
            f"预览图已转后台生成（待生成 {len(deferred_items)}，已有缓存 {cached_n}）"
        )
        log(
            f"[预览图] 批量任务过多，扫描已先发布索引，转后台生成："
            f"待生成 {len(deferred_items)}，已有缓存 {cached_n}"
        )

        if root is not None and not runtime_state.register_thumb_bulk(root):
            log(f"[预览图] 跳过重复后台批量：{root} 已有任务运行")
            return 0, 0

        def run_deferred() -> None:
            started = time.perf_counter()
            log(f"[预览图] 后台批量生成开始：{len(deferred_items)} 个，扫描线程已释放")
            try:
                deferred_ok, deferred_failed = generate_thumbs_parallel(
                    deferred_items,
                    cached_n=cached_n,
                    label="后台补全",
                    burst=False,
                )
                STATE["thumb_progress"] = (
                    f"预览图后台完成（缓存 {cached_n} + 新建 {deferred_ok}，失败 {deferred_failed}）"
                )
                if root is not None:
                    _preserve_catalog_probe_fields_for_thumb_save(
                        videos,
                        cache=cache,
                        root=Path(root),
                    )
                    save_index(cache, root, videos, file_count=file_count, folder_counts=folder_counts)
                    try:
                        from vg.disk_libs import sync_disk_lib_memory
                        sync_disk_lib_memory(str(Path(root).resolve()), videos)
                    except Exception:
                        pass
                elapsed = (time.perf_counter() - started) * 1000.0
                log(f"[预览图] 后台批量生成完成：成功 {deferred_ok}，失败 {deferred_failed}，耗时 {elapsed:.1f}ms")
                from vg.diagnostics import perf
                perf("thumbnail_bulk_background", elapsed, force=True, videos=len(deferred_items), cached=cached_n, ok=deferred_ok, failed=deferred_failed)
            except Exception as exc:
                STATE["thumb_progress"] = f"预览图后台生成失败: {exc}"
                log(f"[预览图] 后台批量生成失败: {exc}")
            finally:
                if root is not None:
                    runtime_state.unregister_thumb_bulk(root)

        threading.Thread(target=run_deferred, daemon=True, name="thumb-bulk-background").start()
        from vg.diagnostics import perf
        perf("thumbnail_bulk_background_started", 0.0, force=True, videos=len(videos), missing=len(deferred_items), cached=cached_n, burst=burst)
        return 0, 0
    if missing:
        label = "新建" if burst else "补全"
        ok_n, fail_n = generate_thumbs_parallel(
            missing, cached_n=cached_n, label=label, burst=burst
        )
        STATE["thumb_progress"] = (
            f"预览图完成（缓存 {cached_n} + 新建 {ok_n}，失败 {fail_n}）"
        )
        log(f"[预览图] 完成：成功 {ok_n}，失败 {fail_n}，已有 {cached_n}（复用 {reused}）")
    else:
        STATE["thumb_progress"] = f"预览图全部来自缓存（{cached_n} 个），无需重建"
        log(f"[预览图] 全部命中缓存（{cached_n}），无需重建")
    if root is not None and (missing or reused or failure_marked):
        _preserve_catalog_probe_fields_for_thumb_save(
            videos,
            cache=cache,
            root=Path(root),
        )
        save_index(cache, root, videos, file_count=file_count, folder_counts=folder_counts)
        try:
            root_s = str(Path(root).resolve())
        except OSError:
            root_s = str(root)
        try:
            sync_disk_lib_memory(root_s, videos)
        except Exception:
            pass
    from vg.diagnostics import perf as diagnostic_perf

    diagnostic_perf(
        "fill_thumbs_for_videos",
        (time.perf_counter() - started) * 1000.0,
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
            mtime_changed = abs(
                float(item.get("mtime") or 0) - float(previous.get("mtime") or 0)
            ) >= 1.0
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
    cache = STATE["cache_dir"] or ensure_cache_dir(root)
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
            and abs(float(old.get("mtime") or 0) - st.st_mtime) < 1.0
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
            elif abs(float(old.get("mtime") or 0) - st.st_mtime) >= 1.0:
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
            if not file_sig:
                count_scan("video_fingerprint_failed", full)
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
                "genres": detect_genres(rel, full.stem),
            }
            if ext in PLAYLIST_EXTS:
                item["kind"] = "m3u8"
            with walk_lock:
                added += 1
        item["root"] = root_s
        item["_lib_root"] = root_s
        ensure_video_taxonomy(item)
        if cache:
            item["_lib_cache"] = str(cache)
        item["_folder_raw"] = folder
        return item

    def note_found() -> None:
        n = len(found)
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
    if target_folders is not None:
        kept = [
            v
            for v in _load_index_videos(cache, root)
            if item_folder_key(v) not in target_folders
        ]
        for folder in sorted(target_folders):
            dirpath = root / folder if folder else root
            count_scan("directories_scanned")
            try:
                if not dirpath.is_dir():
                    continue
                names = os.listdir(dirpath)
            except OSError as err:
                on_walk_error(err if isinstance(err, OSError) else OSError(err))
                continue
            for name in names:
                full = dirpath / name
                try:
                    if not full.is_file():
                        continue
                except OSError:
                    continue
                ext = Path(name).suffix.lower()
                if ext not in VIDEO_EXTS and ext not in PLAYLIST_EXTS:
                    continue
                count_scan("candidate_files")
                item = ingest_file(full, name, ext, count_folder=False)
                if item:
                    scanned.append(item)
                    found.append(item)
                    note_found()
        scanned = collapse_segment_sets(scanned)
        found = kept + scanned
    else:
        walk_heartbeat_lock = threading.Lock()
        walk_last_heartbeat = time.perf_counter()
        walk_subtree_done = 0

        def _walk_tree(start: Path, *, children: list[str] | None = None) -> None:
            nonlocal walk_last_heartbeat, walk_subtree_done
            start_resolved = Path(start).resolve()
            local_dirs = 0
            for dirpath, dirnames, filenames in os.walk(start, onerror=on_walk_error):
                count_scan("directories_scanned")
                local_dirs += 1
                _note_top_level(dirpath)
                dirnames[:] = [d for d in dirnames if not should_skip_dir(d)]
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
        max_workers = max(1, cpus - 2)
        target_jobs = min(SCAN_WALK_JOB_CAP, max(max_workers * 2, 8))
        shallow_jobs, leaf_jobs = expand_scan_walk_jobs(
            root,
            top_kept,
            target_jobs=target_jobs,
        )
        walk_workers = 1
        if len(leaf_jobs) >= 2:
            walk_workers = max(1, min(len(leaf_jobs), max_workers))
        walk_mode = "serial"
        if walk_workers > 1:
            walk_mode = "parallel_expanded" if shallow_jobs else "parallel"
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
        if walk_workers <= 1:
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
            with ThreadPoolExecutor(max_workers=walk_workers) as pool:
                futures = {
                    pool.submit(_walk_tree, path): label for label, path in leaf_jobs
                }
                for future in as_completed(futures):
                    name = futures[future]
                    future.result()
                    with walk_heartbeat_lock:
                        walk_subtree_done += 1
                        done_n = walk_subtree_done
                    from vg.diagnostics import emit as _hb_emit

                    _hb_emit(
                        "INFO",
                        "scan_walk_subtree_done",
                        force=True,
                        root=root,
                        subtree=name,
                        done=done_n,
                        total=len(leaf_jobs),
                        directories=int(scan_counts.get("directories_scanned", 0) or 0),
                        found=len(found),
                        elapsed_ms=f"{(time.perf_counter() - scan_started) * 1000.0:.0f}",
                    )
            _pool_emit(
                "INFO",
                "scan_walk_pool_done",
                force=True,
                root=root,
                workers=walk_workers,
                jobs=len(leaf_jobs),
                elapsed_ms=f"{(time.perf_counter() - pool_started) * 1000.0:.0f}",
                directories=int(scan_counts.get("directories_scanned", 0) or 0),
                found=len(found),
            )
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
            changed_meta = False
            for key in (
                "duration",
                "duration_h",
                "has_audio",
                "audio_codec",
                "audio_channels",
                "sample_rate",
                "bad",
                "bad_reason",
            ):
                if item.get(key) in (None, "", []) and prev.get(key) not in (None, "", []):
                    item[key] = prev.get(key)
                    changed_meta = True
            if prev.get("has_thumb") and not item.get("has_thumb"):
                item["has_thumb"] = True
                item["thumb"] = prev.get("thumb") or item.get("thumb")
                item["thumb_v"] = prev.get("thumb_v") or item.get("thumb_v") or 1
                changed_meta = True
            if changed_meta:
                adopted += 1
        if adopted:
            log(f"[增量] 合集/条目继承旧元数据 {adopted} 个")

    output_reused, output_changed = _final_scan_change_counts(
        found,
        old_by_id,
        incremental=incremental,
    )

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

    if multi:
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
    else:
        _save_ok = save_index(cache, root, found, file_count=saved_n, folder_counts=saved_counts)
        _save_ms = (time.perf_counter() - _save_t0) * 1000.0
        if not _save_ok:
            log(f"[扫描] save_index 返回失败！缓存可能未持久化，下次启动将重新扫描")
        log(f"[计时] save_index: {_save_ms:.1f}ms, 视频={len(found)}, ok={_save_ok}")
    try:
        sync_disk_lib_memory(root_s, found)
    except Exception as e:
        log(f"[扫描] 同步内存索引失败: {e}")
    save_prefs(last_root=str(root))
    try:
        from vg.roots import on_scan_finished

        on_scan_finished(root)
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
        try:
            from vg.roots import get_mounted_roots, publish_unified_library

            if len(get_mounted_roots()) > 1:
                publish_unified_library(
                    heavy=False,
                    reason="thumbnail_finalize",
                    refresh_tree=False,
                )
            else:
                light_refresh_started = time.perf_counter()
                from vg.diagnostics import emit

                emit(
                    "INFO",
                    "thumbnail_finalize_light_refresh_start",
                    force=True,
                    root=root,
                    videos=len(found),
                    duplicate_rebuild_skipped=True,
                    tree_rebuild=True,
                )
                rebuild_indexes(found, heavy=False)
                STATE["tree"] = build_tree(root, found)
                emit(
                    "PERF",
                    "thumbnail_finalize_light_refresh",
                    force=True,
                    root=root,
                    videos=len(found),
                    duplicate_rebuild_skipped=True,
                    elapsed_ms=(time.perf_counter() - light_refresh_started) * 1000.0,
                )
        except Exception as e:
            log(f"[多根] 预览图完成后合并失败: {e}")
            try:
                from vg.roots import get_mounted_roots, publish_unified_library

                if len(get_mounted_roots()) > 1:
                    publish_unified_library(
                        heavy=False,
                        reason="thumbnail_finalize_retry",
                        refresh_tree=False,
                    )
                else:
                    rebuild_indexes(found, heavy=False)
            except Exception as _exc2:
                log(f"[扫描] 最终回退 rebuild_indexes 也失败: {_exc2}")
                try:
                    rebuild_indexes(found, heavy=False)
                except Exception as _exc3:
                    log(f"[扫描] rebuild_indexes 彻底失败: {_exc3}")
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

                    on_scan_finished(root)
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
    """Avoid disk contention: defer ffprobe while a large thumb batch runs."""
    if not runtime_state.thumb_bulk_running(root):
        start_metadata_enrichment()
        return

    log(f"[元数据] 已延后：{root} 正在后台批量生成缩略图，避免 ffprobe 与 ffmpeg 抢盘")

    def wait_then_start() -> None:
        started = time.perf_counter()
        while runtime_state.thumb_bulk_running(root):
            time.sleep(0.5)
        waited_ms = (time.perf_counter() - started) * 1000.0
        log(f"[元数据] 缩略图批量完成，开始后台探测（等待 {waited_ms:.1f}ms）")
        start_metadata_enrichment()

    threading.Thread(
        target=wait_then_start,
        daemon=True,
        name="meta-after-thumb-bulk",
    ).start()


def find_video_by_id(vid: str, prefer_root: str | None = None) -> dict | None:
    """Compatibility wrapper; lookup ownership lives in catalog_repository."""
    from vg.catalog_repository import find_video_by_id as repository_lookup

    return repository_lookup(vid, prefer_root)
