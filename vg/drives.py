# -*- coding: utf-8 -*-
"""Drive discovery and user prefs."""
from __future__ import annotations

import os
import shutil
import sys


import json
import string
from pathlib import Path

from vg.config import PREFS_FILE, VGDATA_DIR
from vg.util import _fmt_bytes, log

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


def _mounted_entry(path: Path, label: str | None = None) -> dict | None:
    """Build a drive-style entry for a POSIX mount or user media directory."""
    try:
        path = path.expanduser().resolve()
        if not path.is_dir():
            return None
    except OSError:
        return None
    free_h = total_h = ""
    try:
        usage = shutil.disk_usage(path)
        free_h = _fmt_bytes(usage.free)
        total_h = _fmt_bytes(usage.total)
    except OSError:
        pass
    name = label or path.name or str(path)
    return {
        "letter": name,
        "path": str(path),
        "label": name,
        "type": "目录" if path.is_relative_to(Path.home()) else "挂载卷",
        "free_h": free_h,
        "total_h": total_h,
        "display": name,
    }


def list_drives_info() -> list[dict]:
    """供前端选择的磁盘/媒体目录列表（尽量简单可靠）。"""
    drives: list[dict] = []
    seen: set[str] = set()

    def add(entry: dict | None) -> None:
        if not entry:
            return
        key = os.path.normcase(os.path.abspath(entry["path"]))
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
    elif sys.platform == "darwin":
        # 用户视频目录是 Mac 上最常见的源码模式入口；外置盘统一挂在
        # /Volumes。不要枚举 /，否则会把 System、Applications 等当片库。
        for folder_name in ("Movies", "Videos"):
            add(_mounted_entry(Path.home() / folder_name, folder_name))
        volumes = Path("/Volumes")
        if volumes.is_dir():
            try:
                for p in sorted(volumes.iterdir(), key=lambda item: item.name.casefold()):
                    try:
                        if p.resolve() == Path("/").resolve():
                            continue
                    except OSError:
                        pass
                    add(_mounted_entry(p))
            except OSError:
                pass
    else:
        for base in (Path("/media"), Path("/mnt")):
            if not base.is_dir():
                continue
            try:
                for p in sorted(base.iterdir()):
                    add(_mounted_entry(p))
            except OSError:
                continue

    return drives


def list_lan_ipv4() -> list[str]:
    """本机局域网 IPv4（用于分享提示）。"""
    ips: list[str] = []
    try:
        import socket
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    # UDP 探测默认出口网卡
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.insert(0, ip)
        finally:
            s.close()
    except Exception:
        pass
    return ips


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
    """优先上次打开的目录；否则选择安全的默认媒体目录。"""
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
    if sys.platform == "darwin":
        movies = (Path.home() / "Movies").resolve()
        default_drive = movies if movies in drives else drives[0]
    else:
        default_drive = drives[-1]
    print(f"检测到磁盘/目录: {', '.join(str(d) for d in drives)}")
    print(f"默认扫描: {default_drive}")
    if len(drives) == 1 and str(default_drive).upper().startswith("C:"):
        print("提示: 当前只有 C 盘，整盘扫描可能较慢，且会跳过 Windows 系统目录。")
        print('      若视频在子文件夹，建议指定目录: python app.py "C:\\Videos"')
    return default_drive


