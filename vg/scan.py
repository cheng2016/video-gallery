# -*- coding: utf-8 -*-
"""Video scanning, indexing, and thumbnail batch jobs."""
from __future__ import annotations

import json
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
from vg.config import (
    GENRE_DEFS,
    INDEX_NAME,
    MIN_VIDEO_FILE_BYTES,
    PLAYLIST_EXTS,
    THUMB_EXT,
    VIDEO_EXTS,
)
from vg.disk_libs import archive_current_library, find_in_disk_libs, stamp_lib_meta
from vg.drives import save_prefs
from vg.genres import detect_genres, ensure_video_genres
from vg.segments import collapse_segment_sets
from vg.series import attach_series
from vg.state import STATE, _scan_lock
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


def _video_search_text(v: dict) -> str:
    cached = v.get("_q")
    if cached is not None:
        return cached
    text = f"{v.get('name') or ''} {v.get('rel') or ''}".lower()
    v["_q"] = text
    return text


def mark_duplicates(videos: list[dict]) -> None:
    """
    标记疑似重复片：
    - 同名（忽略大小写/首尾空格）且均非 m3u8；
    - 或体积完全相同且 ≥ MIN_VIDEO_FILE_BYTES（排除小文件噪声）。
    写入 dup / dup_n / dup_reason，供前端展示。
    """
    for v in videos:
        v.pop("dup", None)
        v.pop("dup_n", None)
        v.pop("dup_reason", None)

    by_name: dict[str, list[dict]] = {}
    by_size: dict[int, list[dict]] = {}
    for v in videos:
        kind = v.get("kind") or ""
        if kind in ("m3u8", "ts_set"):
            continue
        name_key = (v.get("name") or Path(v.get("filename") or "").stem or "").strip().casefold()
        if name_key:
            by_name.setdefault(name_key, []).append(v)
        size = int(v.get("size") or 0)
        if size >= MIN_VIDEO_FILE_BYTES:
            by_size.setdefault(size, []).append(v)

    def _flag(group: list[dict], reason: str) -> None:
        if len(group) < 2:
            return
        n = len(group)
        for v in group:
            prev = int(v.get("dup_n") or 0)
            v["dup"] = True
            v["dup_n"] = max(prev, n)
            reasons = set(str(v.get("dup_reason") or "").split("+")) if v.get("dup_reason") else set()
            reasons.discard("")
            reasons.add(reason)
            v["dup_reason"] = "+".join(sorted(reasons))

    for group in by_name.values():
        _flag(group, "同名")
    for group in by_size.values():
        # 同体积且路径不同才算；同名组已标过也叠加原因
        paths = {(g.get("rel") or "") for g in group}
        if len(paths) >= 2:
            _flag(group, "同体积")


def rebuild_indexes(videos: list[dict] | None = None) -> None:
    """扫描结束后预计算频道索引与侧面统计，加速 /api/tree、/api/videos。"""
    videos = videos if videos is not None else STATE.get("videos") or []
    mark_duplicates(videos)
    attach_series(videos)
    by_cat: dict[str, list] = {}
    by_id: dict[str, dict] = {}
    type_counts: dict[str, int] = {}
    cat_counts: dict[str, int] = {}
    genre_counts: dict[str, int] = {}

    for v in videos:
        vid = v.get("id")
        if vid:
            by_id[vid] = v
        _video_search_text(v)
        cat = _video_category(v)
        by_cat.setdefault(cat, []).append(v)
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
        ext = (v.get("ext") or "").lower() or "unknown"
        type_counts[ext] = type_counts.get(ext, 0) + 1
        for g in ensure_video_genres(v):
            genre_counts[g] = genre_counts.get(g, 0) + 1

    types = [
        {"ext": ext, "count": cnt, "label": ext.lstrip(".").upper() or "未知"}
        for ext, cnt in sorted(type_counts.items(), key=lambda x: (-x[1], x[0]))
    ]
    genre_order = {name: i for i, (name, _) in enumerate(GENRE_DEFS)}
    genres = [
        {"id": name, "name": name, "count": cnt}
        for name, cnt in sorted(
            genre_counts.items(),
            key=lambda x: (genre_order.get(x[0], 999), -x[1], x[0]),
        )
        if cnt > 0
    ]
    prefer = ["电影", "电视剧", "综艺", "动漫", "少儿", "纪录片", "短剧", "体育", "音乐", "教育", "其他", ""]
    prefer_rank = {n: i for i, n in enumerate(prefer)}

    def cat_sort_key(item: tuple[str, int]):
        name, cnt = item
        return (prefer_rank.get(name, 100), -cnt, name.lower())

    categories = []
    for name, cnt in sorted(cat_counts.items(), key=cat_sort_key):
        categories.append({
            "id": name,
            "name": "未分类" if name == "" else name,
            "count": cnt,
        })

    STATE["videos"] = videos
    STATE["by_category"] = by_cat
    STATE["by_id"] = by_id
    # 多盘条目已有 _lib_root，绝不能用当前 STATE.root 全量覆盖
    stamp_lib_meta(videos, overwrite=False)
    STATE["facets"] = {
        "types": types,
        "genres": genres,
        "categories": categories,
        "count": len(videos),
    }


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

