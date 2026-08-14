# -*- coding: utf-8 -*-
"""HLS/m3u8 and TS-set collapse helpers."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from vg.cache import thumb_file_ready
from vg.config import (
    SEGMENT_EXTS,
    SEGMENT_FOLDER_GENERIC,
    STANDALONE_TS_MIN_BYTES,
    THUMB_EXT,
)
from vg.genres import detect_genres
from vg.state import STATE
from vg.taxonomy import TAXONOMY_VERSION, classify_video_taxonomy
from vg.util import format_size, natural_sort_key, video_id

def _ts_set_display_name(folder: str, items: list[dict]) -> str:
    """用文件夹名作为入口名；目录名太泛（如 ts）则用上一级。"""
    parts = [p for p in (folder or "").split("/") if p]
    name = parts[-1] if parts else ""
    if name.lower() in SEGMENT_FOLDER_GENERIC and len(parts) >= 2:
        name = parts[-2]
    if name:
        return name
    # 取文件名公共前缀
    stems = [Path(i.get("filename") or i.get("name") or "").stem for i in items]
    stems = [s for s in stems if s]
    if not stems:
        return "视频流"
    prefix = stems[0]
    for s in stems[1:]:
        while prefix and not s.startswith(prefix):
            prefix = prefix[:-1]
        if not prefix:
            break
    prefix = prefix.rstrip("._- ")
    return prefix or stems[0]


def make_ts_set(folder: str, items: list[dict]) -> dict:
    items = sorted(items, key=lambda x: natural_sort_key(x.get("filename") or x.get("name") or ""))
    first = items[0]
    total_size = sum(int(i.get("size") or 0) for i in items)
    mtime = max(float(i.get("mtime") or 0) for i in items)
    segments = [i["rel"] for i in items if i.get("rel")]
    set_key = f"__ts_set__/{folder or '_root_'}"
    vid = video_id(set_key)
    name = _ts_set_display_name(folder, items)
    themes, backgrounds = classify_video_taxonomy(folder + "/" + name, name)
    return {
        "id": vid,
        "name": name,
        "filename": first.get("filename") or "",
        "rel": first["rel"],
        "folder": folder,
        "ext": first.get("ext") or ".ts",
        "size": total_size,
        "size_h": format_size(total_size),
        "mtime": mtime,
        "mtime_h": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime else "",
        "duration": None,
        "duration_h": "",
        "thumb": f"{vid}{THUMB_EXT}",
        "has_thumb": False,
        "genres": detect_genres(folder + "/" + name, name),
        "themes": themes,
        "backgrounds": backgrounds,
        "taxonomy_ver": TAXONOMY_VERSION,
        "kind": "ts_set",
        "segments": segments,
        "seg_count": len(segments),
    }


def make_m3u8_entry(item: dict) -> dict:
    """把扫描到的 m3u8 规范成播放入口。"""
    folder = (item.get("folder") or "").strip("/")
    name = item.get("name") or Path(item.get("filename") or "playlist").stem
    # 目录名更可读时用目录名
    disp = _ts_set_display_name(folder, [item])
    if disp and disp.lower() not in {"index", "playlist", "master", "video"}:
        name = disp
    vid = item.get("id") or video_id(item.get("rel") or "")
    themes, backgrounds = classify_video_taxonomy(item.get("rel") or "", name)
    return {
        "id": vid,
        "name": name,
        "filename": item.get("filename") or "",
        "rel": item["rel"],
        "folder": folder,
        "ext": ".m3u8",
        "size": int(item.get("size") or 0),
        "size_h": item.get("size_h") or format_size(int(item.get("size") or 0)),
        "mtime": item.get("mtime") or 0,
        "mtime_h": item.get("mtime_h") or "",
        "duration": None,
        "duration_h": "",
        "thumb": f"{vid}{THUMB_EXT}",
        "has_thumb": bool(item.get("has_thumb")),
        "genres": item.get("genres") or detect_genres(item.get("rel") or "", name),
        "themes": item.get("themes") or themes,
        "backgrounds": item.get("backgrounds") or backgrounds,
        "taxonomy_ver": TAXONOMY_VERSION,
        "kind": "m3u8",
        "seg_count": 0,
    }


def _pick_preferred_m3u8(items: list[dict]) -> dict:
    prefer = ("index.m3u8", "playlist.m3u8", "master.m3u8", "video.m3u8")
    by_name = {(i.get("filename") or "").lower(): i for i in items}
    for name in prefer:
        if name in by_name:
            return by_name[name]
    return sorted(items, key=lambda x: (x.get("filename") or "").lower())[0]


def _normalize_playlist_rel(base_dir: str, uri: str) -> str:
    """把 m3u8 内相对 URI 解析为相对扫描根的路径（与播放代理规则一致）。"""
    uri = (uri or "").split("#")[0].split("?")[0].replace("\\", "/").strip()
    if not uri or re.match(r"https?://", uri, re.I) or re.match(r"^[a-zA-Z]:/", uri):
        return ""
    base_dir = (base_dir or "").replace("\\", "/").strip("/")
    if uri.startswith("/"):
        parts = [p for p in uri.split("/") if p and p != "."]
    else:
        parts = [p for p in (f"{base_dir}/{uri}" if base_dir else uri).split("/") if p and p != "."]
    out: list[str] = []
    for p in parts:
        if p == "..":
            if out:
                out.pop()
            continue
        out.append(p)
    return "/".join(out)


def _iter_m3u8_uris(text: str) -> list[str]:
    """取出 m3u8 中的媒体/子列表 URI（普通行 + URI=\"...\" 属性）。"""
    uris: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            for m in re.finditer(r'\bURI="([^"]+)"', s, re.I):
                uris.append(m.group(1))
            continue
        uris.append(s)
    return uris


