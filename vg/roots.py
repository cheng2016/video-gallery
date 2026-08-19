# -*- coding: utf-8 -*-
"""Multi-root library: mount several folders into one catalog."""
from __future__ import annotations

import hashlib
import threading
from pathlib import Path

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
    root_s_l = root_s.lower()

    # Scanning this disk: prefer live in-memory snapshot so the third disk
    # appears while the walk is still running.
    try:
        scan_root = STATE.get("scan_root") or ""
        if scan_root and _norm_root_str(scan_root).lower() == root_s_l:
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
        if (v.get("_lib_root") or v.get("root") or "").strip()
        and _norm_root_str(v.get("_lib_root") or v.get("root") or "").lower() == root_s_l
    ]
    if scoped:
        return scoped

    try:
        cur = STATE.get("root")
        if cur and _norm_root_str(cur).lower() == root_s_l and vids:
            foreign = [
                v for v in vids
                if (v.get("_lib_root") or "").strip()
                and _norm_root_str(v.get("_lib_root")).lower() != root_s_l
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
    try:
        key = _norm_root_str(lib)
    except Exception:
        key = lib
    key_l = key.lower()
    out = []
    for v in videos:
        r = (v.get("_lib_root") or v.get("root") or "").strip()
        if not r:
            continue
        try:
            if _norm_root_str(r).lower() == key_l:
                out.append(v)
        except Exception:
            if r.lower() == key_l:
                out.append(v)
    return out


def videos_for_scope(lib: str | None = None) -> list[dict]:
    """
    API 用片源：
    - 选中某一盘：该盘完整目录（STATE / index / disk_libs）
    - 未选盘 + 多盘：按盘合并，数量与侧栏「全部视频」合计一致
      （不能只用可能漏盘的 STATE.videos）
    - 未选盘 + 单盘：STATE.videos
    """
    lib = (lib or "").strip()
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
    state_vids = list(STATE.get("videos") or [])
    if len(roots) <= 1:
        return state_vids

    per_lists: list[tuple[str, list[dict]]] = [(r, _videos_from_root(r)) for r in roots]
    expected = sum(len(items) for _, items in per_lists)

    # STATE 已覆盖各盘则直接用（快）
    if state_vids and expected > 0:
        covered = True
        for root_s, subset in per_lists:
            if not subset:
                continue
            if len(filter_videos_by_lib(state_vids, root_s)) < len(subset):
                covered = False
                break
        if covered and len(state_vids) >= expected:
            return state_vids

    # 按盘合并，与侧栏「全部视频」合计一致（不在此写回 STATE，避免与扫描抢写）
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
    return merged or state_vids


def roots_summary(videos: list[dict] | None = None) -> list[dict]:
    """Each mount with count + per-disk channel categories.

    每盘频道独立统计，不依赖「合并后可能被冲坏」的 STATE.videos 归属。
    """
    roots = get_mounted_roots()
    out = []
    for r in roots:
        # 优先按盘取片：STATE 打标 / 刚扫完 / 盘上 index / disk_libs
        subset = _videos_from_root(r)
        if not subset and videos is not None:
            subset = filter_videos_by_lib(videos, r)
        cat_counts: dict[str, int] = {}
        for v in subset:
            cat = video_category(v)
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        out.append({
            "path": r,
            "label": root_label(r),
            "count": len(subset),
            "categories": build_category_facets(cat_counts),
        })
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

    STATE["videos"] = merged
    STATE["tree"] = tree_for_scope(None)
    rebuild_indexes(merged)
    STATE["lib_gen"] = int(STATE.get("lib_gen") or 0) + 1
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
