# -*- coding: utf-8 -*-
"""Episode / series detection and grouping."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from vg.util import format_size

# (pattern, season_group, episode_group) — season_group may be None
_EP_PATTERNS: list[tuple[re.Pattern[str], int | None, int]] = [
    (re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})", re.I), 1, 2),
    (re.compile(r"(\d{1,2})[xX](\d{1,3})"), 1, 2),
    (re.compile(r"[Ee][Pp]?(\d{1,3})\b"), None, 1),
    (re.compile(r"第\s*(\d{1,3})\s*[集话期]"), None, 1),
    (re.compile(r"[^\d](\d{2,3})(?=\.[^.]+$|$)"), None, 1),  # trailing 01/02 before ext — last resort
]


def parse_episode(name: str, folder: str = "") -> tuple[str, int, int] | None:
    """
    解析剧名 / 季 / 集。成功返回 (series_title, season, episode)，否则 None。
    """
    text = (name or "").strip()
    if not text:
        return None
    stem = Path(text).stem if "." in text and not text.startswith(".") else text

    season = 1
    episode = None
    title = stem
    matched = False

    for pat, sg, eg in _EP_PATTERNS[:-1]:  # skip trailing-number last resort first pass
        m = pat.search(stem)
        if not m:
            continue
        matched = True
        if sg is not None:
            try:
                season = int(m.group(sg))
            except (TypeError, ValueError):
                season = 1
        try:
            episode = int(m.group(eg))
        except (TypeError, ValueError):
            continue
        title = (stem[: m.start()] + " " + stem[m.end() :]).strip()
        title = re.sub(r"[\s._\-\[\]()（）]+$", "", title)
        title = re.sub(r"^[\s._\-\[\]()（）]+", "", title)
        title = re.sub(r"[\s._\-]{2,}", " ", title).strip(" .-_")
        break

    if not matched or episode is None:
        # 仅「纯数字集」且父目录像剧名时
        m2 = re.search(r"(?:^|[\s._\-])(\d{1,3})(?:[\s._\-]|$)", stem)
        folder_name = (folder or "").replace("\\", "/").rstrip("/").split("/")[-1] if folder else ""
        if m2 and folder_name and len(folder_name) >= 2:
            try:
                episode = int(m2.group(1))
            except ValueError:
                return None
            if episode <= 0 or episode > 999:
                return None
            title = folder_name
            season = 1
            matched = True
        else:
            return None

    if not title:
        folder_name = (folder or "").replace("\\", "/").rstrip("/").split("/")[-1] if folder else ""
        title = folder_name or "未命名剧集"
    if episode is None or episode <= 0:
        return None
    return title, season, episode


def series_id_for(title: str, folder: str = "", lib_root: str = "") -> str:
    # 不同挂载盘上的同名剧不要混成一部
    key = f"{(lib_root or '').casefold()}|{(folder or '').replace(chr(92), '/').strip('/').casefold()}|{title.casefold()}"
    return "s" + hashlib.md5(key.encode("utf-8")).hexdigest()[:15]


def attach_series(videos: list[dict]) -> None:
    """为条目写入 series_id / series_title / season / episode；单集不成团则清空。"""
    for v in videos:
        v.pop("series_id", None)
        v.pop("series_title", None)
        v.pop("season", None)
        v.pop("episode", None)

    groups: dict[str, list[dict]] = {}
    for v in videos:
        kind = v.get("kind") or ""
        if kind in ("m3u8", "ts_set"):
            continue
        name = v.get("name") or Path(v.get("filename") or "").stem or ""
        folder = (v.get("folder") or "").replace("\\", "/")
        parsed = parse_episode(name, folder)
        if not parsed:
            continue
        title, season, episode = parsed
        lib_root = v.get("_lib_root") or v.get("root") or ""
        sid = series_id_for(title, folder, lib_root)
        v["series_id"] = sid
        v["series_title"] = title
        v["season"] = season
        v["episode"] = episode
        groups.setdefault(sid, []).append(v)

    # 不足 2 集的不算合集
    for sid, items in groups.items():
        eps = {(it.get("season"), it.get("episode")) for it in items}
        if len(items) < 2 or len(eps) < 2:
            for it in items:
                it.pop("series_id", None)
                it.pop("series_title", None)
                it.pop("season", None)
                it.pop("episode", None)


def collapse_to_series_cards(videos: list[dict]) -> list[dict]:
    """
    将已带 series_* 的列表收成合集卡 + 未入团的单集。
    合集卡 kind=series，id=series_id，cover 用最新一集。
    """
    by_series: dict[str, list[dict]] = {}
    singles: list[dict] = []
    for v in videos:
        sid = v.get("series_id")
        if sid:
            by_series.setdefault(sid, []).append(v)
        else:
            singles.append(v)

    cards: list[dict] = []
    for sid, items in by_series.items():
        if len(items) < 2:
            singles.extend(items)
            continue
        items_sorted = sorted(
            items,
            key=lambda x: (int(x.get("season") or 0), int(x.get("episode") or 0)),
        )
        cover = items_sorted[-1]
        title = cover.get("series_title") or cover.get("name") or "剧集"
        seasons = sorted({int(x.get("season") or 1) for x in items_sorted})
        total_size = sum(int(x.get("size") or 0) for x in items_sorted)
        cards.append({
            "id": sid,
            "kind": "series",
            "name": title,
            "series_id": sid,
            "series_title": title,
            "ep_count": len(items_sorted),
            "season_count": len(seasons),
            "folder": cover.get("folder") or "",
            "ext": "",
            "size": total_size,
            "size_h": format_size(total_size),
            "mtime": max(float(x.get("mtime") or 0) for x in items_sorted),
            "mtime_h": cover.get("mtime_h") or "",
            "duration": sum(float(x.get("duration") or 0) for x in items_sorted) or None,
            "duration_h": "",
            "genres": cover.get("genres") or [],
            "themes": cover.get("themes") or [],
            "backgrounds": cover.get("backgrounds") or [],
            "taxonomy_ver": cover.get("taxonomy_ver") or 0,
            "cover_id": cover.get("id") or "",
            "has_thumb": cover.get("has_thumb"),
            "thumb_v": cover.get("thumb_v"),
            "dup": any(x.get("dup") for x in items_sorted),
            "bad": any(x.get("bad") for x in items_sorted),
            "lib_label": cover.get("lib_label") or cover.get("_lib_label") or "",
            "root": cover.get("root") or cover.get("_lib_root") or "",
        })
    return cards + singles


def series_episodes(videos: list[dict], series_id: str) -> list[dict]:
    items = [v for v in videos if (v.get("series_id") or "") == series_id]
    items.sort(key=lambda x: (int(x.get("season") or 0), int(x.get("episode") or 0), (x.get("name") or "").lower()))
    return items
