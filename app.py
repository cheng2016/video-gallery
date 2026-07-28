#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地视频库：扫描目录 → 生成预览图 → 浏览器分类浏览 / 播放
用法:
  python app.py                 # 默认扫描「最后一个盘」
  python app.py "D:\\Videos"    # 指定目录
  python app.py --port 8765
  python app.py --no-thumbs     # 跳过缩略图（更快）
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import string
import subprocess
import sys
import threading
import webbrowser
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

try:
    from flask import Flask, Response, abort, jsonify, render_template, request, send_file
except ImportError:
    print("=" * 50)
    print("【错误】未安装依赖 Flask")
    print("请在本目录运行:")
    print(r'  .venv\Scripts\pip.exe install -r requirements.txt')
    print("或重新双击 start.bat")
    print("=" * 50)
    input("按回车键退出…")
    sys.exit(1)

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".ts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".rmvb", ".rm",
}

# 同目录下多段分片（如 HLS/切片）合并为一个播放入口
SEGMENT_EXTS = {".ts", ".m2ts"}
PLAYLIST_EXTS = {".m3u8"}
SEGMENT_FOLDER_GENERIC = {
    "ts", "m2ts", "video", "videos", "stream", "streams",
    "hls", "media", "data", "video_ts", "bdmv",
}
# 大于该体积的 .ts/.m2ts 视为整片，即使同目录有多个也不并入分片合集
# （典型 HLS 分片远小于此；整片录像常见数百 MB～数 GB）
STANDALONE_TS_MIN_BYTES = 50 * 1024 * 1024

# 浏览器较易播放的格式
BROWSER_FRIENDLY_EXTS = {".mp4", ".webm", ".m4v", ".mov"}
# 浏览器常失败，建议本地播放器
BROWSER_HARD_EXTS = {".mkv", ".avi", ".wmv", ".flv", ".rmvb", ".rm", ".ts", ".m2ts", ".mpg", ".mpeg"}

# 影片类型：规范名 + 匹配关键词（路径/文件名包含即命中；无片源的类型前端不显示）
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

THUMB_DIR_NAME = ".video_gallery_cache"  # 旧版扫盘目录名，仅用于跳过/清理
# 早期版本曾把缓存写到视频盘根目录，现已废弃
LEGACY_DISK_CACHE_NAMES = (
    ".video_gallery_cache",
    "video_gallery_cache",
    ".vgdata",
)
INDEX_NAME = "index.json"
APP_DIR = Path(__file__).resolve().parent
# 预览图/索引：固定在程序根目录 preview_cache\（不写视频盘）
VGDATA_DIR = APP_DIR / "preview_cache"
KEY_FILE = VGDATA_DIR / "vault.key"
PREFS_FILE = VGDATA_DIR / "prefs.json"
THUMB_EXT = ".vgt"  # 加密预览图，不能当普通图片打开
THUMB_WORKERS_MAX = 32
THUMB_JPEG_CACHE_MAX = 256  # 内存中缓存已解密 JPEG，加速反复打开

# 扫描整盘时跳过（大小写不敏感）
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

app = Flask(__name__)
STATE: dict = {
    "root": None,
    "cache_dir": None,
    "tree": {"name": "全部", "path": "", "children": [], "videos": []},
    "videos": [],  # flat list
    "by_category": {},  # cat -> list[video]
    "by_id": {},
    "facets": None,  # 预计算的 types/genres/categories
    "scanning": False,
    "scan_progress": "",
    "thumb_progress": "",
    "ffmpeg": None,
    "updating": False,  # 后台增量中
    "exporting": False,
    "export_ok": None,
    "export_msg": "",
    "export_path": "",
    "convert_jobs": {},  # job_id -> job dict
}
_scan_lock = threading.Lock()
_convert_lock = threading.Lock()
_thumb_jpeg_cache: OrderedDict[str, bytes] = OrderedDict()
_thumb_jpeg_lock = threading.Lock()


def log(msg: str) -> None:
    """CMD 窗口可见日志（立即刷新）。"""
    try:
        print(msg, flush=True)
    except Exception:
        pass


def thumb_worker_count(total: int = 0) -> int:
    cpus = os.cpu_count() or 4
    n = max(2, min(THUMB_WORKERS_MAX, cpus))
    if total > 0:
        n = max(1, min(n, total))
    return n


def _video_search_text(v: dict) -> str:
    cached = v.get("_q")
    if cached is not None:
        return cached
    text = f"{v.get('name') or ''} {v.get('rel') or ''}".lower()
    v["_q"] = text
    return text


def rebuild_indexes(videos: list[dict] | None = None) -> None:
    """扫描结束后预计算频道索引与侧面统计，加速 /api/tree、/api/videos。"""
    videos = videos if videos is not None else STATE.get("videos") or []
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
    STATE["facets"] = {
        "types": types,
        "genres": genres,
        "categories": categories,
        "count": len(videos),
    }


def thumb_cache_get(vid: str) -> bytes | None:
    with _thumb_jpeg_lock:
        raw = _thumb_jpeg_cache.get(vid)
        if raw is not None:
            _thumb_jpeg_cache.move_to_end(vid)
        return raw


def thumb_cache_put(vid: str, raw: bytes) -> None:
    with _thumb_jpeg_lock:
        _thumb_jpeg_cache[vid] = raw
        _thumb_jpeg_cache.move_to_end(vid)
        while len(_thumb_jpeg_cache) > THUMB_JPEG_CACHE_MAX:
            _thumb_jpeg_cache.popitem(last=False)


def thumb_cache_invalidate(vid: str | None = None) -> None:
    with _thumb_jpeg_lock:
        if vid:
            _thumb_jpeg_cache.pop(vid, None)
        else:
            _thumb_jpeg_cache.clear()


def find_ffmpeg() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    # 常见 Windows 安装位置
    candidates = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _fmt_bytes(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024:
            return f"{x:.1f} {unit}" if unit != "B" else f"{int(x)} B"
        x /= 1024
    return f"{x:.1f} PB"


def _volume_label(root: str) -> str:
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(261)
        # 不设置 argtypes，避免 None 指针类型不匹配
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), buf, 261, None, None, None, None, 0
        )
        return buf.value if ok else ""
    except Exception:
        return ""


def _drive_type_name(dtype: int) -> str:
    return {2: "可移动", 3: "本地磁盘", 4: "网络"}.get(dtype, "磁盘")


def _drive_entry(letter: str, dtype: int | None = None) -> dict | None:
    root = f"{letter}:\\"
    try:
        if not os.path.isdir(root):
            return None
    except OSError:
        return None
    free_h = total_h = ""
    try:
        usage = shutil.disk_usage(root)
        free_h = _fmt_bytes(usage.free)
        total_h = _fmt_bytes(usage.total)
    except OSError:
        pass
    label = ""
    try:
        label = _volume_label(root)
    except Exception:
        label = ""
    type_name = _drive_type_name(dtype) if dtype is not None else "磁盘"
    return {
        "letter": f"{letter}:",
        "path": root,
        "label": label or "",
        "type": type_name,
        "free_h": free_h,
        "total_h": total_h,
        "display": f"{letter}: {label}" if label else f"{letter}:",
    }


def list_drives_info() -> list[dict]:
    """供前端选择的盘符列表（尽量简单可靠）。"""
    drives: list[dict] = []
    seen: set[str] = set()

    def add(entry: dict | None) -> None:
        if not entry:
            return
        key = entry["letter"].upper()
        if key in seen:
            return
        seen.add(key)
        drives.append(entry)

    if sys.platform == "win32":
        # 方法1：GetLogicalDrives
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            bitmask = int(kernel32.GetLogicalDrives())
            for i, letter in enumerate(string.ascii_uppercase):
                if bitmask & (1 << i):
                    root = f"{letter}:\\"
                    try:
                        dtype = int(kernel32.GetDriveTypeW(root))
                    except Exception:
                        dtype = 3
                    # 1=无效 5=光驱 跳过；其余都列出来
                    if dtype in (1, 5):
                        continue
                    add(_drive_entry(letter, dtype))
        except Exception as e:
            print(f"提示: GetLogicalDrives 失败: {e}")

        # 方法2：兜底 A-Z 探测（U 盘、部分盘符）
        if not drives:
            for letter in string.ascii_uppercase:
                add(_drive_entry(letter, 3))
    else:
        for base in (Path("/media"), Path("/mnt"), Path("/Volumes"), Path("/")):
            if not base.is_dir():
                continue
            try:
                for p in sorted(base.iterdir()):
                    if p.is_dir():
                        add({
                            "letter": p.name,
                            "path": str(p),
                            "label": p.name,
                            "type": "挂载",
                            "free_h": "",
                            "total_h": "",
                            "display": p.name,
                        })
            except OSError:
                continue

    return drives


def list_ready_drives() -> list[Path]:
    """列出本机可用硬盘盘符。"""
    return [Path(d["path"]) for d in list_drives_info()]



