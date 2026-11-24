# -*- coding: utf-8 -*-
"""Delete files to recycle bin (Windows) / trash helpers."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from vg.diagnostics import perf as diagnostic_perf


def move_to_trash(path: Path) -> tuple[bool, str]:
    """
    移到回收站。成功 (True, msg)；失败不硬删，返回 (False, err)。
    """
    path = Path(path)
    if not path.exists():
        return False, "文件不存在"
    path_str = str(path.resolve())

    if sys.platform == "win32":
        # VisualBasic FileIO → 回收站。文件失败后再试目录，两次合成一条日志。
        quoted = path_str.replace(chr(39), chr(39) + chr(39))
        ps = (
            "Add-Type -AssemblyName Microsoft.VisualBasic; "
            f"[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile("
            f"'{quoted}', "
            "'OnlyErrorDialogs', 'SendToRecycleBin')"
        )
        file_ms = 0.0
        dir_ms = 0.0
        file_rc = None
        dir_rc = None
        tried_dir = False
        error_text = ""
        t_all = time.perf_counter()
        try:
            t_attempt = time.perf_counter()
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            file_ms = (time.perf_counter() - t_attempt) * 1000.0
            file_rc = r.returncode
            if r.returncode == 0 and not path.exists():
                outcome = (True, "已移到回收站")
            else:
                tried_dir = True
                ps2 = (
                    "Add-Type -AssemblyName Microsoft.VisualBasic; "
                    f"[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory("
                    f"'{quoted}', "
                    "'OnlyErrorDialogs', 'SendToRecycleBin')"
                )
                t_attempt = time.perf_counter()
                r2 = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps2],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                dir_ms = (time.perf_counter() - t_attempt) * 1000.0
                dir_rc = r2.returncode
                if r2.returncode == 0 and not path.exists():
                    outcome = (True, "已移到回收站")
                else:
                    err = (r.stderr or r2.stderr or r.stdout or r2.stdout or "").strip()
                    outcome = (False, err[:300] or "无法移到回收站")
        except Exception as e:
            error_text = str(e)[:200]
            outcome = (False, str(e))
        elapsed = (time.perf_counter() - t_all) * 1000.0
        try:
            fields = {
                "op": "dir" if tried_dir else "file",
                "ok": outcome[0],
                "path": path_str,
                "file_ms": round(file_ms, 1),
            }
            if tried_dir:
                fields["dir_ms"] = round(dir_ms, 1)
            if error_text:
                fields["error"] = error_text
            elif not outcome[0]:
                if file_rc is not None:
                    fields["file_rc"] = file_rc
                if dir_rc is not None:
                    fields["dir_rc"] = dir_rc
            diagnostic_perf(
                "trash_ps_error" if error_text else "trash_ps",
                elapsed,
                force=not outcome[0],
                **fields,
            )
        except Exception:
            pass
        return outcome

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
