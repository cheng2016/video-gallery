# -*- coding: utf-8 -*-
"""Video scanning, indexing, and thumbnail batch jobs."""
from __future__ import annotations

import json
import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from vg.cache import (
    ensure_cache_dir,
    save_index,
    thumb_cache_invalidate,
    thumb_file_ready,
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
    save_library_item,
    stamp_lib_meta,
    store_live_library,
    sync_disk_lib_memory,
)
from vg.drives import save_prefs
from vg.genres import detect_genres, ensure_video_genres
from vg.segments import collapse_segment_sets
from vg.state import STATE, _scan_lock
from vg.taxonomy import ensure_video_taxonomy
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
    """切换根目录。force=False 优先读缓存（秒开）再后台增量；force=True 增量全盘扫描。
    replace_mounts=True：片库只保留这一根；False：保留已挂载目录（用于「加入片库」）。
    """
    if STATE["scanning"] or not _scan_lock.acquire(blocking=False):
        return False, "正在扫描中，请稍候"

    root = root.expanduser().resolve()
    if not root.is_dir():
        _scan_lock.release()
        return False, f"目录不存在: {root}"

    want_bg_incremental = False

    def run():
        nonlocal want_bg_incremental
        try:
            # 换盘前归档当前片库，局域网历史仍可播旧盘视频
            try:
                cur = STATE.get("root")
                if cur and Path(cur).resolve() != root.resolve():
                    archive_current_library()
            except OSError:
                archive_current_library()
            try:
                from vg.roots import get_mounted_roots, set_mounted_roots

                root_s = str(root.resolve())
                if replace_mounts:
                    set_mounted_roots([root_s], primary=root_s)
                else:
                    mounts = get_mounted_roots()
                    if root_s.lower() not in {m.lower() for m in mounts}:
                        mounts = list(mounts) + [root_s]
                    set_mounted_roots(mounts, primary=root_s)
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
                    from vg.roots import get_mounted_roots
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
                        STATE["videos"] = keep
                    else:
                        STATE["videos"] = []
                except Exception:
                    STATE["videos"] = []
                STATE["tree"] = {
                    "name": root.name or str(root),
                    "path": "",
                    "count": 0,
                    "children": [],
                    "videos": [],
                }
                scan_videos(root, do_thumbs=do_thumbs, incremental=True, quiet=False)
            else:
                STATE["scan_progress"] = f"正在加载 {root}（优先缓存）…"
                STATE["thumb_progress"] = ""
                STATE["scanning"] = True
                used_cache = load_or_scan(root, do_thumbs=do_thumbs, force=False, background=False)
                STATE["scanning"] = False
                # 仅「读到缓存」后才后台增量；若已全量扫描则不必再扫一遍
                want_bg_incremental = bool(used_cache)
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
            if want_bg_incremental:
                threading.Thread(
                    target=_bg_incremental_scan,
                    args=(root, do_thumbs),
                    daemon=True,
                ).start()

    STATE["scanning"] = True
    threading.Thread(target=run, daemon=True).start()
    mode = "增量全盘扫描" if force else "加载缓存"
    return True, f"开始{mode} {root}"


def _bg_incremental_scan(root: Path, do_thumbs: bool) -> None:
    """打开此盘后的后台增量：只更新新增/变更/删除，不挡浏览。"""
    if not _scan_lock.acquire(blocking=False):
        log("[增量] 跳过：已有扫描任务")
        return
    try:
        if STATE.get("root") and Path(STATE["root"]).resolve() != root.resolve():
            log("[增量] 跳过：根目录已切换")
            return
        STATE["updating"] = True
        STATE["cache_dir"] = ensure_cache_dir(root)
        log(f"[增量] 后台检查 {root} …")
        scan_videos(root, do_thumbs=do_thumbs, incremental=True, quiet=True)
    except Exception as e:
        log(f"[增量] 失败: {e}")
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