def load_prefs() -> dict:
    try:
        if PREFS_FILE.exists():
            data = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_prefs(**kwargs) -> None:
    try:
        VGDATA_DIR.mkdir(parents=True, exist_ok=True)
        prefs = load_prefs()
        prefs.update({k: v for k, v in kwargs.items() if v is not None})
        PREFS_FILE.write_text(
            json.dumps(prefs, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        log(f"[偏好] 保存失败: {e}")


def default_scan_root() -> Path:
    """优先上次打开的盘；否则扫「最后一个盘」。"""
    prefs = load_prefs()
    last = (prefs.get("last_root") or "").strip()
    if last:
        try:
            p = Path(last)
            if p.is_dir():
                log(f"[偏好] 使用上次目录: {p}")
                return p
        except OSError:
            pass
    drives = list_ready_drives()
    if not drives:
        raise SystemExit("未检测到可用硬盘")
    last_drive = drives[-1]
    print(f"检测到盘符: {', '.join(str(d) for d in drives)}")
    print(f"默认扫描最后一个盘: {last_drive}")
    if len(drives) == 1 and str(last_drive).upper().startswith("C:"):
        print("提示: 当前只有 C 盘，整盘扫描可能较慢，且会跳过 Windows 系统目录。")
        print('      若视频在子文件夹，建议指定目录: python app.py "C:\\Videos"')
    return last_drive


def start_scan(root: Path, do_thumbs: bool = True, force: bool = False) -> tuple[bool, str]:
    """切换根目录。force=False 优先读缓存（秒开）再后台增量；force=True 增量全盘扫描。"""
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
            STATE["root"] = root
            STATE["cache_dir"] = ensure_cache_dir(root)
            save_prefs(last_root=str(root))
            if force:
                STATE["scan_progress"] = f"正在增量扫描 {root} …"
                STATE["thumb_progress"] = ""
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


def safe_rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def resolve_under_root(rel: str) -> Path | None:
    """解析 root 下任意文件（含 m3u8 分片），防止路径穿越。"""
    root = STATE["root"]
    if root is None:
        return None
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return None
    try:
        full = (root / rel).resolve()
        full.relative_to(root.resolve())
        return full if full.is_file() else None
    except (ValueError, OSError):
        return None


def resolve_video_path(rel: str) -> Path | None:
    """把相对路径解析到 root 下，防止路径穿越。"""
    return resolve_under_root(rel)


def video_id(rel: str) -> str:
    return hashlib.md5(rel.encode("utf-8")).hexdigest()[:16]


def natural_sort_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s or "")]


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


def should_skip_dir(name: str) -> bool:
    return name.startswith(".") or name.lower() in SKIP_DIR_NAMES


def _clear_path_attrs_windows(path: Path) -> None:
    """去掉 Hidden/System，否则 Windows 上覆盖写入常报 PermissionError (errno 13)。"""
    if sys.platform != "win32":
        return
    try:
        if not path.exists():
            return
        import ctypes
        # FILE_ATTRIBUTE_NORMAL
        ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x80)
    except Exception:
        pass


def _hide_path_windows(path: Path) -> None:
    """仅标记隐藏，不再加 System（System 会导致无法覆盖写入）。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x2)  # FILE_ATTRIBUTE_HIDDEN
    except Exception:
        pass


def _ensure_vault_key() -> bytes:
    VGDATA_DIR.mkdir(parents=True, exist_ok=True)
    if KEY_FILE.exists():
        _clear_path_attrs_windows(KEY_FILE)
        try:
            key = KEY_FILE.read_bytes()
            if len(key) >= 32:
                return key[:32]
        except OSError as e:
            log(f"[预览图] 读取密钥失败: {e}，将重新生成（旧预览图会失效）")
    key = os.urandom(32)
    try:
        _clear_path_attrs_windows(KEY_FILE)
        KEY_FILE.write_bytes(key)
    except OSError as e:
        log(f"[预览图] 写入密钥失败: {e}")
        raise
    return key


def _xor_stream(key: bytes, nonce: bytes, length: int) -> bytes:
    seed = hashlib.sha256(key + nonce).digest()
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hashlib.sha256(seed + counter.to_bytes(8, "big")).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encrypt_blob_with_key(data: bytes, key: bytes) -> bytes:
    """用指定密钥加密（VG1 + nonce + ciphertext）。"""
    nonce = os.urandom(16)
    stream = _xor_stream(key[:32], nonce, len(data))
    cipher = bytes(a ^ b for a, b in zip(data, stream))
    return b"VG1\0" + nonce + cipher


def encrypt_blob(data: bytes) -> bytes:
    """本地预览图加密：VG1 + nonce + ciphertext（无密钥无法当图片打开）。"""
    return encrypt_blob_with_key(data, _ensure_vault_key())


def decrypt_blob_with_key(blob: bytes, key: bytes) -> bytes | None:
    if not blob.startswith(b"VG1\0") or len(blob) < 20:
        return None
    nonce = blob[4:20]
    cipher = blob[20:]
    stream = _xor_stream(key[:32], nonce, len(cipher))
    return bytes(a ^ b for a, b in zip(cipher, stream))


def decrypt_blob(blob: bytes) -> bytes | None:
    if not blob.startswith(b"VG1\0") or len(blob) < 20:
        return None
    return decrypt_blob_with_key(blob, _ensure_vault_key())


def thumb_path(cache: Path, vid: str) -> Path:
    return cache / f"{vid}{THUMB_EXT}"


def thumb_version(cache: Path | None, vid: str) -> int:
    """用于前端缓存破坏；有有效文件则返回 mtime。"""
    if not cache:
        return 0
    p = thumb_path(cache, vid)
    try:
        if p.exists() and p.stat().st_size > 24:
            return int(p.stat().st_mtime)
    except OSError:
        pass
    return 0


def thumb_file_ready(cache: Path | None, vid: str) -> bool:
    """只检查文件是否存在且非空，不解密（扫描/列表用，更快）。"""
    if not cache or not vid:
        return False
    p = thumb_path(cache, vid)
    try:
        return p.exists() and p.stat().st_size > 24
    except OSError:
        return False


def read_thumb_jpeg(cache: Path, vid: str) -> bytes | None:
    """读取并解密预览图；带内存 LRU。"""
    cached = thumb_cache_get(vid)
    if cached is not None:
        return cached
    p = thumb_path(cache, vid)
    try:
        if not p.exists() or p.stat().st_size <= 24:
            return None
        _clear_path_attrs_windows(p)
        raw = decrypt_blob(p.read_bytes())
        if raw and len(raw) > 100 and raw[:2] == b"\xff\xd8":
            thumb_cache_put(vid, raw)
            return raw
    except OSError as e:
        log(f"[预览图] 读取失败 {vid}: {e}")
    return None


def has_encrypted_thumb(cache: Path, vid: str) -> bool:
    """服务端校验：优先内存缓存，否则快速文件探测，必要时再解密。"""
    if thumb_cache_get(vid) is not None:
        return True
    if not thumb_file_ready(cache, vid):
        return False
    return read_thumb_jpeg(cache, vid) is not None


def ensure_cache_dir(root: Path) -> Path:
    """缓存固定：程序根目录/preview_cache/<盘符标识>/（绝不写到视频盘根目录）。"""
    VGDATA_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_vault_key()
    cleanup_legacy_disk_cache(root)
    # 用盘符字母作子目录名，方便辨认（如 E、D）；整路径再哈希兜底
    try:
        drive = root.resolve().drive.rstrip(":\\/") or "disk"
    except OSError:
        drive = "disk"
    safe = re.sub(r"[^\w\-]+", "_", drive)[:16] or "disk"
    digest = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:8]
    cache = VGDATA_DIR / f"{safe}_{digest}"
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def cleanup_legacy_disk_cache(root: Path) -> None:
    """删除早期版本误写在视频盘根目录的缓存文件夹（可安全删）。"""
    if not root:
        return
    for name in LEGACY_DISK_CACHE_NAMES:
        p = root / name
        try:
            if not p.is_dir():
                continue
        except OSError:
            continue
        try:
            _clear_path_attrs_windows(p)
            shutil.rmtree(p)
            log(f"[清理] 已删除旧版盘根缓存（现已改到程序目录 preview_cache）: {p}")
        except OSError as e:
            log(f"[清理] 删不掉旧缓存 {p}: {e}（可在资源管理器里手动删除）")


def save_index(cache: Path, root: Path, videos: list[dict]) -> None:
    path = cache / INDEX_NAME
    tmp = cache / (INDEX_NAME + ".tmp")
    try:
        cache.mkdir(parents=True, exist_ok=True)
        _clear_path_attrs_windows(path)
        _clear_path_attrs_windows(tmp)
        # 去掉运行期字段，减小索引体积
        clean = []
        for v in videos:
            if "_q" in v:
                clean.append({k: val for k, val in v.items() if k != "_q"})
            else:
                clean.append(v)
        payload = json.dumps(
            {"root": str(root), "videos": clean, "updated": datetime.now().isoformat()},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        print(f"提示: 保存索引失败: {e}")
        print(f"       路径: {path}")
        try:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        except OSError:
            pass


def format_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


def format_duration(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return ""
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def probe_duration(ffmpeg: str, path: Path) -> float | None:
    ffprobe = ffmpeg.replace("ffmpeg", "ffprobe").replace("ffmpeg.exe", "ffprobe.exe")
    if not os.path.isfile(ffprobe):
        ffprobe = shutil.which("ffprobe") or ""
    if not ffprobe:
        return None
    try:
        r = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
        )
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except Exception:
        pass
    return None


def make_thumbnail(ffmpeg: str, video: Path, out: Path, seek: float = 3.0) -> bool:
    """截帧后写入加密预览图 out（.vgt）。有效缓存则跳过；损坏则重建。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    _clear_path_attrs_windows(out)
    if out.exists():
        try:
            raw = decrypt_blob(out.read_bytes())
            if raw and raw[:2] == b"\xff\xd8" and len(raw) > 100:
                return True
            out.unlink(missing_ok=True)
        except OSError:
            pass

    if not video.is_file():
        return False

    tmp = out.with_suffix(".tmp.jpg")
    try:
        for ss in (seek, 1.0, 0.0, 10.0):
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            try:
                cmd = [
                    ffmpeg, "-y", "-ss", str(ss), "-i", str(video),
                    "-frames:v", "1", "-vf", "scale=480:-2",
                    "-q:v", "4", str(tmp),
                ]
                r = subprocess.run(
                    cmd, capture_output=True, timeout=60,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
                )
                if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
                    raw = tmp.read_bytes()
                    if not (raw[:2] == b"\xff\xd8"):
                        continue
                    _clear_path_attrs_windows(out)
                    out.write_bytes(encrypt_blob(raw))
                    return True
            except Exception:
                continue
        return False
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _first_media_from_m3u8(playlist: Path) -> Path | None:
    try:
        text = playlist.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if re.match(r"https?://", s, re.I):
            continue
        cand = (playlist.parent / s.split("?")[0]).resolve()
        try:
            if cand.is_file():
                if cand.suffix.lower() == ".m3u8":
                    nested = _first_media_from_m3u8(cand)
                    if nested:
                        return nested
                    continue
                return cand
        except OSError:
            continue
    return None


