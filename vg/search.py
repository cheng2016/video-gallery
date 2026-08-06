# -*- coding: utf-8 -*-
"""Search helpers: pinyin/initials, actor hints from filenames, query syntax."""
from __future__ import annotations

import re
from functools import lru_cache

from vg.config import GENRE_DEFS
from vg.util import log

# 文件名里常见噪声（分辨率/编码/站点），不当作演员
_NOISE_TOKENS = {
    "1080p", "720p", "2160p", "480p", "4k", "8k", "hdr", "sdr", "dolby", "atmos",
    "bluray", "blu-ray", "web-dl", "webrip", "hdtv", "hdrip", "dvdrip", "remux",
    "x264", "x265", "h264", "h265", "hevc", "avc", "aac", "ac3", "dts", "truehd",
    "flac", "mp3", "chs", "cht", "eng", "jpn", "kor", "sub", "chs&eng", "内嵌",
    "外挂", "中字", "简中", "繁中", "双语", "国语", "粤语", "日语", "韩语",
    "完整版", "未删减", "高清", "超清", "蓝光", "枪版", "TC", "TS", "CAM",
    "mkv", "mp4", "avi", "wmv", "flv", "mov", "m4v", "rmvb",
}
_NOISE_RE = re.compile(
    r"(?i)("
    r"\d{3,4}[pP]|4[kK]|8[kK]|UHD|HDR10?|DV|"
    r"Blu-?Ray|WEB-?DL|WEBRip|HDTV|REMUX|"
    r"[xhXH]\.?26[45]|HEVC|AVC|AAC|AC-?3|DTS(?:-HD)?|TrueHD|"
    r"简中|繁中|中字|内嵌|外挂|双语|国语|粤语|"
    r"\[.*?\]|【.*?】|\(.*?\)|（.*?）"
    r")"
)

# 演员：主演/演员/出演 后跟中文名；或点分中文短词
_ACTOR_LABEL_RE = re.compile(
    r"(?:主演|演员|出演|饰演|主演是|演员表)[:：\s\-]*([^\d\[\]【】()（）/\\|]+)",
    re.I,
)
_CN_NAME_RE = re.compile(r"[\u4e00-\u9fff]{2,4}")
_DOT_SPLIT_RE = re.compile(r"[.·・‧•＿_\-\s]+")

_GENRE_WORDS: set[str] | None = None


def _genre_words() -> set[str]:
    global _GENRE_WORDS
    if _GENRE_WORDS is None:
        words: set[str] = set()
        for name, keys in GENRE_DEFS:
            words.add(name)
            for k in keys:
                if re.fullmatch(r"[\u4e00-\u9fff]{1,6}", k):
                    words.add(k)
        _GENRE_WORDS = words
    return _GENRE_WORDS


@lru_cache(maxsize=8192)
def pinyin_blob(text: str) -> str:
    """全拼 + 首字母，便于「xingji」或「xj」搜「星际」。"""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        return ""
    try:
        full = "".join(lazy_pinyin(text, errors="ignore")).lower()
        initials = "".join(lazy_pinyin(text, style=Style.FIRST_LETTER, errors="ignore")).lower()
        spaced = " ".join(lazy_pinyin(text, errors="ignore")).lower()
        return " ".join(x for x in (full, initials, spaced) if x)
    except Exception as e:
        log(f"[搜索] 拼音失败: {e}")
        return ""


def _clean_for_actors(name: str) -> str:
    s = (name or "").strip()
    s = _NOISE_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" .-_")
    return s


def extract_actors_from_name(name: str, folder: str = "") -> list[str]:
    """
    从文件名/路径抠可能的演员名（启发式，宁缺毋滥）。
    例：
      星际穿越.马修麦康纳.安妮海瑟薇 → 马修麦康纳, 安妮海瑟薇（中英混合难抠）
      某某.张三.李四.1080P → 张三, 李四
      主演：周迅 某某剧场版 → 周迅
    """
    raw = f"{name or ''} {folder or ''}"
    found: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        t = (token or "").strip(" .-_·・‧•")
        if not t or len(t) < 2 or len(t) > 8:
            return
        low = t.casefold()
        if low in _NOISE_TOKENS or t in _genre_words():
            return
        if not _CN_NAME_RE.fullmatch(t):
            # 只要纯中文短名，避免把英文片名当演员
            return
        if t in seen:
            return
        seen.add(t)
        found.append(t)

    for m in _ACTOR_LABEL_RE.finditer(raw):
        chunk = m.group(1)
        for part in re.split(r"[,，、/|\\&\s]+", chunk):
            add(part)
            for cn in _CN_NAME_RE.findall(part):
                add(cn)

    cleaned = _clean_for_actors(name or "")
    # 点分段落：取中间偏后的中文短词（片名常在前）
    parts = [p for p in _DOT_SPLIT_RE.split(cleaned) if p]
    if len(parts) >= 2:
        # 跳过第一段（多半是片名），检查后面几段
        for p in parts[1:]:
            if _CN_NAME_RE.fullmatch(p):
                add(p)
            else:
                for cn in _CN_NAME_RE.findall(p):
                    if len(cn) >= 2:
                        add(cn)

    # 文件夹末级若是「演员名」式短中文，也收下
    folder_tail = (folder or "").replace("\\", "/").rstrip("/").split("/")[-1] if folder else ""
    if folder_tail and _CN_NAME_RE.fullmatch(folder_tail) and folder_tail not in _genre_words():
        # 仅当像人名（2~3 字更常见）且不是频道名
        if 2 <= len(folder_tail) <= 3:
            add(folder_tail)

    return found[:8]


