# -*- coding: utf-8 -*-
"""Constants and paths for video gallery."""
from __future__ import annotations

import sys
from pathlib import Path


def _bundle_dir() -> Path:
    """只读资源目录（templates 等）。frozen 时为 PyInstaller _MEIPASS。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent.parent


def _writable_root() -> Path:
    """可写根目录（preview_cache）。frozen 时为 exe 同目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".ts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".rmvb", ".rm",
}

SEGMENT_EXTS = {".ts", ".m2ts"}
PLAYLIST_EXTS = {".m3u8"}
SEGMENT_FOLDER_GENERIC = {
    "ts", "m2ts", "video", "videos", "stream", "streams",
    "hls", "media", "data", "video_ts", "bdmv",
}
STANDALONE_TS_MIN_BYTES = 50 * 1024 * 1024
MIN_VIDEO_FILE_BYTES = 100 * 1024
MIN_SEGMENT_FILE_BYTES = 1024

BROWSER_FRIENDLY_EXTS = {".mp4", ".webm", ".m4v", ".mov"}
BROWSER_HARD_EXTS = {".mkv", ".avi", ".wmv", ".flv", ".rmvb", ".rm", ".ts", ".m2ts", ".mpg", ".mpeg"}
BROWSER_FRIENDLY_AUDIO = {"aac", "mp3", "opus", "vorbis"}
PROBE_META_VER = 2

GENRE_DEFS: list[tuple[str, tuple[str, ...]]] = [
    ("动作", ("动作", "action", "武打", "打斗", "搏击")),
    ("喜剧", ("喜剧", "搞笑", "欢喜", "comedy", "幽默")),
    ("爱情", ("爱情", "恋爱", "浪漫", "言情", "romance", "love")),
    ("剧情", ("剧情", "drama", "文艺")),
    ("科幻", ("科幻", "sci-fi", "scifi", "science fiction", "星际", "太空", "未来")),
    ("恐怖", ("恐怖", "惊悚", "horror", "鬼片", "灵异")),
    ("悬疑", ("悬疑", "推理", "mystery", "侦探", "detective")),
    ("犯罪", ("犯罪", "crime", "黑帮", "黑道", "gangster")),
    ("警匪", ("警匪", "警察", "公安", "刑侦", "police")),
    ("枪战", ("枪战", "shooter", "枪火")),
    ("战争", ("战争", "战火", "抗战", "二战", "war", "军事")),
    ("冒险", ("冒险", "adventure", "探险")),
    ("奇幻", ("奇幻", "魔幻", "fantasy", "魔法")),
    ("玄幻", ("玄幻", "修真", "修仙")),
    ("仙侠", ("仙侠", "仙侠剧")),
    ("武侠", ("武侠", "江湖", "wuxia")),
    ("功夫", ("功夫", "kungfu", "kung fu", "咏春", "截拳")),
    ("动画", ("动画", "动漫", "animation", "anime", "卡通")),
    ("灾难", ("灾难", "disaster", "末日", "apocalypse", "丧尸", "zombie")),
    ("青春", ("青春", "校园", "youth", "少年", "少女")),
    ("古装", ("古装", "古代", "朝代", "costume")),
    ("历史", ("历史", "history", "年代", "传记", "biography")),
    ("宫廷", ("宫廷", "宫斗", "后宫", "穿越")),
    ("家庭", ("家庭", "亲情", "family", "伦理")),
    ("儿童", ("儿童", "少儿", "kids", "卡通片")),
    ("运动", ("运动", "体育", "sports", "足球", "篮球", "赛车")),
    ("音乐", ("音乐", "歌舞", "musical", "演唱会")),
    ("西部", ("西部", "western", "牛仔")),
    ("黑色", ("黑色电影", "noir", "暗黑", "黑色幽默")),
    ("纪录片", ("纪录片", "documentary", "纪实")),
    ("短片", ("短片", "short", "微电影")),
    ("同性", ("同性", "lgbt", "百合", "耽美")),
    ("医疗", ("医疗", "医院", "医生", "medical")),
    ("律政", ("律政", "律师", "法庭", "legal")),
    ("职场", ("职场", "商战", "workplace")),
    ("都市", ("都市", "城市", "urban")),
    ("农村", ("农村", "乡村", "乡土")),
    ("谍战", ("谍战", "间谍", "特务", "spy")),
    ("僵尸", ("僵尸", "vampire", "吸血鬼")),
    ("超级英雄", ("超级英雄", "超英", "漫威", "superhero", "英雄")),
    ("机器人", ("机器人", "机甲", "robot", "mecha")),
    ("公路", ("公路", "road trip", "自驾")),
    ("美食", ("美食", "料理", "food", "烹饪")),
    ("旅行", ("旅行", "旅游", "travel")),
    ("自然", ("自然", "动物", "nature", "野生")),
    ("神话", ("神话", "传说", "神话故事", "myth")),
    ("童话", ("童话", "fairy")),
    ("励志", ("励志", "奋斗")),
    ("治愈", ("治愈", "温馨", "healing")),
    ("惊悚", ("thriller", "紧张刺激")),
    ("恐怖喜剧", ("恐喜", "恐怖喜剧")),
    ("动作喜剧", ("动作喜剧", "喜动作")),
    ("爱情喜剧", ("爱情喜剧", "浪漫喜剧", "romcom")),
    ("犯罪悬疑", ("犯罪悬疑",)),
    ("科幻动作", ("科幻动作",)),
    ("武侠仙侠", ("武侠仙侠",)),
    ("戏曲", ("戏曲", "京剧", "越剧", "黄梅戏")),
    ("脱口秀", ("脱口秀", "相声", "小品", "standup")),
    ("真人秀", ("真人秀", "reality")),
    ("晚会", ("晚会", "春晚", "庆典")),
]

THUMB_DIR_NAME = ".video_gallery_cache"
LEGACY_DISK_CACHE_NAMES = (
    ".video_gallery_cache",
    "video_gallery_cache",
    ".vgdata",
)
INDEX_NAME = "index.json"
APP_DIR = _bundle_dir()
WRITABLE_ROOT = _writable_root()
VGDATA_DIR = WRITABLE_ROOT / "preview_cache"
KEY_FILE = VGDATA_DIR / "vault.key"
PREFS_FILE = VGDATA_DIR / "prefs.json"
THUMB_EXT = ".vgt"
# Background thumbnail extraction is I/O heavy and each ffmpeg process may
# otherwise fan out to several decoder threads.  Keep enough parallelism to
# make progress without starving the web UI or video playback.
THUMB_WORKERS_MAX = 2
# First-scan burst: one ffmpeg process per logical CPU (each already uses
# -threads 1).  0 means "use os.cpu_count()".
THUMB_WORKERS_BURST = 0
THUMB_JPEG_CACHE_MAX = 256
# 转码/修声音同时跑的任务数（1=最稳，不打满 CPU；可在 prefs 覆盖）
CONVERT_MAX_PARALLEL = 1

SKIP_DIR_NAMES = {
    "$recycle.bin", "system volume information", "recovery",
    "windows", "program files", "program files (x86)", "programdata",
    "perflogs", "msocache", "documents and settings",
    "boot", "efi", "config.msi", "system", "sysvol",
    "appdata", "application data", "local settings",
    "android", ".android", "emulator", "intel", "nvidia", "amd",
    "node_modules", "__pycache__", ".venv", "venv",
    THUMB_DIR_NAME, "video_gallery_cache", "preview_cache",
    "_video_gallery_static", ".git", ".svn",
}

STATIC_EXPORT_DIRNAME = "_video_gallery_static"