def _video_file_for_thumb(item: dict) -> Path | None:
    """取可用于截帧的实体文件（TS 合集用第一段；m3u8 解析首个媒体）。"""
    if not STATE.get("root"):
        return None
    if item.get("kind") == "ts_set" and item.get("segments"):
        return resolve_under_root(item["segments"][0])
    if item.get("kind") == "m3u8" or (item.get("ext") or "").lower() == ".m3u8":
        pl = resolve_under_root(item.get("rel") or "")
        if pl:
            hit = _first_media_from_m3u8(pl)
            if hit:
                return hit
        return None
    return resolve_under_root(item.get("rel") or "")


def attach_thumb_meta(v: dict) -> dict:
    """给列表项补 has_thumb / thumb_v（只看文件是否存在，避免列表接口解密过慢）。"""
    cache = STATE.get("cache_dir")
    vid = v.get("id") or ""
    if cache and vid and (thumb_cache_get(vid) is not None or thumb_file_ready(cache, vid)):
        v["has_thumb"] = True
        v["thumb_v"] = thumb_version(cache, vid) or 1
        return v
    v["has_thumb"] = False
    v["thumb_v"] = 0
    return v


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
    STATE["tree"] = build_tree(root, found)
    rebuild_indexes(found)
    extra = f"（{len(errors)} 个目录跳过）" if errors else ""
    tip = f"，复用 {reused}，新建/变更 {added}" if incremental else ""
    STATE["scan_progress"] = f"扫描完成，共 {len(found)} 个视频{tip}{extra}"
    log(f"[扫描] 完成，共 {len(found)} 个{tip}{extra}")
    save_index(cache, root, found)
    save_prefs(last_root=str(root))

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
                    vid = v.get("id") or video_id(rel)
                    v["id"] = vid
                    v["thumb"] = f"{vid}{THUMB_EXT}"
                    v["has_thumb"] = thumb_file_ready(cache, vid)
                    ensure_video_genres(v)
                    videos.append(v)
                videos = collapse_segment_sets(videos)
                STATE["tree"] = build_tree(root, videos)
                rebuild_indexes(videos)
                dropped = len(data["videos"]) - len(videos)
                tip = f"，已忽略失效 {dropped} 个" if dropped else ""
                STATE["scan_progress"] = f"已加载缓存，共 {len(videos)} 个视频{tip}（后台将增量更新）"
                log(f"[缓存] 已加载 {len(videos)} 个视频{tip} ← {index_path}")
                save_index(cache, root, videos)
                save_prefs(last_root=str(root))
                # 缺图交给随后的后台增量扫描统一并行补全，避免与增量抢状态
                missing = sum(1 for v in videos if not v.get("has_thumb"))
                if missing:
                    log(f"[预览图] 缓存缺图约 {missing} 个，将在后台增量时补全")
                else:
                    log("[预览图] 缓存齐全")
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


# ---------- routes ----------

STATIC_EXPORT_DIRNAME = "_video_gallery_static"


def _rel_between(from_dir: Path, to_path: Path) -> str:
    return Path(os.path.relpath(str(to_path), str(from_dir))).as_posix()


def _ensure_hls_js(dest: Path) -> bool:
    """把 hls.min.js 放到静态站 _cache 目录（优先本地缓存，其次下载）。"""
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / "hls.min.js"
    if target.exists() and target.stat().st_size > 1000:
        return True
    local_candidates = [
        APP_DIR / "static" / "hls.min.js",
        APP_DIR / "preview_cache" / "hls.min.js",
    ]
    for c in local_candidates:
        try:
            if c.exists() and c.stat().st_size > 1000:
                target.write_bytes(c.read_bytes())
                return True
        except OSError:
            pass
    urls = [
        "https://cdn.jsdelivr.net/npm/hls.js@1.5.18/dist/hls.min.js",
        "https://unpkg.com/hls.js@1.5.18/dist/hls.min.js",
    ]
    try:
        import urllib.request
        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=20) as resp:
                    data = resp.read()
                if data and len(data) > 1000:
                    target.write_bytes(data)
                    (APP_DIR / "preview_cache").mkdir(parents=True, exist_ok=True)
                    (APP_DIR / "preview_cache" / "hls.min.js").write_bytes(data)
                    return True
            except Exception:
                continue
    except Exception:
        pass
    # 没有 hls 也能打开页面，只是 TS/m3u8 浏览器播可能受限
    target.write_text("/* hls.js missing */\n", encoding="utf-8")
    return False


def _export_playlist_files(export_dir: Path, root: Path, item: dict) -> str | None:
    """为 ts_set / m3u8 生成相对路径播放列表，返回相对 index.html 的路径。"""
    play_dir = export_dir / "_cache" / "playlists"
    play_dir.mkdir(parents=True, exist_ok=True)
    vid = item.get("id") or "x"
    out = play_dir / f"{vid}.m3u8"
    kind = item.get("kind") or ""

    if kind == "ts_set" and item.get("segments"):
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-TARGETDURATION:30",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
        ]
        for seg in item["segments"]:
            seg_path = root / seg
            if not seg_path.is_file():
                continue
            rel = _rel_between(play_dir, seg_path)
            lines.append("#EXTINF:10.0,")
            lines.append(rel)
        lines.append("#EXT-X-ENDLIST")
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return f"_cache/playlists/{vid}.m3u8"

    if kind == "m3u8" or (item.get("ext") or "").lower() == ".m3u8":
        src = root / (item.get("rel") or "")
        if not src.is_file():
            return None
        try:
            text = src.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        base_dir = str(Path(item["rel"]).parent).replace("\\", "/")
        if base_dir == ".":
            base_dir = ""
        lines = []
        for line in text.splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or re.match(r"https?://", raw, re.I):
                lines.append(line)
                continue
            seg_rel = _normalize_rel_join(base_dir, raw)
            seg_path = root / seg_rel
            if seg_path.is_file():
                lines.append(_rel_between(play_dir, seg_path))
            else:
                lines.append(line)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return f"_cache/playlists/{vid}.m3u8"
    return None