def generate_thumbs_parallel(missing: list[dict], cached_n: int = 0, label: str = "新建") -> tuple[int, int]:
    """并行生成预览图。返回 (成功数, 失败数)。"""
    ffmpeg = STATE.get("ffmpeg")
    cache = STATE.get("cache_dir")
    if not missing or not ffmpeg or not cache:
        return 0, 0
    total = len(missing)
    STATE["thumb_progress"] = f"预览图缓存 {cached_n} 个，需{label} {total} 个（{thumb_worker_count(total)} 线程）…"
    log(f"[预览图] {label} {total} 个，并行 {thumb_worker_count(total)} 线程")
    ok_n = 0
    fail_n = 0
    done = 0
    lock = threading.Lock()

    def one(item: dict) -> tuple[dict, bool, str]:
        name = item.get("name") or item.get("rel") or item.get("id") or "?"
        try:
            out = thumb_path(cache, item["id"])
            src = _video_file_for_thumb(item)
            ok = bool(src and make_thumbnail(ffmpeg, src, out))
            if ok:
                thumb_cache_invalidate(item["id"])
            return item, ok, name
        except Exception as e:
            return item, False, f"{name} ({e})"

    workers = thumb_worker_count(total)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, it) for it in missing]
        for fut in as_completed(futures):
            item, ok, name = fut.result()
            with lock:
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
    return ok_n, fail_n