def build_tree(root: Path, videos: list[dict]) -> dict:
    """按相对路径文件夹层级建树。"""
    root_node = {"name": root.name or str(root), "path": "", "children": {}, "videos": []}

    for v in videos:
        parts = Path(v["rel"]).parts
        folders, filename = parts[:-1], parts[-1]
        node = root_node
        cum = []
        for folder in folders:
            cum.append(folder)
            key = "/".join(cum)
            if folder not in node["children"]:
                node["children"][folder] = {
                    "name": folder,
                    "path": key,
                    "children": {},
                    "videos": [],
                }
            node = node["children"][folder]
        node["videos"].append(v)

    def finalize(n: dict) -> dict:
        children = [finalize(c) for c in sorted(n["children"].values(), key=lambda x: x["name"].lower())]
        videos_sorted = sorted(n["videos"], key=lambda x: x["name"].lower())
        # 统计本节点及子节点视频数
        count = len(videos_sorted) + sum(c["count"] for c in children)
        return {
            "name": n["name"],
            "path": n["path"],
            "count": count,
            "children": children,
            "videos": videos_sorted,
        }

    return finalize(root_node)


def _load_old_video_map(cache: Path, root: Path) -> dict[str, dict]:
    """从索引建立 rel → 条目，供增量复用（TS 合集拆成段后不进 map，行走时重收）。"""
    index_path = cache / INDEX_NAME
    old_map: dict[str, dict] = {}
    if not index_path.exists():
        return old_map
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
        if data.get("root") != str(root) or not isinstance(data.get("videos"), list):
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
            if (
                old
                and int(old.get("size") or -1) == st.st_size
                and abs(float(old.get("mtime") or 0) - st.st_mtime) < 1.0
            ):
                item = dict(old)
                item["id"] = item.get("id") or video_id(rel)
                item["size"] = st.st_size
                item["size_h"] = format_size(st.st_size)
                item["mtime"] = st.st_mtime
                item["mtime_h"] = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
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
                    "duration": None,
                    "duration_h": "",
                    "thumb": f"{vid}{THUMB_EXT}",
                    "has_thumb": False,
                    "genres": detect_genres(rel, full.stem),
                }
                if ext in PLAYLIST_EXTS:
                    item["kind"] = "m3u8"
                added += 1
            found.append(item)
            if len(found) % 200 == 0:
                STATE["videos"] = found
                if not quiet:
                    STATE["tree"] = build_tree(root, found)
                STATE["scan_progress"] = f"已发现 {len(found)} 个视频…"
                log(f"[扫描] 已发现 {len(found)} 个…（复用 {reused} / 新建 {added}）")
            elif len(found) % 50 == 0:
                STATE["scan_progress"] = f"已发现 {len(found)} 个视频…"
                if not quiet:
                    STATE["videos"] = found

    found.sort(key=lambda x: x["rel"].lower())
    found = collapse_segment_sets(found)
    try:
        root_s = str(Path(root).resolve())
    except OSError:
        root_s = str(root)
    stamp_lib_meta(found, root=root_s, cache=cache, overwrite=True)
    for v in found:
        v["root"] = root_s
        if "_folder_raw" not in v:
            v["_folder_raw"] = (v.get("folder") or "").replace("\\", "/").strip("/")
    STATE["tree"] = build_tree(root, found)
    rebuild_indexes(found)
    extra = f"（{len(errors)} 个目录跳过）" if errors else ""
    tip = f"，复用 {reused}，新建/变更 {added}" if incremental else ""
    STATE["scan_progress"] = f"扫描完成，共 {len(found)} 个视频{tip}{extra}"
    log(f"[扫描] 完成，共 {len(found)} 个{tip}{extra}")
    save_index(cache, root, found)
    save_prefs(last_root=str(root))
    try:
        from vg.roots import on_scan_finished

        on_scan_finished(root)
    except Exception as e:
        log(f"[多根] 扫描收尾失败: {e}")

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
        rebuild_indexes(found)
    elif not ffmpeg:
        STATE["thumb_progress"] = "未找到 ffmpeg，已跳过预览图（安装后重启可生成）"
        log("[预览图] 未找到 ffmpeg，已跳过")
    elif not quiet:
        STATE["thumb_progress"] = ""

    STATE["tree"] = build_tree(root, found)
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
            if data.get("root") == str(root) and isinstance(data.get("videos"), list):
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
        save_index(cache, root, STATE["videos"])



def find_video_by_id(vid: str, prefer_root: str | None = None) -> dict | None:
    """Find video in active library, then archived / cached disk indexes.

    prefer_root: when history remembers which disk, prefer that library (avoids id collision).
    """
    from vg.disk_libs import ensure_cached_indexes_scanned, ensure_library

    prefer = (prefer_root or "").strip() or None
    if prefer:
        ensure_library(prefer)
        hit = find_in_disk_libs(vid, prefer_root=prefer)
        if hit is not None:
            return hit
        # same as active?
        cur = STATE.get("root")
        try:
            if cur and str(Path(cur).resolve()) == str(Path(prefer).expanduser().resolve()):
                by_id = STATE.get("by_id") or {}
                hit = by_id.get(vid)
                if hit is not None:
                    return hit
        except OSError:
            pass

    by_id = STATE.get("by_id") or {}
    hit = by_id.get(vid)
    if hit is not None:
        return hit
    hit = next((v for v in STATE.get("videos") or [] if v.get("id") == vid), None)
    if hit is not None:
        return hit

    hit = find_in_disk_libs(vid, prefer_root=None)
    if hit is not None:
        return hit

    # 冷启动 / 未归档过的盘：扫一遍 preview_cache 里仍在线的索引
    ensure_cached_indexes_scanned()
    return find_in_disk_libs(vid, prefer_root=prefer)


def _video_category(v: dict) -> str:
    """一级目录作为频道。"""
    folder = (v.get("folder") or "").strip("/")
    if not folder:
        return ""
    return folder.split("/")[0]