def export_static_site(root: Path | None = None, videos: list[dict] | None = None) -> tuple[bool, str, str]:
    """
    导出纯静态站到「视频盘根目录/_video_gallery_static/」。
    预览图等资源全部在站内 _cache/，不写盘根其它缓存目录。
    返回 (ok, msg, export_path)。
    """
    root = Path(root or STATE.get("root") or "")
    if not root.is_dir():
        return False, "请先打开/扫描一个盘", ""
    videos = list(videos if videos is not None else (STATE.get("videos") or []))
    if not videos:
        return False, "当前没有可导出的视频，请先扫描", ""

    cleanup_legacy_disk_cache(root)

    cache = STATE.get("cache_dir") or ensure_cache_dir(root)
    export_dir = root / STATIC_EXPORT_DIRNAME
    cache_dir = export_dir / "_cache"
    thumbs_dir = cache_dir / "thumbs"
    try:
        export_dir.mkdir(parents=True, exist_ok=True)
        thumbs_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"无法创建导出目录: {e}", ""

    # 清理旧版导出在站根的 thumbs/（已迁入 _cache）
    for old in (export_dir / "thumbs", export_dir / "playlists", export_dir / "hls.min.js"):
        try:
            if old.is_dir():
                shutil.rmtree(old)
            elif old.is_file():
                old.unlink()
        except OSError:
            pass

    log(f"[静态导出] 开始 → {export_dir}（共 {len(videos)} 个）")
    STATE["export_msg"] = f"正在导出静态站 0/{len(videos)}…"
    _ensure_hls_js(cache_dir)

    # 预览图目录整夹重建（比逐个删旧 .js/.vgt 快很多）
    try:
        if thumbs_dir.exists():
            shutil.rmtree(thumbs_dir)
        thumbs_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"无法重建预览图目录: {e}", str(export_dir)

    # 文件名混淆盐：预览图仍是 JPEG，只是文件名看不出，扩展名也不是 .jpg
    name_salt = os.urandom(8).hex()

    def obscure_thumb_name(vid: str) -> str:
        digest = hashlib.sha256(f"{name_salt}:{vid}".encode("utf-8")).hexdigest()[:20]
        return f"{digest}.vgj"

    exported = []
    thumb_ok = thumb_fail = 0
    for i, v in enumerate(videos, 1):
        vid = v.get("id") or video_id(v.get("rel") or str(i))
        rel = (v.get("rel") or "").replace("\\", "/")
        folder = (v.get("folder") or "").strip("/")
        category = folder.split("/")[0] if folder else ""
        kind = v.get("kind") or ""
        abs_path = None
        if kind == "ts_set" and v.get("segments"):
            abs_path = root / v["segments"][0]
        else:
            abs_path = root / rel if rel else None

        src_rel = ""
        file_url = ""
        path_str = ""
        if abs_path and abs_path.is_file():
            src_rel = _rel_between(export_dir, abs_path)
            try:
                file_url = abs_path.resolve().as_uri()
            except OSError:
                file_url = ""
            path_str = str(abs_path)

        # 预览图：只拷贝已有缓存图，改个看不出的文件名（不做重加密，避免导出卡死）
        thumb_rel = ""
        raw = None
        if cache and thumb_file_ready(cache, vid):
            raw = read_thumb_jpeg(cache, vid)
        if raw:
            try:
                fname = obscure_thumb_name(vid)
                (thumbs_dir / fname).write_bytes(raw)
                thumb_rel = f"_cache/thumbs/{fname}"
                thumb_ok += 1
            except OSError:
                thumb_fail += 1
        else:
            thumb_fail += 1

        playlist = None
        note = ""
        if kind in ("ts_set", "m3u8") or (v.get("ext") or "").lower() == ".m3u8":
            playlist = _export_playlist_files(export_dir, root, v)
            note = "HLS/分片，建议 Firefox 或系统播放器"
        elif (v.get("ext") or "").lower() in BROWSER_HARD_EXTS:
            note = "浏览器可能无法内嵌播放，请用「打开文件」"

        exported.append({
            "id": vid,
            "name": v.get("name") or Path(rel).stem,
            "folder": folder,
            "category": category,
            "ext": v.get("ext") or "",
            "kind": kind,
            "size": int(v.get("size") or 0),
            "size_h": v.get("size_h") or "",
            "mtime": float(v.get("mtime") or 0),
            "mtime_h": v.get("mtime_h") or "",
            "genres": ensure_video_genres(v),
            "seg_count": int(v.get("seg_count") or 0),
            "src": src_rel,
            "path": path_str,
            "file_url": file_url,
            "thumb": thumb_rel,
            "playlist": playlist,
            "note": note,
        })
        if i % 50 == 0 or i == len(videos):
            STATE["export_msg"] = f"正在导出静态站 {i}/{len(videos)}…"
            log(f"[静态导出] 进度 {i}/{len(videos)}…")

    data_js = (
        "window.__VG_DATA__ = "
        + json.dumps(
            {
                "root": str(root),
                "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "count": len(exported),
                "bridge_port": 8767,
                "videos": exported,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + ";\n"
    )
    (export_dir / "data.js").write_text(data_js, encoding="utf-8")

    html_src = APP_DIR / "templates" / "static_site.html"
    if not html_src.exists():
        return False, "缺少 templates/static_site.html", str(export_dir)
    (export_dir / "index.html").write_text(html_src.read_text(encoding="utf-8"), encoding="utf-8")

    bridge_src = APP_DIR / "templates" / "static_bridge.py"
    if bridge_src.exists():
        (cache_dir / "static_bridge.py").write_text(bridge_src.read_text(encoding="utf-8"), encoding="utf-8")
    (cache_dir / "bridge.json").write_text(
        json.dumps({"root": str(root), "port": 8767}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    paths_map = {row["id"]: row["path"] for row in exported if row.get("id") and row.get("path")}
    (cache_dir / "paths.json").write_text(
        json.dumps(paths_map, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    hta_src = APP_DIR / "templates" / "static_launcher.hta"
    if hta_src.exists():
        (export_dir / "打开图库.hta").write_text(hta_src.read_text(encoding="utf-8"), encoding="utf-8")

    try:
        _hide_path_windows(cache_dir)
    except Exception:
        pass

    bat = export_dir / "打开图库.bat"
    bat.write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "cd /d \"%~dp0\"\r\n"
        "set VG_STATIC_PORT=8767\r\n"
        "where python >nul 2>&1 && set \"PY=python\"\r\n"
        "if not defined PY where py >nul 2>&1 && set \"PY=py\"\r\n"
        "if not defined PY (\r\n"
        "  echo 未找到 Python，改用 打开图库.hta\r\n"
        "  if exist \"打开图库.hta\" (start \"\" \"打开图库.hta\") else (start \"\" \"index.html\")\r\n"
        "  pause\r\n"
        "  exit /b 1\r\n"
        ")\r\n"
        "echo ========================================\r\n"
        "echo  静态视频库助手\r\n"
        "echo  浏览器将自动打开 http://127.0.0.1:%VG_STATIC_PORT%/\r\n"
        "echo  关闭本窗口 = 停止（系统播放器依赖此窗口）\r\n"
        "echo ========================================\r\n"
        "%PY% \"_cache\\static_bridge.py\"\r\n"
        "pause\r\n",
        encoding="utf-8",
    )
    readme = export_dir / "说明.txt"
    readme.write_text(
        "本地视频库 · 静态离线版\n"
        "====================\n"
        "【重要】请双击「打开图库.bat」，不要双击 index.html。\n"
        "\n"
        "1. 打开图库.bat：启动本地助手 + 浏览器（系统播放器可用）\n"
        "2. 打开图库.hta：无 Python 时的备用（系统播放器也可用）\n"
        "3. 地址栏应是 http://127.0.0.1:8767/ 才正常\n"
        "4. 预览图在 _cache\\thumbs\\，勿删 _cache\n"
        "5. 影片仍在原位置，未复制\n"
        f"6. 导出时间：{datetime.now().isoformat(timespec='seconds')}\n"
        f"7. 视频根目录：{root}\n",
        encoding="utf-8",
    )

    save_prefs(last_static_export=str(export_dir))
    msg = (
        f"已导出 {len(exported)} 个到：\n{export_dir}\n"
        f"预览图成功 {thumb_ok}，缺图 {thumb_fail}。\n"
        "双击该目录下「打开图库.bat」即可离线浏览。"
    )
    log(f"[静态导出] 完成：{export_dir}")
    return True, msg, str(export_dir)

@app.route("/")
def index():
    """返回页面；用字符串注入盘符 JSON，不依赖 Jinja，避免 {{ }} 原样显示。"""
    html_path = APP_DIR / "templates" / "index.html"
    html = html_path.read_text(encoding="utf-8")
    try:
        drives = list_drives_info()
        root = STATE["root"]
        current = str(root) if root else ""
        for d in drives:
            try:
                d["active"] = bool(current) and os.path.normcase(
                    os.path.abspath(d["path"])
                ) == os.path.normcase(os.path.abspath(current))
            except OSError:
                d["active"] = False
        payload = json.dumps(
            {"drives": drives, "current": current, "scanning": STATE["scanning"]},
            ensure_ascii=False,
        )
    except Exception as e:
        print(f"【错误】页面注入盘符失败: {e}")
        payload = '{"drives":[],"current":"","scanning":false}'
    boot = f"<script>window.__BOOT_DRIVES__ = {payload};</script>"
    if "</head>" in html:
        html = html.replace("</head>", boot + "\n</head>", 1)
    else:
        html = boot + html
    return Response(html, mimetype="text/html; charset=utf-8")


@app.route("/api/tree")
def api_tree():
    root = STATE["root"]
    videos = STATE["videos"]
    facets = STATE.get("facets")
    if not facets or STATE.get("scanning"):
        # 扫描中或尚未建索引：即时统计（或触发一次重建）
        if videos and not facets:
            rebuild_indexes(videos)
            facets = STATE.get("facets")
    if facets and not STATE.get("scanning"):
        types = facets.get("types") or []
        genres = facets.get("genres") or []
        categories = facets.get("categories") or []
        count = facets.get("count", len(videos))
    else:
        type_counts: dict[str, int] = {}
        cat_counts: dict[str, int] = {}
        genre_counts: dict[str, int] = {}
        for v in videos:
            ext = (v.get("ext") or "").lower() or "unknown"
            type_counts[ext] = type_counts.get(ext, 0) + 1
            cat = _video_category(v)
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
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
        count = len(videos)

    return jsonify({
        "tree": STATE["tree"],
        "types": types,
        "genres": genres,
        "categories": categories,
        "scanning": STATE["scanning"],
        "updating": bool(STATE.get("updating")),
        "exporting": bool(STATE.get("exporting")),
        "export_msg": STATE.get("export_msg") or "",
        "export_path": STATE.get("export_path") or "",
        "export_ok": STATE.get("export_ok"),
        "scan_progress": STATE["scan_progress"],
        "thumb_progress": STATE["thumb_progress"],
        "count": count,
        "has_ffmpeg": bool(STATE["ffmpeg"]),
        "root": str(root) if root else "",
    })


def find_video_by_id(vid: str) -> dict | None:
    by_id = STATE.get("by_id") or {}
    hit = by_id.get(vid)
    if hit is not None:
        return hit
    return next((v for v in STATE.get("videos") or [] if v.get("id") == vid), None)


def _video_category(v: dict) -> str:
    """一级目录作为频道。"""
    folder = (v.get("folder") or "").strip("/")
    if not folder:
        return ""
    return folder.split("/")[0]


def _genre_facets(videos: list[dict]) -> list[dict]:
    """统计类型；只返回有片的。含子目录路径里识别到的类型。"""
    genre_counts: dict[str, int] = {}
    for v in videos:
        for g in ensure_video_genres(v):
            genre_counts[g] = genre_counts.get(g, 0) + 1
    genre_order = {name: i for i, (name, _) in enumerate(GENRE_DEFS)}
    return [
        {"id": name, "name": name, "count": cnt}
        for name, cnt in sorted(
            genre_counts.items(),
            key=lambda x: (genre_order.get(x[0], 999), -x[1], x[0]),
        )
        if cnt > 0
    ]


def _subfolder_facets(videos: list[dict], category: str, folder: str) -> list[dict]:
    """
    当前频道/子目录下的「下一级」文件夹列表。
    例如选了 电影 → 列出 华语/欧美；再选 电影/华语 → 列出 动作/爱情。
    仅统计真正的子目录（路径更深），本层直接放文件的不算子类。
    """
    prefix = (folder or category or "").strip("/")
    counts: dict[str, int] = {}
    for v in videos:
        f = (v.get("folder") or "").strip("/")
        if not f:
            continue
        if prefix:
            if f == prefix:
                continue  # 就在本层文件，没有更深子目录
            if not f.startswith(prefix + "/"):
                continue
            rest = f[len(prefix) + 1 :]
        else:
            rest = f
        nxt = rest.split("/")[0]
        if not nxt:
            continue
        full = f"{prefix}/{nxt}" if prefix else nxt
        if f == full or f.startswith(full + "/"):
            counts[nxt] = counts.get(nxt, 0) + 1
    return [
        {
            "id": (f"{prefix}/{name}" if prefix else name),
            "name": name,
            "count": cnt,
        }
        for name, cnt in sorted(counts.items(), key=lambda x: (-x[1], x[0].lower()))
        if cnt > 0
    ]


def _subfolder_levels(cat_videos: list[dict], category: str, folder: str) -> list[dict]:
    """
    多层子类行：
    - 第 1 行：频道下的直接子目录
    - 选中某一项且其下还有子目录时，再追加一行
    - folder 始终为完整相对路径（含频道名）
    """
    cat = (category or "").strip("/")
    if not cat or cat == "__root__":
        return []

    folder_norm = (folder or "").strip("/").replace("\\", "/")
    if folder_norm == cat:
        folder_norm = ""
    # 纠正：子路径必须挂在当前频道下
    if folder_norm and folder_norm != cat and not folder_norm.startswith(cat + "/"):
        folder_norm = f"{cat}/{folder_norm}"

    # 从频道到当前选中路径的前缀链
    prefixes: list[str] = [cat]
    if folder_norm.startswith(cat + "/"):
        acc = cat
        for part in folder_norm[len(cat) + 1 :].split("/"):
            if not part:
                continue
            acc = f"{acc}/{part}"
            prefixes.append(acc)

    levels: list[dict] = []
    for i, prefix in enumerate(prefixes):
        items = _subfolder_facets(cat_videos, "", prefix)
        if not items:
            break  # 该层没有子目录（只有文件）→ 不再追加行
        selected = ""
        if folder_norm.startswith(prefix + "/"):
            selected = prefix + "/" + folder_norm[len(prefix) + 1 :].split("/")[0]
        label = "子类" if i == 0 else prefix.split("/")[-1]
        # 第 1 行「全部」清空；更深行「全部」= 停在本层（勾选=含子目录全部，取消=只看根目录）
        all_id = "" if prefix == cat else prefix
        levels.append({
            "label": label,
            "prefix": prefix,
            "all_id": all_id,
            "selected": selected,
            "items": items,
        })
    return levels


@app.route("/api/videos")
def api_videos():
    folder = request.args.get("folder", "").strip().strip("/")
    category = request.args.get("category", "").strip().strip("/")
    genre = request.args.get("genre", "").strip()
    q = request.args.get("q", "").strip().lower()
    ext = request.args.get("ext", "").strip().lower()
    sort = request.args.get("sort", "mtime_desc").strip().lower()

    # 优先用预建频道索引，避免每次全表拷贝+过滤
    by_cat = STATE.get("by_category") or {}
    if category == "__root__":
        videos = list(by_cat.get("", []) or [
            v for v in STATE["videos"] if not (v.get("folder") or "").strip("/")
        ])
    elif category and category in by_cat:
        videos = list(by_cat[category])
    elif category:
        videos = [v for v in STATE["videos"] if _video_category(v) == category]
    else:
        videos = list(STATE["videos"])

    # 多层子类用「频道内全部片」统计各层兄弟项
    cat_videos = videos
    if ext or q:
        cat_videos = list(videos)
        if ext:
            ext_n = ext if ext.startswith(".") else "." + ext
            cat_videos = [v for v in cat_videos if (v.get("ext") or "").lower() == ext_n]
        if q:
            cat_videos = [v for v in cat_videos if q in _video_search_text(v)]

    subfolder_levels = _subfolder_levels(cat_videos, category, folder)

    if folder:
        folder = folder.replace("\\", "/")
        # 有子分类时：folder_all=1（默认）含子目录全部；取消全部则只看本层根目录
        # has_children 用未按搜索/格式收窄的频道列表，避免搜索时误判无子类
        folder_all = request.args.get("folder_all", "").strip() in ("1", "true", "yes")
        has_children = bool(_subfolder_facets(videos, "", folder))
        if has_children and not folder_all:
            videos = [
                v for v in videos
                if (v.get("folder") or "").replace("\\", "/") == folder
            ]
        else:
            videos = [
                v for v in videos
                if (v.get("folder") or "").replace("\\", "/") == folder
                or (v.get("folder") or "").replace("\\", "/").startswith(folder + "/")
            ]
    if ext:
        if not ext.startswith("."):
            ext = "." + ext
        videos = [v for v in videos if (v.get("ext") or "").lower() == ext]
    if q:
        videos = [v for v in videos if q in _video_search_text(v)]

    scoped_genres = _genre_facets(videos)
    scoped_subs = _subfolder_facets(videos, category, folder)

    if genre:
        videos = [v for v in videos if genre in ensure_video_genres(v)]

    reverse = True
    key_fn = lambda v: v.get("mtime") or 0
    if sort == "mtime_asc":
        reverse = False
    elif sort == "name":
        key_fn = lambda v: (v.get("name") or "").lower()
        reverse = False
    elif sort == "size_desc":
        key_fn = lambda v: v.get("size") or 0
        reverse = True
    elif sort == "size_asc":
        key_fn = lambda v: v.get("size") or 0
        reverse = False
    else:
        reverse = True
        key_fn = lambda v: v.get("mtime") or 0

    videos.sort(key=key_fn, reverse=reverse)
    total = len(videos)
    try:
        offset = max(0, int(request.args.get("offset", 0) or 0))
    except ValueError:
        offset = 0
    try:
        limit = int(request.args.get("limit", 60) or 60)
    except ValueError:
        limit = 60
    limit = max(1, min(limit, 200))

    page = videos[offset: offset + limit]
    slim = []
    for v in page:
        row = {k: v[k] for k in v if k not in ("segments", "_q")} if (
            v.get("kind") == "ts_set" and v.get("segments")
        ) else {k: v[k] for k in v if k != "_q"}
        attach_thumb_meta(row)
        slim.append(row)

    return jsonify({
        "videos": slim,
        "count": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(slim) < total,
        "genres": scoped_genres,
        "subfolders": scoped_subs,
        "subfolder_levels": subfolder_levels,
    })


@app.route("/api/drives")
def api_drives():
    root = STATE["root"]
    current = str(root) if root else ""
    try:
        drives = list_drives_info()
    except Exception as e:
        print(f"【错误】列出盘符失败: {e}")
        drives = []
    for d in drives:
        try:
            d["active"] = bool(current) and os.path.normcase(
                os.path.abspath(d["path"])
            ) == os.path.normcase(os.path.abspath(current))
        except OSError:
            d["active"] = False
    return jsonify({
        "drives": drives,
        "current": current,
        "scanning": STATE["scanning"],
        "count": len(drives),
    })


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """选择盘符或目录并扫描。body: { path?: "E:\\", drive?: "E:", thumbs?: true, force?: true }"""
    data = request.get_json(silent=True) or {}
    path = (data.get("path") or data.get("drive") or "").strip().strip('"')
    if not path:
        return jsonify({"ok": False, "msg": "请选择盘符"}), 400
    # 允许 E: / E:\ / E:/
    if len(path) == 2 and path[1] == ":":
        path = path + "\\"
    do_thumbs = data.get("thumbs", True)
    force = data.get("force", False)
    ok, msg = start_scan(Path(path), do_thumbs=bool(do_thumbs), force=bool(force))
    if not ok:
        return jsonify({"ok": False, "msg": msg}), 409
    return jsonify({"ok": True, "msg": msg, "root": str(Path(path).resolve())})


@app.route("/api/rescan", methods=["POST"])
def api_rescan():
    if not STATE["root"]:
        return jsonify({"ok": False, "msg": "尚未选择盘符"}), 400
    data = request.get_json(silent=True) or {}
    do_thumbs = data.get("thumbs", True)
    ok, msg = start_scan(STATE["root"], do_thumbs=bool(do_thumbs), force=True)
    if not ok:
        return jsonify({"ok": False, "msg": msg}), 409
    return jsonify({"ok": True, "msg": msg})


@app.route("/thumb/<vid>")
def thumb(vid: str):
    if not re.fullmatch(r"[a-f0-9]{16}", vid):
        abort(404)
    cache = STATE["cache_dir"]
    placeholder = '''<svg xmlns="http://www.w3.org/2000/svg" width="480" height="270" viewBox="0 0 480 270">
      <rect fill="#1a1d24" width="480" height="270"/>
      <polygon points="210,100 210,170 280,135" fill="#4a5568"/>
    </svg>'''
    placeholder_headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
    }

    if cache:
        raw = read_thumb_jpeg(cache, vid)
        if raw:
            return Response(
                raw,
                mimetype="image/jpeg",
                headers={"Cache-Control": "public, max-age=86400"},
            )
        # 损坏或不存在：尝试现场重建一次
        item = find_video_by_id(vid)
        ffmpeg = STATE.get("ffmpeg")
        if item and ffmpeg:
            src = _video_file_for_thumb(item)
            out = thumb_path(cache, vid)
            if src and make_thumbnail(ffmpeg, src, out):
                thumb_cache_invalidate(vid)
                raw = read_thumb_jpeg(cache, vid)
                if raw:
                    item["has_thumb"] = True
                    item["thumb_v"] = thumb_version(cache, vid)
                    return Response(
                        raw,
                        mimetype="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"},
                    )
            # 解密失败的坏文件删掉，避免反复失败
            try:
                if out.exists() and not read_thumb_jpeg(cache, vid):
                    _clear_path_attrs_windows(out)
                    out.unlink(missing_ok=True)
                    thumb_cache_invalidate(vid)
                    log(f"[预览图] 已删除损坏缓存: {vid}")
            except OSError:
                pass

    return Response(placeholder, mimetype="image/svg+xml", headers=placeholder_headers)


def rewrite_m3u8_for_proxy(text: str, playlist_rel: str, vid: str) -> str:
    """把 m3u8 里的相对分片改写到本服务 /hls/<vid>/file?rel=..."""
    from urllib.parse import quote

    base_dir = str(Path(playlist_rel.replace("\\", "/")).parent).replace("\\", "/")
    if base_dir == ".":
        base_dir = ""
    lines: list[str] = []
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            lines.append(line)
            continue
        if re.match(r"https?://", raw, re.I):
            lines.append(line)
            continue
        seg_rel = _normalize_playlist_rel(base_dir, raw)
        lines.append(f"/hls/{vid}/file?rel={quote(seg_rel, safe='')}")
    return "\n".join(lines) + "\n"


@app.route("/playlist/<vid>.m3u8")
def playlist_m3u8(vid: str):
    """HLS：支持自建 TS 合集，或磁盘上的 .m3u8（改写分片地址）。"""
    item = find_video_by_id(vid)
    if not item:
        abort(404)
    kind = item.get("kind") or ""

    if kind == "m3u8" or (item.get("ext") or "").lower() == ".m3u8":
        path = resolve_under_root(item.get("rel") or "")
        if not path:
            abort(404)
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            abort(404)
        body = rewrite_m3u8_for_proxy(text, item["rel"], vid)
        return Response(
            body,
            mimetype="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache"},
        )

    if kind != "ts_set":
        abort(404)
    segments = item.get("segments") or []
    if len(segments) < 2:
        abort(404)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:30",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    for i in range(len(segments)):
        lines.append("#EXTINF:10.0,")
        lines.append(f"/stream/{vid}/seg/{i}")
    lines.append("#EXT-X-ENDLIST")
    return Response(
        "\n".join(lines) + "\n",
        mimetype="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache"},
    )


@app.route("/hls/<vid>/file")
def hls_proxy_file(vid: str):
    """代理 m3u8 分片/子列表（必须属于当前条目所在目录树）。"""
    item = find_video_by_id(vid)
    if not item:
        abort(404)
    rel = (request.args.get("rel") or "").replace("\\", "/").strip("/")
    if not rel:
        abort(404)
    # 限制：分片须在播放列表所在目录或其子目录下
    base = str(Path((item.get("rel") or "x")).parent).replace("\\", "/")
    if base == ".":
        base = ""
    if base and not (rel == base or rel.startswith(base + "/")):
        # 也允许与 playlist 同级的相对解析结果
        pl_folder = (item.get("folder") or "").strip("/")
        if pl_folder and not (rel == pl_folder or rel.startswith(pl_folder + "/")):
            abort(403)
    path = resolve_under_root(rel)
    if not path:
        abort(404)
    if path.suffix.lower() == ".m3u8":
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            abort(404)
        body = rewrite_m3u8_for_proxy(text, rel, vid)
        return Response(
            body,
            mimetype="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache"},
        )
    mime = mimetypes.guess_type(str(path))[0] or "video/mp2t"
    return _stream_file(path, mime)


