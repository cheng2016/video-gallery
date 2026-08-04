# -*- coding: utf-8 -*-
"""Genre detection from path/name."""
from __future__ import annotations

import re


from vg.config import GENRE_DEFS

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
    genres = v.get("genres")
    if isinstance(genres, list) and genres:
        return [str(g) for g in genres]
    genres = detect_genres(v.get("rel") or "", v.get("name") or "")
    v["genres"] = genres
    return genres