def collect_playlist_media_rels(
    playlist_rel: str,
    root: Path | None = None,
    _seen: set[str] | None = None,
    _nested_playlists: set[str] | None = None,
) -> set[str]:
    """
    解析 m3u8（含 master 嵌套子列表），返回其中引用到的 .ts/.m2ts 相对路径集合。
    只有这些文件才应视为「播放流分片」并隐藏；同目录其它大 TS 仍可单独展示。
    若传入 _nested_playlists，会一并收集被引用的子 .m3u8 路径。
    """
    root = root or STATE.get("root")
    playlist_rel = (playlist_rel or "").replace("\\", "/").strip("/")
    if not root or not playlist_rel:
        return set()
    seen = _seen if _seen is not None else set()
    nested = _nested_playlists if _nested_playlists is not None else set()
    if playlist_rel in seen:
        return set()
    seen.add(playlist_rel)

    path = Path(root) / playlist_rel
    try:
        if not path.is_file():
            return set()
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return set()

    media: set[str] = set()
    base_dir = str(Path(playlist_rel).parent).replace("\\", "/")
    if base_dir == ".":
        base_dir = ""

    for uri in _iter_m3u8_uris(text):
        seg_rel = _normalize_playlist_rel(base_dir, uri)
        if not seg_rel:
            continue
        ext = Path(seg_rel).suffix.lower()
        if ext == ".m3u8":
            nested.add(seg_rel)
            media |= collect_playlist_media_rels(seg_rel, root, seen, nested)
            continue
        if ext in SEGMENT_EXTS:
            media.add(seg_rel)
            continue
        # 无扩展名或非常规后缀：若磁盘上确是分片也收入
        try:
            cand = Path(root) / seg_rel
            if cand.is_file():
                cext = cand.suffix.lower()
                if cext == ".m3u8":
                    nested.add(seg_rel)
                    media |= collect_playlist_media_rels(seg_rel, root, seen, nested)
                elif cext in SEGMENT_EXTS:
                    media.add(seg_rel)
        except OSError:
            pass
    return media


def _segment_file_size(it: dict) -> int:
    """取 TS 体积；条目缺 size 时回落读盘（用于拆开误合并的 ts_set）。"""
    sz = int(it.get("size") or 0)
    if sz > 0:
        return sz
    rel = (it.get("rel") or "").replace("\\", "/")
    root = STATE.get("root")
    if not rel or not root:
        return 0
    try:
        p = Path(root) / rel
        if p.is_file():
            return int(p.stat().st_size)
    except OSError:
        return 0
    return 0