def _stream_file(path: Path, mime: str | None = None):
    mime = mime or mimetypes.guess_type(str(path))[0] or "video/mp2t"
    try:
        file_size = path.stat().st_size
    except OSError:
        abort(404)
    range_header = request.headers.get("Range")

    if not range_header:
        return send_file(path, mimetype=mime, conditional=True)

    m = re.match(r"bytes=(\d+)-(\d*)", range_header)
    if not m:
        abort(400)
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else file_size - 1
    end = min(end, file_size - 1)
    if start > end or start >= file_size:
        abort(416)
    length = end - start + 1

    def generate():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            chunk = 1024 * 256
            while remaining > 0:
                data = f.read(min(chunk, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    resp = Response(generate(), status=206, mimetype=mime, direct_passthrough=True)
    resp.headers.add("Content-Range", f"bytes {start}-{end}/{file_size}")
    resp.headers.add("Accept-Ranges", "bytes")
    resp.headers.add("Content-Length", str(length))
    return resp


@app.route("/stream/<vid>/seg/<int:idx>")
def stream_seg(vid: str, idx: int):
    item = find_video_by_id(vid)
    if not item or item.get("kind") != "ts_set":
        abort(404)
    segments = item.get("segments") or []
    if idx < 0 or idx >= len(segments):
        abort(404)
    path = resolve_video_path(segments[idx])
    if not path:
        abort(404)
    return _stream_file(path, "video/mp2t")


@app.route("/stream/<vid>")
def stream(vid: str):
    item = find_video_by_id(vid)
    if not item:
        abort(404)
    # 分片集合：单文件直链播第一段（预览/兼容）；完整观看用 /playlist/
    path = resolve_video_path(item["rel"])
    if not path:
        abort(404)
    mime = mimetypes.guess_type(str(path))[0] or "video/mp4"
    return _stream_file(path, mime)


@app.route("/api/info/<vid>")
def api_info(vid: str):
    item = find_video_by_id(vid)
    if not item:
        abort(404)
    # 懒加载时长
    if not item.get("duration") and STATE["ffmpeg"]:
        path = resolve_video_path(item["rel"])
        if path:
            d = probe_duration(STATE["ffmpeg"], path)
            if d:
                item["duration"] = d
                item["duration_h"] = format_duration(d)
    payload = {k: v for k, v in item.items() if k not in ("_q", "segments")}
    # 本地路径（供复制 / 系统播放器）
    local = _local_path_for_item(item)
    payload["path"] = str(local) if local else ""
    ext = (item.get("ext") or "").lower()
    kind = item.get("kind") or ""
    payload["browser_ok"] = (
        kind in ("m3u8", "ts_set")
        or ext in BROWSER_FRIENDLY_EXTS
    )
    payload["browser_hard"] = ext in BROWSER_HARD_EXTS and kind not in ("m3u8", "ts_set")
    if kind == "ts_set":
        payload["seg_count"] = item.get("seg_count") or len(item.get("segments") or [])
        payload["kind"] = "ts_set"
    return jsonify(payload)


def _local_path_for_item(item: dict) -> Path | None:
    if item.get("kind") == "ts_set" and item.get("segments"):
        return resolve_under_root(item["segments"][0])
    return resolve_under_root(item.get("rel") or "")


@app.route("/api/local/<vid>", methods=["POST"])
def api_local(vid: str):
    """本机操作：open=系统播放器打开，reveal=资源管理器定位，path=仅返回路径。"""
    item = find_video_by_id(vid)
    if not item:
        return jsonify({"ok": False, "msg": "未找到视频"}), 404
    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "path").strip().lower()
    path = _local_path_for_item(item)
    if not path:
        return jsonify({"ok": False, "msg": "文件不存在"}), 404
    path_str = str(path)

    if action == "path":
        return jsonify({"ok": True, "path": path_str})

    if action == "open":
        try:
            if sys.platform == "win32":
                os.startfile(path_str)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path_str])
            else:
                subprocess.Popen(["xdg-open", path_str])
            log(f"[本地] 已用系统播放器打开: {path_str}")
            return jsonify({"ok": True, "path": path_str, "msg": "已调用系统播放器"})
        except Exception as e:
            return jsonify({"ok": False, "msg": f"打开失败: {e}", "path": path_str}), 500

    if action == "reveal":
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", path_str])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path_str])
            else:
                subprocess.Popen(["xdg-open", str(path.parent)])
            return jsonify({"ok": True, "path": path_str, "msg": "已在文件夹中显示"})
        except Exception as e:
            return jsonify({"ok": False, "msg": f"定位失败: {e}", "path": path_str}), 500

    return jsonify({"ok": False, "msg": "未知操作"}), 400


