# -*- coding: utf-8 -*-
"""Video scanning, indexing, and thumbnail batch jobs."""
from __future__ import annotations

import json
import hashlib
import os
import threading
from concurrent.futures import as_completed
from datetime import datetime
from pathlib import Path

from vg.cache import (
    ensure_cache_dir,
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
    INDEX_NAME,
    PLAYLIST_EXTS,
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
from vg.state import STATE, _scan_lock
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


_FINGERPRINT_CHUNK = 64 * 1024


def _file_fingerprint(path: Path, st: os.stat_result | None = None) -> str:
    """Return a cheap content fingerprint for incremental-scan validation.

    Only three 64 KiB samples (head, middle and tail) are read for large files.  The digest is
    deliberately independent of mtime: mtime remains the fast path, while
    this value catches a file rewritten in place with the same size and an
    unchanged/low-resolution timestamp.
    """
    try:
        stat = st or path.stat()
        size = int(stat.st_size)
        digest = hashlib.blake2b(digest_size=16)
        with path.open("rb") as stream:
            if size <= _FINGERPRINT_CHUNK * 2:
                digest.update(stream.read())
            else:
                digest.update(stream.read(_FINGERPRINT_CHUNK))
                stream.seek(max(0, (size // 2) - (_FINGERPRINT_CHUNK // 2)))
                digest.update(stream.read(_FINGERPRINT_CHUNK))
                stream.seek(max(0, size - _FINGERPRINT_CHUNK))
                digest.update(stream.read(_FINGERPRINT_CHUNK))
        return f"b2:{size}:{digest.hexdigest()}"
    except (OSError, ValueError):
        return ""


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


def start_scan(
    root: Path,
    do_thumbs: bool = True,
    force: bool = False,
    replace_mounts: bool = True,
) -> tuple[bool, str]:
    """切换根目录。force=False 优先读缓存（秒开）再后台按目录核个数；force=True 增量全盘扫描。
    replace_mounts=True：片库只保留这一根；False：保留已挂载目录（用于「加入片库」）。
    """
    if STATE["scanning"] or not _scan_lock.acquire(blocking=False):
        return False, "正在扫描中，请稍候"

    root = root.expanduser().resolve()
    if not root.is_dir():
        _scan_lock.release()
        return False, f"目录不存在: {root}"

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
                used_cache = load_or_scan(root, do_thumbs=do_thumbs, force=False, background=False)
                STATE["scanning"] = False
                # 读到缓存后只后台核文件个数；个数一致则不再 walk 视频盘
                want_bg_verify = bool(used_cache)
        except Exception as e:
            STATE["scan_progress"] = f"扫描失败: {e}"
            log(f"[扫描] 失败: {e}")
            STATE["scanning"] = False
        finally:
            STATE["scanning"] = False
            try:
                _scan_lock.release()
            except RuntimeError:
                pass
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


def count_video_files_by_folder(root: Path) -> dict[str, int]:
    """Cheap walk: count video/playlist filenames per directory (no size/fingerprint)."""
    counts: dict[str, int] = {}

    def on_walk_error(err: OSError) -> None:
        log(f"[计数] 跳过无权限目录: {err}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_walk_error):
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
    return counts


def changed_folder_keys(stored: dict[str, int] | None, live: dict[str, int]) -> set[str]:
    old = {folder_key(k): int(v) for k, v in (stored or {}).items() if int(v) > 0}
    new = {folder_key(k): int(v) for k, v in live.items() if int(v) > 0}
    return {key for key in set(old) | set(new) if old.get(key, 0) != new.get(key, 0)}


def _bg_count_then_maybe_scan(root: Path, do_thumbs: bool) -> None:
    """Reopen: count files per folder; scan only folders whose counts changed."""
    if not _scan_lock.acquire(blocking=False):
        log("[计数] 跳过：已有扫描任务")
        return
    try:
        if STATE.get("root") and Path(STATE["root"]).resolve() != root.resolve():
            log("[计数] 跳过：根目录已切换")
            return
        STATE["updating"] = True
        cache = ensure_cache_dir(root)
        STATE["cache_dir"] = cache
        live = count_video_files_by_folder(root)
        live_n = sum(live.values())
        _stored_n, stored = read_index_counts(cache)
        if stored is None:
            videos = _load_index_videos(cache, root)
            save_index(cache, root, videos, file_count=live_n, folder_counts=live)
            log(f"[缓存] 已写入目录文件计数 {live_n}，跳过全盘扫描")
            STATE["scan_progress"] = f"已加载缓存，共 {len(videos)} 个视频"
        else:
            changed = changed_folder_keys(stored, live)
            if not changed:
                log(f"[缓存] 文件个数一致（{live_n}），跳过扫描")
                STATE["scan_progress"] = f"已加载缓存，共 {len(STATE.get('videos') or [])} 个视频"
            else:
                log(f"[增量] {len(changed)} 个目录文件数变化，定向扫描")
                STATE["scan_progress"] = f"后台更新 {len(changed)} 个目录…"
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
        log(f"[计数] 失败: {e}")
    finally:
        STATE["updating"] = False
        try:
            _scan_lock.release()
        except RuntimeError:
            pass

def _load_old_video_map(cache: Path, root: Path) -> dict[str, dict]:
    """从索引建立 rel → 条目，供增量复用（TS 合集拆成段后不进 map，行走时重收）。"""
    index_path = cache / INDEX_NAME
    old_map: dict[str, dict] = {}
    if not index_path.exists():
        return old_map
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
        if not _same_root(data.get("root"), root) or not isinstance(data.get("videos"), list):
            return old_map
        for v in data["videos"]:
            if v.get("kind") == "ts_set":
                continue
            rel = (v.get("rel") or "").replace("\\", "/")
            if rel:
                old_map[rel] = v
    except Exception as e:
        log(f"[增量] 读取旧索引失败: {e}")
    return old_map


def generate_thumbs_parallel(
    missing: list[dict],
    cached_n: int = 0,
    label: str = "新建",
    *,
    burst: bool = False,
) -> tuple[int, int]:
    """通过共享队列生成预览图。返回 (成功数, 失败数)。"""
    ffmpeg = STATE.get("ffmpeg")
    cache = STATE.get("cache_dir")
    if not missing or not ffmpeg or not cache:
        return 0, 0
    total = len(missing)
    workers = thumb_worker_count(total, burst=burst)
    ensure_thumbnail_workers(workers)
    mode = "满核" if burst else "后台"
    STATE["thumb_progress"] = (
        f"预览图缓存 {cached_n} 个，需{label} {total} 个"
        f"（{mode} {workers} 路）…"
    )
    log(f"[预览图] {label} {total} 个，{mode} {workers} 路生成")
    ok_n = 0
    fail_n = 0
    done = 0
    def one(item: dict) -> bool:
        try:
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
                    item["has_thumb"] = True
                    item["thumb"] = f"{item['id']}{THUMB_EXT}"
                    item["thumb_v"] = thumb_version(cache, item["id"])
                    log(f"[预览图] ({i}/{total}) OK  {name}")
                else:
                    fail_n += 1
                    item["has_thumb"] = False
                    item["thumb_v"] = 0
                    log(f"[预览图] ({i}/{total}) 失败  {name}")
                STATE["thumb_progress"] = (
                    f"{label}加密预览图 {i}/{total}（已有缓存 {cached_n}，成功 {ok_n}）…"
                )
            except Exception as exc:
                fail_n += 1
                log(f"[预览图] 更新状态失败 {name}: {exc}")
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
    ffmpeg = STATE.get("ffmpeg")
    if not videos or not cache:
        return 0, 0
    if not ffmpeg:
        STATE["thumb_progress"] = "未找到 ffmpeg，已跳过预览图（安装后重启可生成）"
        log("[预览图] 未找到 ffmpeg，已跳过")
        return 0, 0
    missing = missing_thumb_items(videos, cache)
    missing, reused = adopt_thumbs_from_caches(missing, cache)
    cached_n = len(videos) - len(missing)
    ok_n = fail_n = 0
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
    if root is not None and (missing or reused):
        save_index(cache, root, videos, file_count=file_count, folder_counts=folder_counts)
        try:
            root_s = str(Path(root).resolve())
        except OSError:
            root_s = str(root)
        try:
            sync_disk_lib_memory(root_s, videos)
        except Exception:
            pass
    return ok_n, fail_n


def _load_index_videos(cache: Path, root: Path) -> list[dict]:
    index_path = cache / INDEX_NAME
    if not index_path.exists():
        return []
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
        if not _same_root(data.get("root"), root) or not isinstance(data.get("videos"), list):
            return []
        return [dict(v) for v in data["videos"] if isinstance(v, dict)]
    except Exception as e:
        log(f"[增量] 读取旧索引失败: {e}")
        return []


def scan_videos(
    root: Path,
    do_thumbs: bool = True,
    incremental: bool = True,
    quiet: bool = False,
    only_folders: set[str] | None = None,
    folder_counts: dict[str, int] | None = None,
    burst_thumbs: bool | None = None,
) -> None:
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

    old_map = _load_old_video_map(cache, root) if incremental else {}
    if old_map:
        log(f"[增量] 可复用旧条目 {len(old_map)} 个")
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
    last_progress_ts = 0.0
    last_tree_n = 0

    def _publish_live(force_tree: bool = False) -> None:
        """Push mid-scan catalog without wiping other disks from STATE."""
        nonlocal last_progress_ts, last_tree_n
        import time as _time

        now = _time.time()
        # Items are stamped as they are found; just expose the live list.
        STATE["scan_live"] = found
        STATE["scan_progress"] = f"已发现 {len(found)} 个视频…"
        STATE["lib_gen"] = int(STATE.get("lib_gen") or 0) + 1
        # Sync disk_libs / tree infrequently — scan_live already covers API reads.
        if force_tree or (len(found) - last_tree_n >= 400) or (now - last_progress_ts >= 3.0):
            try:
                store_live_library(root_s, found)
            except Exception as e:
                log(f"[扫描] 实时入库失败: {e}")
            if not quiet:
                try:
                    from vg.roots import get_mounted_roots, tree_for_scope

                    if len(get_mounted_roots()) > 1:
                        STATE["tree"] = tree_for_scope(None)
                    else:
                        STATE["tree"] = build_tree(root, found)
                except Exception:
                    try:
                        STATE["tree"] = build_tree(root, found)
                    except Exception:
                        pass
            last_tree_n = len(found)
            last_progress_ts = now

    def on_walk_error(err: OSError) -> None:
        if len(errors) < 5:
            errors.append(str(err))
            log(f"[扫描] 跳过无权限目录: {err}")
        STATE["scan_progress"] = f"已发现 {len(found)} 个视频…（部分目录无权限已跳过）"

    def ingest_file(full: Path, name: str, ext: str, *, count_folder: bool) -> dict | None:
        nonlocal reused, added
        try:
            rel = safe_rel(full, root)
            st = full.stat()
        except (ValueError, OSError):
            return None
        folder = folder_key(str(Path(rel).parent) if Path(rel).parent != Path(".") else "")
        if count_folder and accumulate_counts:
            walk_counts[folder] = walk_counts.get(folder, 0) + 1
        if is_too_small_video(ext, st.st_size):
            return None
        old = old_map.get(rel)
        metadata_match = bool(
            old
            and int(old.get("size") or -1) == st.st_size
            and abs(float(old.get("mtime") or 0) - st.st_mtime) < 1.0
        )
        old_sig = str(old.get("file_sig") or "") if old else ""
        signature_match = True
        current_sig = ""
        if metadata_match and old_sig:
            current_sig = _file_fingerprint(full, st)
            signature_match = bool(current_sig) and current_sig == old_sig
        if metadata_match and signature_match:
            item = dict(old)
            item["id"] = item.get("id") or video_id(rel)
            item["size"] = st.st_size
            item["size_h"] = format_size(st.st_size)
            item["mtime"] = st.st_mtime
            item["mtime_h"] = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
            item["file_sig"] = current_sig or old_sig or _file_fingerprint(full, st)
            item["thumb"] = f"{item['id']}{THUMB_EXT}"
            item["ext"] = ext
            if ext in PLAYLIST_EXTS:
                item["kind"] = "m3u8"
            ensure_video_genres(item)
            reused += 1
        else:
            vid = video_id(rel)
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
                "file_sig": _file_fingerprint(full, st),
                "duration": None,
                "duration_h": "",
                "thumb": f"{vid}{THUMB_EXT}",
                "has_thumb": False,
                "genres": detect_genres(rel, full.stem),
            }
            if ext in PLAYLIST_EXTS:
                item["kind"] = "m3u8"
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
                log(f"[扫描] 已发现 {n} 个…（复用 {reused} / 新建 {added}）")
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
                item = ingest_file(full, name, ext, count_folder=False)
                if item:
                    scanned.append(item)
                    found.append(item)
                    note_found()
        scanned = collapse_segment_sets(scanned)
        found = kept + scanned
    else:
        for dirpath, dirnames, filenames in os.walk(root, onerror=on_walk_error):
            dirnames[:] = [d for d in dirnames if not should_skip_dir(d)]
            for name in filenames:
                ext = Path(name).suffix.lower()
                if ext not in VIDEO_EXTS and ext not in PLAYLIST_EXTS:
                    continue
                full = Path(dirpath) / name
                item = ingest_file(full, name, ext, count_folder=True)
                if item:
                    found.append(item)
                    note_found()
        found = collapse_segment_sets(found)

    found.sort(key=lambda x: (x.get("rel") or "").lower())
    stamp_lib_meta(found, root=root_s, cache=cache, overwrite=True)
    for v in found:
        v["root"] = root_s
        if "_folder_raw" not in v:
            v["_folder_raw"] = (v.get("folder") or "").replace("\\", "/").strip("/")
    STATE["scan_live"] = found
    try:
        store_live_library(root_s, found)
    except Exception:
        pass
    try:
        from vg.roots import get_mounted_roots, tree_for_scope

        if len(get_mounted_roots()) > 1:
            STATE["tree"] = tree_for_scope(None)
        else:
            STATE["tree"] = build_tree(root, found)
    except Exception:
        STATE["tree"] = build_tree(root, found)

    # Never replace the whole multi-disk STATE with one disk mid-flight.
    multi = False
    try:
        from vg.roots import get_mounted_roots

        multi = len(get_mounted_roots()) > 1
    except Exception:
        multi = False

    if multi:
        # Leave other disks intact; unified publish happens after save_index.
        pass
    else:
        rebuild_indexes(found)

    extra = f"（{len(errors)} 个目录跳过）" if errors else ""
    tip = f"，复用 {reused}，新建/变更 {added}" if incremental else ""
    STATE["scan_progress"] = f"扫描完成，共 {len(found)} 个视频{tip}{extra}"
    log(f"[扫描] 完成，共 {len(found)} 个{tip}{extra}")
    saved_counts = walk_counts
    saved_n = sum(saved_counts.values())
    save_index(cache, root, found, file_count=saved_n, folder_counts=saved_counts)
    try:
        sync_disk_lib_memory(root_s, found)
    except Exception as e:
        log(f"[扫描] 同步内存索引失败: {e}")
    save_prefs(last_root=str(root))
    try:
        from vg.roots import on_scan_finished

        on_scan_finished(root)
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
                publish_unified_library()
            else:
                rebuild_indexes(found)
                STATE["tree"] = build_tree(root, found)
        except Exception as e:
            log(f"[多根] 预览图完成后合并失败: {e}")
            try:
                from vg.roots import get_mounted_roots, publish_unified_library

                if len(get_mounted_roots()) > 1:
                    publish_unified_library()
                else:
                    rebuild_indexes(found)
            except Exception:
                rebuild_indexes(found)
    elif not ffmpeg:
        STATE["thumb_progress"] = "未找到 ffmpeg，已跳过预览图（安装后重启可生成）"
        log("[预览图] 未找到 ffmpeg，已跳过")
        if not multi:
            STATE["tree"] = build_tree(root, found)
    elif not quiet:
        STATE["thumb_progress"] = ""
        if not multi:
            STATE["tree"] = build_tree(root, found)

    STATE["scan_live"] = None
    STATE["scan_root"] = ""
    if not quiet:
        STATE["scanning"] = False
    log("[扫描] 全部结束，可在浏览器浏览")
    start_metadata_enrichment()


def load_or_scan(root: Path, do_thumbs: bool, force: bool = False, background: bool = True) -> bool:
    """加载缓存或扫描。返回 True 表示成功使用了缓存。"""
    cache = ensure_cache_dir(root)
    STATE["root"] = root
    STATE["cache_dir"] = cache
    index_path = cache / INDEX_NAME

    if not force and index_path.exists():
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
            if _same_root(data.get("root"), root) and isinstance(data.get("videos"), list):
                videos = []
                for raw in data["videos"]:
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
                STATE["tree"] = build_tree(root, videos)
                rebuild_indexes(videos)
                STATE["scan_progress"] = f"已加载缓存，共 {len(videos)} 个视频"
                log(f"[缓存] 已加载 {len(videos)} 个视频 ← {index_path}")
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


def find_video_by_id(vid: str, prefer_root: str | None = None) -> dict | None:
    """Compatibility wrapper; lookup ownership lives in catalog_repository."""
    from vg.catalog_repository import find_video_by_id as repository_lookup

    return repository_lookup(vid, prefer_root)