def _materialize_standalone_ts(it: dict, folder: str) -> dict:
    """把单片 TS（含从 ts_set 拆出的 stub）补成可展示的完整条目。"""
    rel = (it.get("rel") or "").replace("\\", "/")
    if (
        it.get("kind") != "ts_set"
        and it.get("id")
        and rel
        and int(it.get("size") or 0) > 0
        and (it.get("ext") or "").lower() in SEGMENT_EXTS
    ):
        out = {k: v for k, v in it.items() if k not in ("segments", "seg_count")}
        out.pop("kind", None)
        return out

    size = _segment_file_size(it)
    mtime = float(it.get("mtime") or 0)
    root = STATE.get("root")
    if root and rel:
        try:
            p = Path(root) / rel
            if p.is_file():
                st = p.stat()
                if not size:
                    size = int(st.st_size)
                if not mtime:
                    mtime = float(st.st_mtime)
        except OSError:
            pass
    name = it.get("name") or Path(rel).stem or "视频"
    filename = it.get("filename") or Path(rel).name
    ext = (it.get("ext") or Path(rel).suffix or ".ts").lower()
    vid = video_id(rel) if rel else (it.get("id") or video_id(f"{folder}/{filename}"))
    cache = STATE.get("cache_dir")
    return {
        "id": vid,
        "name": name,
        "filename": filename,
        "rel": rel,
        "folder": folder,
        "ext": ext,
        "size": size,
        "size_h": format_size(size),
        "mtime": mtime,
        "mtime_h": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime else "",
        "duration": None,
        "duration_h": "",
        "thumb": f"{vid}{THUMB_EXT}",
        "has_thumb": thumb_file_ready(cache, vid) if cache else False,
        "genres": it.get("genres") or detect_genres(rel, name),
    }


def collapse_segment_sets(videos: list[dict]) -> list[dict]:
    """
    1) 保留 m3u8 入口；解析列表内容，仅隐藏其中引用到的 .ts/.m2ts
    2) 未被任何 m3u8 引用的小体积多段 TS → 合成 ts_set
    3) 大体积（≥ STANDALONE_TS_MIN_BYTES）或未被引用的单片 → 独立视频
    """
    kept: list[dict] = []
    by_folder_seg: dict[str, list[dict]] = {}
    by_folder_m3u8: dict[str, list[dict]] = {}

    for v in videos:
        kind = v.get("kind") or ""
        ext = (v.get("ext") or "").lower()
        folder = (v.get("folder") or "").strip("/")

        if kind == "m3u8" or ext == ".m3u8":
            by_folder_m3u8.setdefault(folder, []).append(v)
            continue
        if kind == "ts_set" and len(v.get("segments") or []) >= 2:
            by_folder_seg.setdefault(folder, []).append(v)
            continue
        if ext in SEGMENT_EXTS:
            by_folder_seg.setdefault(folder, []).append(v)
            continue
        kept.append(v)

    root = STATE.get("root")
    referenced_segs: set[str] = set()
    nested_playlists: set[str] = set()

    # 先解析全部 m3u8，再决定保留哪些入口 / 隐藏哪些分片
    for _folder, items in by_folder_m3u8.items():
        for it in items:
            rel = (it.get("rel") or "").replace("\\", "/").strip("/")
            if rel:
                referenced_segs |= collect_playlist_media_rels(
                    rel, root, _nested_playlists=nested_playlists
                )

    for folder, items in by_folder_m3u8.items():
        ready = [x for x in items if x.get("kind") == "m3u8" and x.get("rel")]
        if ready:
            pick = _pick_preferred_m3u8(ready)
        else:
            pick = make_m3u8_entry(_pick_preferred_m3u8(items))
        if pick.get("kind") != "m3u8":
            pick = make_m3u8_entry(pick)
        pick_rel = (pick.get("rel") or "").replace("\\", "/").strip("/")
        # 已被其它 master 引用的子列表：不单独占一个入口
        if pick_rel and pick_rel in nested_playlists:
            continue
        kept.append(pick)

    for folder, items in by_folder_seg.items():
        flat: list[dict] = []
        for it in items:
            if it.get("kind") == "ts_set" and it.get("segments"):
                for rel in it["segments"]:
                    flat.append({
                        "rel": rel,
                        "filename": Path(rel).name,
                        "name": Path(rel).stem,
                        "ext": Path(rel).suffix.lower(),
                        "size": 0,
                        "mtime": it.get("mtime") or 0,
                    })
            else:
                flat.append(it)

        # 播放列表已引用的分片：隐藏；其余再按体积决定单片 / 合集
        candidates: list[dict] = []
        for it in flat:
            rel = (it.get("rel") or "").replace("\\", "/").strip("/")
            if rel and rel in referenced_segs:
                continue
            candidates.append(it)

        standalone: list[dict] = []
        small: list[dict] = []
        for it in candidates:
            if _segment_file_size(it) >= STANDALONE_TS_MIN_BYTES:
                standalone.append(it)
            else:
                small.append(it)

        for it in standalone:
            kept.append(_materialize_standalone_ts(it, folder))

        if len(small) >= 2:
            kept.append(make_ts_set(folder, small))
        elif len(small) == 1:
            kept.append(_materialize_standalone_ts(small[0], folder))

    kept.sort(
        key=lambda x: (
            (x.get("folder") or "").lower(),
            (x.get("name") or "").lower(),
        )
    )
    return kept