def _sanitize_filename(name: str) -> str:
    name = (name or "video").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.rstrip(" .")
    return name[:120] or "video"


def _unique_mp4_path(out_dir: Path, base_name: str) -> Path:
    stem = _sanitize_filename(base_name)
    candidate = out_dir / f"{stem}.mp4"
    n = 1
    while candidate.exists():
        candidate = out_dir / f"{stem}_{n}.mp4"
        n += 1
    return candidate


def _path_under_root(path: Path) -> bool:
    root = STATE.get("root")
    if not root or not path:
        return False
    try:
        path.resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def _convert_job_update(job_id: str, **kwargs) -> None:
    with _convert_lock:
        job = STATE["convert_jobs"].get(job_id)
        if not job:
            return
        job.update(kwargs)


def _parse_ffmpeg_time_seconds(line: str) -> float | None:
    m = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", line)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _run_ffmpeg_convert(
    job_id: str,
    ffmpeg: str,
    input_args: list[str],
    out_path: Path,
    duration_hint: float | None = None,
) -> tuple[bool, str]:
    """先 copy 封装，失败再重编码。返回 (ok, msg)。"""
    attempts = [
        (
            "封装",
            input_args
            + ["-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(out_path)],
        ),
        (
            "转码",
            input_args
            + [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                str(out_path),
            ],
        ),
    ]
    last_err = ""
    for label, cmd_tail in attempts:
        try:
            if out_path.exists():
                out_path.unlink()
        except OSError:
            pass
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "info"] + cmd_tail
        _convert_job_update(job_id, status="running", msg=f"正在{label}…", percent=0)
        log(f"[转MP4] {label}: {' '.join(cmd[:8])} … → {out_path.name}")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="ignore",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
            )
            with _convert_lock:
                job = STATE["convert_jobs"].get(job_id)
                if job is not None:
                    job["proc"] = proc
            err_chunks: list[str] = []
            assert proc.stderr is not None
            for line in proc.stderr:
                err_chunks.append(line)
                if len(err_chunks) > 40:
                    err_chunks = err_chunks[-40:]
                t = _parse_ffmpeg_time_seconds(line)
                if t is not None and duration_hint and duration_hint > 0:
                    pct = max(0, min(99, int(t * 100 / duration_hint)))
                    _convert_job_update(job_id, percent=pct, msg=f"正在{label}… {pct}%")
            code = proc.wait()
            with _convert_lock:
                job = STATE["convert_jobs"].get(job_id)
                if job is not None:
                    job["proc"] = None
            if code == 0 and out_path.is_file() and out_path.stat().st_size > 0:
                return True, f"{label}完成"
            last_err = "".join(err_chunks[-12:]).strip() or f"ffmpeg 退出码 {code}"
            log(f"[转MP4] {label}失败: {last_err[:200]}")
        except Exception as e:
            last_err = str(e)
            log(f"[转MP4] {label}异常: {e}")
    return False, last_err or "转换失败"


