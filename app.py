#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local video gallery entry. Logic lives in vg/."""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _app_dir() -> Path:
    if _frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _safe_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass


def _message_box(title: str, text: str) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(text)[:1000], str(title)[:100], 0x10)
    except Exception:
        pass


def _pause() -> None:
    if not _frozen():
        return
    try:
        input("按回车键退出…")
    except Exception:
        try:
            import msvcrt
            print("按任意键退出…")
            msvcrt.getch()
        except Exception:
            pass


def _early_log(text: str) -> Path | None:
    """在 vg 包 import 之前也能落盘。"""
    try:
        p = _app_dir() / "startup.log"
        with p.open("a", encoding="utf-8", errors="replace") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {text.rstrip()}\n")
            f.flush()
        return p
    except Exception:
        return None


if __name__ == "__main__":
    if _frozen():
        try:
            from multiprocessing import freeze_support
            freeze_support()
        except Exception:
            pass
        try:
            os.chdir(_app_dir())
        except OSError:
            pass
    _safe_stdio()

    early = _early_log(
        f"[early] pid={os.getpid()} frozen={_frozen()} exe={sys.executable!r} cwd={os.getcwd()!r}"
    )

    bl = None
    try:
        from vg import bootlog as bl
        # 追加会话，保留上次闪退记录方便对照
        bl.init(reset=False)
        bl.step("bootstrap_done")
    except Exception:
        _early_log("[early] bootlog import failed:\n" + traceback.format_exc())

    try:
        if bl:
            bl.step("import_web")
        from vg.web import app  # noqa: F401
        if bl:
            bl.step("import_main")
        from vg.main import main

        if bl:
            bl.step("enter_main")
        main()
        if bl:
            bl.step("main_returned")
    except SystemExit as e:
        if bl:
            bl.write(f"SystemExit code={e.code!r}")
        log_file = str(bl.log_path()) if bl else str(early)
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        if code not in (0, None):
            if _frozen() and not getattr(sys, "_vg_paused", False):
                _message_box("本地视频库 — 启动失败", f"退出码: {e.code}\n详情见 {log_file}")
                _pause()
        raise
    except Exception as e:
        if bl:
            bl.exception("startup")
            log_file = str(bl.log_path())
        else:
            log_file = str(early)
            _early_log(traceback.format_exc())
        print()
        print("=" * 50)
        print("【错误】启动失败")
        print(f"{type(e).__name__}: {e}")
        print("=" * 50)
        print(f"日志: {log_file}")
        if _frozen():
            _message_box("本地视频库 — 启动失败", f"{type(e).__name__}: {e}\n\n详见:\n{log_file}")
            _pause()
        raise SystemExit(1) from e
else:
    from vg.web import app  # noqa: F401
    from vg.main import main  # noqa: F401