def scan_videos(
    root: Path,
    do_thumbs: bool = True,
    incremental: bool = True,
    quiet: bool = False,
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

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_walk_error):
        dirnames[:] = [d for d in dirnames if not should_skip_dir(d)]
        for name in filenames:
            ext = Path(name).suffix.lower()
            if ext not in VIDEO_EXTS and ext not in PLAYLIST_EXTS:
                continue
            full = Path(dirpath) / name
            try:
                rel = safe_rel(full, root)
                st = full.stat()
            except (ValueError, OSError):
                continue
            # 跳过过小的假/损坏视频；播放列表 .m3u8 体积小，不按此过滤
            if is_too_small_video(ext, st.st_size):
                continue
            old = old_map.get(rel)
            metadata_match = bool(
                old
                and int(old.get("size") or -1) == st.st_size
                and abs(float(old.get("mtime") or 0) - st.st_mtime) < 1.0
            )
            old_sig = str(old.get("file_sig") or "") if old else ""
            # Existing indexes predating file_sig remain usable.  Newer rows
            # get a content check only after the cheap metadata check passes.
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
                # Backfill the fingerprint on reuse.  For legacy rows this is
                # a migration; on the next scan it becomes a real comparison.
                item["file_sig"] = current_sig or _file_fingerprint(full, st)
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
                    "folder": str(Path(rel).parent).replace("\\", "/") if Path(rel).parent != Path(".") else "",
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
            if "_folder_raw" not in item:
                item["_folder_raw"] = (item.get("folder") or "").replace("\\", "/").strip("/")
            found.append(item)
            n = len(found)
            if n % 100 == 0:
                _publish_live(force_tree=(n % 500 == 0))
                if n % 200 == 0:
                    log(f"[扫描] 已发现 {n} 个…（复用 {reused} / 新建 {added}）")
            elif n == 25:
                # First batch: make the third disk visible ASAP
                _publish_live(force_tree=True)

    found.sort(key=lambda x: x["rel"].lower())
    found = collapse_segment_sets(found)
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
    save_index(cache, root, found)
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
        missing = []
        for item in found:
            if thumb_file_ready(cache, item["id"]):
                item["has_thumb"] = True
                item["thumb"] = f"{item['id']}{THUMB_EXT}"
                item["thumb_v"] = thumb_version(cache, item["id"])
            else:
                missing.append(item)
        cached_n = len(found) - len(missing)
        if missing:
            ok_n, fail_n = generate_thumbs_parallel(missing, cached_n=cached_n, label="新建")
            STATE["thumb_progress"] = f"预览图完成（缓存 {cached_n} + 新建 {ok_n}，失败 {fail_n}，已加密）"
            log(f"[预览图] 完成：成功 {ok_n}，失败 {fail_n}，原缓存 {cached_n}")
        else:
            for item in found:
                item["has_thumb"] = True
                item["thumb"] = f"{item['id']}{THUMB_EXT}"
            STATE["thumb_progress"] = f"预览图全部来自加密缓存（{cached_n} 个），无需重建"
            log(f"[预览图] 全部命中缓存（{cached_n}），无需重建")
        save_index(cache, root, found)
        try:
            sync_disk_lib_memory(root_s, found)
        except Exception:
            pass
        # One final merge/rebuild after thumbs — avoid triple publish.
        try:
            from vg.roots import get_mounted_roots, publish_unified_library

            if len(get_mounted_roots()) > 1:
                publish_unified_library()
            else:
                rebuild_indexes(found)
                STATE["tree"] = build_tree(root, found)
        except Exception as e:
            log(f"[多根] 预览图完成后合并失败: {e}")
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
                for v in data["videos"]:
                    if v.get("kind") == "m3u8":
                        rel = v.get("rel") or ""
                        try:
                            if not (root / rel).is_file():
                                continue
                        except OSError:
                            continue
                        v = dict(v)
                        v["kind"] = "m3u8"
                        v["ext"] = ".m3u8"
                        vid = v.get("id") or video_id(rel)
                        v["id"] = vid
                        v["thumb"] = f"{vid}{THUMB_EXT}"
                        v["has_thumb"] = thumb_file_ready(cache, vid)
                        ensure_video_genres(v)
                        videos.append(v)
                        continue
                    if v.get("kind") == "ts_set" and v.get("segments"):
                        segs = []
                        for rel in v["segments"]:
                            try:
                                if (root / rel).is_file():
                                    segs.append(rel)
                            except OSError:
                                continue
                        if len(segs) < 2:
                            continue
                        v = dict(v)
                        v["segments"] = segs
                        v["seg_count"] = len(segs)
                        v["rel"] = segs[0]
                        vid = v.get("id") or video_id(f"__ts_set__/{v.get('folder') or '_root_'}")
                        v["id"] = vid
                        v["thumb"] = f"{vid}{THUMB_EXT}"
                        v["has_thumb"] = thumb_file_ready(cache, vid)
                        ensure_video_genres(v)
                        videos.append(v)
                        continue
                    rel = v.get("rel") or ""
                    try:
                        if not (root / rel).is_file():
                            continue
                    except OSError:
                        continue
                    ext = (v.get("ext") or Path(rel).suffix).lower()
                    size = int(v.get("size") or 0)
                    if size <= 0:
                        try:
                            size = int((root / rel).stat().st_size)
                        except OSError:
                            size = 0
                    if is_too_small_video(ext, size):
                        continue
                    vid = v.get("id") or video_id(rel)
                    v["id"] = vid
                    v["thumb"] = f"{vid}{THUMB_EXT}"
                    v["has_thumb"] = thumb_file_ready(cache, vid)
                    ensure_video_genres(v)
                    videos.append(v)
                videos = collapse_segment_sets(videos)
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
                dropped = len(data["videos"]) - len(videos)
                tip = f"，已忽略失效 {dropped} 个" if dropped else ""
                STATE["scan_progress"] = f"已加载缓存，共 {len(videos)} 个视频{tip}（后台将增量更新）"
                log(f"[缓存] 已加载 {len(videos)} 个视频{tip} ← {index_path}")
                save_index(cache, root, videos)
                save_prefs(last_root=str(root))
                try:
                    from vg.roots import on_scan_finished

                    on_scan_finished(root)
                except Exception as e:
                    log(f"[多根] 缓存收尾失败: {e}")
                # 缺图交给随后的后台增量扫描统一并行补全，避免与增量抢状态
                missing = sum(1 for v in videos if not v.get("has_thumb"))
                if missing:
                    log(f"[预览图] 缓存缺图约 {missing} 个，将在后台增量时补全")
                else:
                    log("[预览图] 缓存齐全")
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
            kwargs={"do_thumbs": do_thumbs, "incremental": True, "quiet": False},
            daemon=True,
        ).start()
    else:
        scan_videos(root, do_thumbs=do_thumbs, incremental=True, quiet=False)
    return False


def _fill_missing_thumbs(missing: list[dict]) -> None:
    cache = STATE["cache_dir"]
    root = STATE["root"]
    ok_n, fail_n = generate_thumbs_parallel(missing, cached_n=0, label="补全")
    STATE["thumb_progress"] = f"预览图完成（成功 {ok_n}，失败 {fail_n}，已加密）"
    log(f"[预览图] 补全完成：成功 {ok_n}，失败 {fail_n}")
    if root and cache:
        STATE["tree"] = build_tree(root, STATE["videos"])
        rebuild_indexes(STATE["videos"])
        for item in missing:
            try:
                save_library_item(item)
            except Exception as e:
                log(f"[预览图] 保存单条索引失败: {e}")


def find_video_by_id(vid: str, prefer_root: str | None = None) -> dict | None:
    """Compatibility wrapper; lookup ownership lives in catalog_repository."""
    from vg.catalog_repository import find_video_by_id as repository_lookup

    return repository_lookup(vid, prefer_root)