def _prepare_convert_input(item: dict) -> tuple[list[str], Path, Path | None, float | None]:
    """
    返回 (ffmpeg -i 前的参数含 -i, 输出目录, 临时文件或None, 时长提示)。
    """
    kind = item.get("kind") or ""
    root = Path(STATE["root"])
    duration = item.get("duration")
    duration_f = float(duration) if duration else None
    tmp_path: Path | None = None

    if kind == "m3u8" or (item.get("ext") or "").lower() == ".m3u8":
        pl = resolve_under_root(item.get("rel") or "")
        if not pl:
            raise FileNotFoundError("找不到 m3u8 文件")
        out_dir = pl.parent
        # 允许本地 m3u8 引用同目录/子目录 .ts 分片
        return [
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-allowed_extensions", "ALL",
            "-i", str(pl),
        ], out_dir, None, duration_f

    if kind == "ts_set":
        segs = item.get("segments") or []
        if len(segs) < 2:
            raise ValueError("分片不足，无法转换")
        paths: list[Path] = []
        for rel in segs:
            p = resolve_under_root(rel)
            if not p:
                raise FileNotFoundError(f"缺少分片: {rel}")
            paths.append(p)
        folder = (item.get("folder") or "").strip("/").replace("\\", "/")
        out_dir = (root / folder) if folder else root
        out_dir.mkdir(parents=True, exist_ok=True)
        cache = STATE.get("cache_dir") or (VGDATA_DIR)
        cache.mkdir(parents=True, exist_ok=True)
        tmp_path = cache / f"convert_{item.get('id') or 'tmp'}.ffconcat"
        lines = []
        for p in paths:
            s = str(p.resolve()).replace("\\", "/")
            s = s.replace("'", r"'\''")
            lines.append(f"file '{s}'")
        tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return ["-f", "concat", "-safe", "0", "-i", str(tmp_path)], out_dir, tmp_path, duration_f

    raise ValueError("仅支持 m3u8 / TS 合集")


def _convert_worker(job_id: str, vid: str) -> None:
    tmp_path: Path | None = None
    try:
        item = find_video_by_id(vid)
        if not item:
            _convert_job_update(job_id, status="error", msg="未找到视频", percent=0)
            return
        ffmpeg = STATE.get("ffmpeg")
        if not ffmpeg:
            _convert_job_update(job_id, status="error", msg="未找到 ffmpeg", percent=0)
            return
        input_args, out_dir, tmp_path, duration_hint = _prepare_convert_input(item)
        if not _path_under_root(out_dir):
            _convert_job_update(job_id, status="error", msg="输出目录不在扫描根下", percent=0)
            return
        base_name = item.get("name") or Path(item.get("filename") or "video").stem
        out_path = _unique_mp4_path(out_dir, base_name)
        if not _path_under_root(out_path):
            _convert_job_update(job_id, status="error", msg="输出路径非法", percent=0)
            return
        _convert_job_update(
            job_id,
            status="running",
            msg="开始转换…",
            percent=0,
            out_path=str(out_path),
        )
        ok, msg = _run_ffmpeg_convert(job_id, ffmpeg, input_args, out_path, duration_hint)
        if ok:
            _convert_job_update(
                job_id,
                status="done",
                msg=f"已保存：{out_path}",
                percent=100,
                out_path=str(out_path),
            )
            log(f"[转MP4] 完成 {vid} → {out_path}")
        else:
            try:
                if out_path.exists() and out_path.stat().st_size == 0:
                    out_path.unlink(missing_ok=True)
            except OSError:
                pass
            _convert_job_update(job_id, status="error", msg=msg[:500] or "转换失败", percent=0)
    except Exception as e:
        _convert_job_update(job_id, status="error", msg=str(e), percent=0)
        log(f"[转MP4] 任务失败 {vid}: {e}")
    finally:
        if tmp_path:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