def ensure_video_actors(v: dict) -> list[str]:
    actors = v.get("actors")
    if isinstance(actors, list):
        return [str(a) for a in actors if a]
    name = v.get("name") or ""
    folder = (v.get("folder") or "").replace("\\", "/")
    actors = extract_actors_from_name(name, folder)
    v["actors"] = actors
    return actors


def build_search_text(v: dict) -> str:
    """预计算搜索串：原名/路径 + 拼音 + 首字母 + 类型 + 演员 + 剧名。"""
    name = v.get("name") or ""
    rel = v.get("rel") or ""
    folder = (v.get("folder") or "").replace("\\", "/")
    series = v.get("series_title") or ""
    genres = v.get("genres") or []
    if not isinstance(genres, list):
        genres = []
    actors = ensure_video_actors(v)
    parts = [
        name,
        rel,
        folder,
        series,
        " ".join(str(g) for g in genres),
        " ".join(actors),
        pinyin_blob(name),
        pinyin_blob(series) if series else "",
        pinyin_blob("".join(actors)) if actors else "",
        pinyin_blob(folder.split("/")[-1] if folder else ""),
    ]
    text = " ".join(p for p in parts if p).lower()
    # 压缩空白
    text = re.sub(r"\s+", " ", text).strip()
    return text


_TOKEN_RE = re.compile(
    r"""
    (?P<field>ext|type|genre|g|actor|a|cast|lib|disk|channel|cat)
    :
    (?P<value>"[^"]+"|'[^']+'|[^\s]+)
    |
    (?P<plain>\S+)
    """,
    re.I | re.X,
)


def parse_search_query(q: str) -> dict:
    """
    解析搜索语法：
      星际穿越          → 纯文本（拼音/原文）
      ext:mkv           → 仅 mkv
      type:mp4          → 同 ext
      genre:动作 / g:科幻
      actor:周迅 / a:张三
      channel:电影 / cat:动漫
      多条件空格分隔，纯文本多项需同时命中（AND）
    """
    q = (q or "").strip()
    out: dict = {
        "terms": [],
        "ext": "",
        "genre": "",
        "actor": "",
        "category": "",
        "raw": q,
    }
    if not q:
        return out

    for m in _TOKEN_RE.finditer(q):
        if m.group("field"):
            field = m.group("field").lower()
            val = (m.group("value") or "").strip().strip("\"'")
            if not val:
                continue
            if field in ("ext", "type"):
                ext = val.lower()
                if not ext.startswith("."):
                    ext = "." + ext
                out["ext"] = ext
            elif field in ("genre", "g"):
                out["genre"] = val
            elif field in ("actor", "a", "cast"):
                out["actor"] = val
            elif field in ("channel", "cat"):
                out["category"] = val
            elif field in ("lib", "disk"):
                out["lib_hint"] = val
        else:
            plain = (m.group("plain") or "").strip()
            if not plain:
                continue
            if re.match(r"^(ext|type|genre|g|actor|a|cast|lib|disk|channel|cat):", plain, re.I):
                continue
            out["terms"].append(plain.lower())
    return out


def video_matches_query(v: dict, parsed: dict, search_text_fn) -> bool:
    """按 parse_search_query 结果匹配单条视频。"""
    if not parsed or not parsed.get("raw"):
        return True

    if parsed.get("ext"):
        if (v.get("ext") or "").lower() != parsed["ext"]:
            return False

    if parsed.get("genre"):
        from vg.genres import ensure_video_genres
        g = parsed["genre"]
        genres = ensure_video_genres(v)
        if not any(g.lower() in str(x).lower() for x in genres):
            return False

    if parsed.get("actor"):
        actors = ensure_video_actors(v)
        a = parsed["actor"].lower()
        blob = " ".join(actors).lower() + " " + search_text_fn(v)
        if a not in blob:
            return False

    if parsed.get("category"):
        from vg.catalog import video_category
        cat = video_category(v) or ""
        if parsed["category"].lower() not in cat.lower():
            return False

    if parsed.get("lib_hint"):
        hint = str(parsed["lib_hint"]).lower()
        lab = (v.get("lib_label") or v.get("_lib_label") or v.get("root") or "").lower()
        if hint not in lab:
            return False

    text = search_text_fn(v)
    for term in parsed.get("terms") or []:
        if term not in text:
            return False
    return True
