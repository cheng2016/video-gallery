# -*- coding: utf-8 -*-
"""Delete files to recycle bin (Windows) / trash helpers."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def move_to_trash(path: Path) -> tuple[bool, str]:
    """
    移到回收站。成功 (True, msg)；失败不硬删，返回 (False, err)。
    """
    path = Path(path)
    if not path.exists():
        return False, "文件不存在"
    path_str = str(path.resolve())

    if sys.platform == "win32":
        # VisualBasic FileIO → 回收站
        ps = (
            "Add-Type -AssemblyName Microsoft.VisualBasic; "
            f"[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile("
            f"'{path_str.replace(chr(39), chr(39)+chr(39))}', "
            "'OnlyErrorDialogs', 'SendToRecycleBin')"
        )
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if r.returncode == 0 and not path.exists():
                return True, "已移到回收站"
            # 目录
            ps2 = (
                "Add-Type -AssemblyName Microsoft.VisualBasic; "
                f"[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory("
                f"'{path_str.replace(chr(39), chr(39)+chr(39))}', "
                "'OnlyErrorDialogs', 'SendToRecycleBin')"
            )
            r2 = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps2],
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if r2.returncode == 0 and not path.exists():
                return True, "已移到回收站"
            err = (r.stderr or r2.stderr or r.stdout or r2.stdout or "").strip()
            return False, err[:300] or "无法移到回收站"
        except Exception as e:
            return False, str(e)

    # macOS
    if sys.platform == "darwin":
        try:
            # 路径作为 argv 传给 AppleScript，避免引号、反斜杠等字符被当脚本解析。
            result = subprocess.run(
                [
                    "osascript",
                    "-e", "on run argv",
                    "-e", 'tell application "Finder" to delete POSIX file (item 1 of argv)',
                    "-e", "end run",
                    path_str,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if path.exists():
                detail = (result.stderr or result.stdout or "").strip()
                return False, detail[:300] or "Finder 未能把文件移到废纸篓"
            return True, "已移到废纸篓"
        except Exception as e:
            return False, str(e)

    # Linux: gio trash
    try:
        subprocess.run(["gio", "trash", path_str], check=True, timeout=30)
        return True, "已移到回收站"
    except Exception:
        pass
    return False, "当前系统无法安全移到回收站（已拒绝硬删除）"