@app.route("/api/convert-mp4/<vid>", methods=["POST"])
def api_convert_mp4_start(vid: str):
    """将 m3u8 / ts_set 转为同目录 MP4（后台任务）。"""
    if not re.fullmatch(r"[a-f0-9]{16}", vid or ""):
        return jsonify({"ok": False, "msg": "无效 id"}), 400
    if not STATE.get("ffmpeg"):
        return jsonify({"ok": False, "msg": "未找到 ffmpeg，请先安装后再试"}), 400
    if not STATE.get("root"):
        return jsonify({"ok": False, "msg": "尚未选择盘符"}), 400
    item = find_video_by_id(vid)
    if not item:
        return jsonify({"ok": False, "msg": "未找到视频"}), 404
    kind = item.get("kind") or ""
    if kind not in ("m3u8", "ts_set") and (item.get("ext") or "").lower() != ".m3u8":
        return jsonify({"ok": False, "msg": "仅支持 m3u8 / TS 合集"}), 400

    with _convert_lock:
        for jid, job in STATE["convert_jobs"].items():
            if job.get("vid") == vid and job.get("status") in ("queued", "running"):
                return jsonify({
                    "ok": True,
                    "job_id": jid,
                    "msg": "已有转换任务进行中",
                    "status": job.get("status"),
                })
        job_id = hashlib.md5(f"{vid}-{datetime.now().timestamp()}".encode()).hexdigest()[:12]
        STATE["convert_jobs"][job_id] = {
            "id": job_id,
            "vid": vid,
            "status": "queued",
            "msg": "排队中…",
            "percent": 0,
            "out_path": "",
            "proc": None,
        }

    threading.Thread(
        target=_convert_worker,
        args=(job_id, vid),
        daemon=True,
        name=f"convert-mp4-{vid[:8]}",
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "msg": "已开始转换", "status": "queued"})


@app.route("/api/convert-mp4/job/<job_id>")
def api_convert_mp4_status(job_id: str):
    with _convert_lock:
        job = STATE["convert_jobs"].get(job_id)
        if not job:
            return jsonify({"ok": False, "msg": "任务不存在"}), 404
        return jsonify({
            "ok": True,
            "job_id": job_id,
            "vid": job.get("vid") or "",
            "status": job.get("status") or "error",
            "msg": job.get("msg") or "",
            "percent": int(job.get("percent") or 0),
            "out_path": job.get("out_path") or "",
        })


@app.route("/api/export-static", methods=["POST"])
def api_export_static():
    """导出纯静态站到视频盘根目录/_video_gallery_static/（后台线程，不挡浏览）。"""
    if STATE.get("exporting"):
        return jsonify({"ok": False, "msg": "正在导出中，请稍候…", "exporting": True})
    if not STATE.get("root"):
        return jsonify({"ok": False, "msg": "请先打开/扫描一个盘"}), 400
    if not (STATE.get("videos") or []):
        return jsonify({"ok": False, "msg": "当前没有可导出的视频"}), 400
    if STATE.get("scanning"):
        return jsonify({"ok": False, "msg": "扫描进行中，请稍后再导出"}), 400

    data = request.get_json(silent=True) or {}
    open_folder = bool(data.get("open_folder", True))

    def job() -> None:
        STATE["exporting"] = True
        STATE["export_ok"] = None
        STATE["export_msg"] = "正在导出静态站…"
        STATE["export_path"] = ""
        try:
            ok, msg, path = export_static_site()
            STATE["export_ok"] = ok
            STATE["export_msg"] = msg
            STATE["export_path"] = path or ""
            if ok and open_folder and path:
                try:
                    if sys.platform == "win32":
                        os.startfile(path)  # type: ignore[attr-defined]
                    else:
                        subprocess.Popen(["xdg-open", path])
                except Exception as e:
                    log(f"[静态导出] 打开目录失败: {e}")
        except Exception as e:
            STATE["export_ok"] = False
            STATE["export_msg"] = f"导出失败: {e}"
            log(f"[静态导出] 异常: {e}")
        finally:
            STATE["exporting"] = False

    threading.Thread(target=job, daemon=True, name="export-static").start()
    return jsonify({"ok": True, "msg": "已开始导出静态站，完成后会打开文件夹", "exporting": True})


@app.route("/api/export-static/status")
def api_export_static_status():
    return jsonify({
        "exporting": bool(STATE.get("exporting")),
        "ok": STATE.get("export_ok"),
        "msg": STATE.get("export_msg") or "",
        "path": STATE.get("export_path") or "",
    })


@app.route("/api/export-static/reveal", methods=["POST"])
def api_export_static_reveal():
    root = STATE.get("root")
    path = STATE.get("export_path") or ""
    if not path and root:
        path = str(Path(root) / STATIC_EXPORT_DIRNAME)
    if not path or not Path(path).is_dir():
        return jsonify({"ok": False, "msg": "尚未导出，或目录不存在"}), 404
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return jsonify({"ok": True, "path": path})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e), "path": path}), 500


def fail(msg: str, detail: str = "", code: int = 1) -> None:
    """打印醒目错误提示后退出（窗口由 start.bat 的 pause 保持）。"""
    print()
    print("=" * 50)
    print(f"【错误】{msg}")
    if detail:
        print(detail)
    print("=" * 50)
    sys.exit(code)


def main():
    parser = argparse.ArgumentParser(description="本地视频库 — 浏览器分类浏览播放")
    parser.add_argument("root", nargs="?", help="视频根目录，例如 D:\\Videos")
    parser.add_argument("--port", type=int, default=8765, help="端口，默认 8765")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址")
    parser.add_argument("--no-thumbs", action="store_true", help="不生成预览图")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--rescan", action="store_true", help="忽略缓存强制重扫")
    args = parser.parse_args()

    try:
        if args.root:
            root = Path(args.root).expanduser().resolve()
        else:
            root = default_scan_root().resolve()
    except SystemExit as e:
        # default_scan_root 等主动退出
        msg = e.code if isinstance(e.code, str) else "启动失败"
        fail(str(msg))
    except OSError as e:
        fail("无法访问指定路径", str(e))

    if not root.is_dir():
        fail("目录不存在或不是文件夹", str(root))

    # 探测是否有读权限
    try:
        next(root.iterdir(), None)
    except PermissionError:
        fail("没有权限读取该盘/目录", f"路径: {root}\n请用管理员运行，或换一个目录。")
    except OSError as e:
        fail("无法读取该盘/目录", f"路径: {root}\n原因: {e}")

    STATE["ffmpeg"] = find_ffmpeg()
    if not STATE["ffmpeg"]:
        print("提示: 未检测到 ffmpeg，将无法生成预览图（不影响播放）。")
        print("  安装方式: winget install ffmpeg  或从 https://ffmpeg.org 下载")
    else:
        print(f"ffmpeg: {STATE['ffmpeg']}")

    print(f"扫描目录: {root}")
    print(f"预览图目录: {VGDATA_DIR.resolve()}（程序根目录，文件已加密）")
    print("正在后台加载/扫描，网页会先打开，列表随后刷新…")
    try:
        # 与页面操作共用 start_scan，避免与「打开此盘」并发冲突
        ok, msg = start_scan(root, do_thumbs=not args.no_thumbs, force=args.rescan)
        if not ok:
            print(f"提示: {msg}")
    except Exception as e:
        fail("扫描视频时出错", f"{type(e).__name__}: {e}")

    url = f"http://{args.host}:{args.port}"
    print(f"\n本地视频库已启动 → {url}")
    print("浏览器打开上述地址即可浏览。按 Ctrl+C 停止。\n")

    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        _run_server(args.host, args.port)
    except OSError as e:
        err = str(e).lower()
        if "address already in use" in err or "10048" in str(e) or "通常每个套接字地址" in str(e):
            fail(
                f"端口 {args.port} 已被占用",
                f"可能已有一个视频库在运行。\n"
                f"请关掉旧窗口，或换端口启动:\n"
                f'  python app.py --port 8766',
            )
        fail("无法启动网页服务", str(e))
    except KeyboardInterrupt:
        print("\n已停止。")
        sys.exit(0)


def _run_server(host: str, port: int) -> None:
    """本机浏览用 waitress；未安装时回退 Flask（并关闭开发服务器提示）。"""
    try:
        from waitress import serve
    except ImportError:
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "waitress", "-q"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            from waitress import serve
        except Exception:
            serve = None  # type: ignore
    if serve is not None:
        log(f"[服务] waitress 监听 http://{host}:{port}")
        serve(app, host=host, port=port, threads=16)
        return
    # 回退：本机自用，隐藏 Flask 开发服务器横幅
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    try:
        import flask.cli as flask_cli
        flask_cli.show_server_banner = lambda *a, **k: None  # type: ignore
    except Exception:
        pass
    log(f"[服务] Flask 回退监听 http://{host}:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        fail("程序异常退出", f"{type(e).__name__}: {e}")
