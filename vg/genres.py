# -*- coding: utf-8 -*-
"""Genre detection from path/name."""
from __future__ import annotations

import re


from vg.config import GENRE_DEFS

# Bump when GENRE_DEFS / detect_genres rules change so cached empty results
# get invalidated.  Mirrors the ``taxonomy_ver`` mechanism in taxonomy.py.
# Without this, ``ensure_video_genres`` treated an empty ``genres=[]`` as a
# cache miss (because ``[]`` is falsy) and re-ran ``detect_genres`` on every
# call — observed as ``genre_ms=971.6 genre_misses=2526`` in tree_build for a
# 2785-video catalog where 2526 videos had no genre keyword matches.
GENRES_VERSION = 1


def detect_genres(rel: str, name: str = "") -> list[str]:
    """从相对路径、文件夹、文件名识别类型（可多选）。英文词要求整词匹配，避免误伤。"""
    text_raw = f"{rel} {name}".replace("\\", "/")
    text = text_raw.lower()
    # 用非字母数字切开，便于英文整词匹配
    tokens = set(re.findall(r"[a-z0-9]+", text))
    hit: list[str] = []
    for genre, keys in GENRE_DEFS:
        for k in keys:
            if not k:
                continue
            if k.isascii():
                kl = k.lower()
                if " " in kl:
                    if kl in text:
                        hit.append(genre)
                        break
                elif len(kl) <= 3:
                    if kl in tokens:
                        hit.append(genre)
                        break
                else:
                    if kl in tokens or re.search(rf"(?<![a-z0-9]){re.escape(kl)}(?![a-z0-9])", text):
                        hit.append(genre)
                        break
            else:
                if k in text_raw:
                    hit.append(genre)
                    break
    seen: set[str] = set()
    out: list[str] = []
    for g in hit:
        if g not in seen:
            seen.add(g)
            out.append(g)
    return out


def ensure_video_genres(v: dict) -> list[str]:
    """Return cached genres, classifying once and caching even empty results.

    Uses ``genres_ver`` to invalidate when GENRE_DEFS rules change, mirroring
    ``taxonomy_ver``.  The previous form treated an empty ``genres=[]`` as a
    cache miss (``isinstance(genres, list) and genres`` is False for ``[]``)
    and re-ran ``detect_genres`` on every call.  For a 2785-video catalog
    where ~90% of videos have no genre keyword in their path, this made
    ``ensure_video_genres`` the dominant cost in tree_build:

        [PERF] tree_build_facets_breakdown ...
            loop_ms=987.7 genre_ms=971.6 cat_ms=6.5
            genre_hits=259 genre_misses=2526 taxonomy_ms=3.7

    With versioned caching, the 2526 empty-genre videos are served from the
    cached ``genres=[]`` field on subsequent calls (genre_hits→2785,
    genre_ms→~5ms).
    """
    try:
        if int(v.get("genres_ver") or 0) == GENRES_VERSION:
            genres = v.get("genres")
            if isinstance(genres, list):
                return [str(g) for g in genres]
    except (TypeError, ValueError):
        pass
    genres = detect_genres(v.get("rel") or "", v.get("name") or "")
    v["genres"] = genres
    v["genres_ver"] = GENRES_VERSION
    v.pop("_q", None)
    v.pop("_q_sig", None)
    return genres

