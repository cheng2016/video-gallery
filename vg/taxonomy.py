# -*- coding: utf-8 -*-
"""Independent theme/background inference from paths and filenames.

Folder channels remain owned by ``catalog.video_category``.  This module only
adds optional facets alongside that navigation and never rewrites ``folder``.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import PurePath

TAXONOMY_VERSION = 1

# Broad content subjects.  Keep this intentionally smaller and more stable
# than the legacy genre vocabulary so the right-side filter remains useful.
THEME_DEFS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("动作", ("动作", "武打", "打斗", "搏击", "action")),
    ("喜剧", ("喜剧", "搞笑", "幽默", "comedy")),
    ("爱情", ("爱情", "恋爱", "浪漫", "言情", "romance", "love")),
    ("剧情", ("剧情", "文艺", "drama")),
    ("科幻", ("科幻", "星际", "太空", "宇宙", "science fiction", "sci-fi", "scifi")),
    ("奇幻", ("奇幻", "魔幻", "魔法", "fantasy")),
    ("悬疑", ("悬疑", "推理", "侦探", "mystery", "detective")),
    ("恐怖", ("恐怖", "灵异", "鬼片", "horror")),
    ("犯罪", ("犯罪", "警匪", "刑侦", "黑帮", "crime", "gangster")),
    ("战争", ("战争", "战火", "抗战", "二战", "军事", "war")),
    ("冒险", ("冒险", "探险", "adventure")),
    ("武侠", ("武侠", "仙侠", "功夫", "江湖", "wuxia", "kung fu", "kungfu")),
    ("动画", ("动画", "动漫", "卡通", "animation", "anime")),
    ("纪录片", ("纪录片", "纪实", "documentary")),
    ("家庭", ("家庭", "亲情", "family")),
    ("儿童", ("儿童", "少儿", "kids")),
    ("音乐", ("音乐", "歌舞", "演唱会", "music", "musical")),
    ("运动", ("运动", "体育", "足球", "篮球", "赛车", "sports")),
    ("历史", ("历史", "传记", "年代", "history", "biography")),
    ("灾难", ("灾难", "末日", "disaster", "apocalypse")),
    ("励志", ("励志", "奋斗", "inspirational")),
)

# Narrative settings/contexts, deliberately separate from content subjects.
BACKGROUND_DEFS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("校园", ("校园", "学校", "学生", "school", "campus")),
    ("职场", ("职场", "办公室", "商战", "workplace", "office")),
    ("都市", ("都市", "城市", "urban", "city")),
    ("乡村", ("农村", "乡村", "乡土", "village", "rural")),
    ("医疗", ("医院", "医生", "医疗", "hospital", "medical")),
    ("宫廷", ("宫廷", "宫斗", "后宫", "皇宫", "palace")),
    ("古代", ("古装", "古代", "朝代", "costume", "period drama")),
    ("警务", ("警察", "警匪", "公安", "刑侦", "police")),
    ("军旅", ("军旅", "军事", "部队", "战场", "military")),
    ("西部", ("西部", "牛仔", "western")),
    ("公路", ("公路", "自驾", "road trip")),
    ("旅行", ("旅行", "旅游", "travel")),
    ("自然", ("自然", "动物", "野生", "nature", "wildlife")),
    ("太空", ("星际", "太空", "宇宙", "space")),
    ("未来", ("未来", "赛博朋克", "future", "futuristic", "cyberpunk")),
    ("末日", ("末日", "丧尸", "僵尸", "apocalypse", "zombie")),
    ("海洋", ("海洋", "海岛", "水下", "ocean", "underwater")),
)

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)?")


def _norm(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").casefold().strip()


def _score_keyword(
    keyword: str,
    *,
    filename: str,
    folder_segments: set[str],
    text: str,
    tokens: set[str],
) -> int:
    key = _norm(keyword)
    if not key:
        return 0
    if key in folder_segments:
        return 5
    if key.isascii():
        if " " in key:
            return 4 if re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", text) else 0
        return 4 if key in tokens else 0
    if key in filename:
        return 4
    if any(key in segment for segment in folder_segments):
        return 3
    return 0


def _detect_axis(
    definitions: tuple[tuple[str, tuple[str, ...]], ...],
    rel: str,
    name: str,
    *,
    limit: int,
) -> list[str]:
    normalized_rel = _norm((rel or "").replace("\\", "/"))
    filename = _norm(name or PurePath(normalized_rel).stem)
    parts = [part for part in normalized_rel.split("/") if part]
    folder_segments = set(parts[:-1])
    text = f"{' '.join(parts)} {filename}"
    tokens = set(_TOKEN_RE.findall(text))
    scored: list[tuple[int, int, str]] = []
    for order, (label, keywords) in enumerate(definitions):
        score = max(
            (
                _score_keyword(
                    keyword,
                    filename=filename,
                    folder_segments=folder_segments,
                    text=text,
                    tokens=tokens,
                )
                for keyword in keywords
            ),
            default=0,
        )
        if score >= 3:
            scored.append((-score, order, label))
    scored.sort()
    return [label for _, _, label in scored[:limit]]


def classify_video_taxonomy(rel: str, name: str = "") -> tuple[list[str], list[str]]:
    """Infer independent themes and backgrounds without changing folders."""
    themes = _detect_axis(THEME_DEFS, rel, name, limit=4)
    backgrounds = _detect_axis(BACKGROUND_DEFS, rel, name, limit=3)
    return themes, backgrounds


def ensure_video_taxonomy(video: dict) -> tuple[list[str], list[str]]:
    """Return current taxonomy, refreshing old index rows when rules change."""
    themes = video.get("themes")
    backgrounds = video.get("backgrounds")
    try:
        version = int(video.get("taxonomy_ver") or 0)
    except (TypeError, ValueError):
        version = 0
    if (
        version == TAXONOMY_VERSION
        and isinstance(themes, list)
        and isinstance(backgrounds, list)
    ):
        return [str(value) for value in themes if value], [
            str(value) for value in backgrounds if value
        ]
    themes, backgrounds = classify_video_taxonomy(
        video.get("rel") or "",
        video.get("name") or "",
    )
    video["themes"] = themes
    video["backgrounds"] = backgrounds
    video["taxonomy_ver"] = TAXONOMY_VERSION
    video.pop("_q", None)
    return themes, backgrounds


def taxonomy_facets(videos: list[dict], axis: str) -> list[dict]:
    """Count one taxonomy axis using its stable display order."""
    field = "backgrounds" if axis == "backgrounds" else "themes"
    definitions = BACKGROUND_DEFS if field == "backgrounds" else THEME_DEFS
    counts: dict[str, int] = {}
    for video in videos:
        themes, backgrounds = ensure_video_taxonomy(video)
        values = backgrounds if field == "backgrounds" else themes
        for value in values:
            counts[value] = counts.get(value, 0) + 1
    order = {name: index for index, (name, _) in enumerate(definitions)}
    return [
        {"id": name, "name": name, "count": count}
        for name, count in sorted(
            counts.items(),
            key=lambda item: (order.get(item[0], 999), -item[1], item[0]),
        )
        if count > 0
    ]


def taxonomy_facets_pair(videos: list[dict]) -> tuple[list[dict], list[dict]]:
    """Count both taxonomy axes in a single pass.

    The previous ``_build_tree_payload`` flow called
    ``taxonomy_facets(videos, "themes")`` and
    ``taxonomy_facets(videos, "backgrounds")`` separately, each iterating
    the entire video list and calling ``ensure_video_taxonomy``.

    This folded single pass also emits a structured PERF line
    ``taxonomy_facets_pair_detail`` that splits the total time into:
      - ``cold``  : videos whose taxonomy_ver != TAXONOMY_VERSION (need
                    ``classify_video_taxonomy`` regex matching)
      - ``hot``   : videos already classified (read cached fields only)
      - ``classify_ms`` : time spent inside classify_video_taxonomy (cold)
      - ``iterate_ms``  : time spent counting + reading cached fields (hot)

    This lets the log tell whether a slow ``facets_ms`` is dominated by
    cold classification (one-time cost, expected on first tree_build) or
    by per-item overhead even when everything is cached (would indicate
    a real algorithmic problem worth optimising).
    """
    import time
    from vg.diagnostics import emit

    t_start = time.perf_counter()
    theme_counts: dict[str, int] = {}
    bg_counts: dict[str, int] = {}
    cold_count = 0
    hot_count = 0
    classify_ms = 0.0
    for video in videos:
        try:
            already = (
                int(video.get("taxonomy_ver") or 0) == TAXONOMY_VERSION
                and isinstance(video.get("themes"), list)
                and isinstance(video.get("backgrounds"), list)
            )
        except (TypeError, ValueError):
            already = False
        if already:
            hot_count += 1
            themes = video["themes"]
            backgrounds = video["backgrounds"]
        else:
            cold_count += 1
            tc = time.perf_counter()
            themes, backgrounds = classify_video_taxonomy(
                video.get("rel") or "",
                video.get("name") or "",
            )
            classify_ms += (time.perf_counter() - tc) * 1000.0
            video["themes"] = themes
            video["backgrounds"] = backgrounds
            video["taxonomy_ver"] = TAXONOMY_VERSION
        for value in themes:
            theme_counts[value] = theme_counts.get(value, 0) + 1
        for value in backgrounds:
            bg_counts[value] = bg_counts.get(value, 0) + 1
    iterate_ms = (time.perf_counter() - t_start) * 1000.0 - classify_ms
    emit(
        "PERF",
        "taxonomy_facets_pair_detail",
        force=True,
        videos=len(videos),
        cold=cold_count,
        hot=hot_count,
        classify_ms=f"{classify_ms:.1f}",
        iterate_ms=f"{iterate_ms:.1f}",
        total_ms=f"{classify_ms + iterate_ms:.1f}",
    )
    theme_order = {name: index for index, (name, _) in enumerate(THEME_DEFS)}
    bg_order = {name: index for index, (name, _) in enumerate(BACKGROUND_DEFS)}
    themes_facets = [
        {"id": name, "name": name, "count": count}
        for name, count in sorted(
            theme_counts.items(),
            key=lambda item: (theme_order.get(item[0], 999), -item[1], item[0]),
        )
        if count > 0
    ]
    backgrounds_facets = [
        {"id": name, "name": name, "count": count}
        for name, count in sorted(
            bg_counts.items(),
            key=lambda item: (bg_order.get(item[0], 999), -item[1], item[0]),
        )
        if count > 0
    ]
    return themes_facets, backgrounds_facets
