# -*- coding: utf-8 -*-
"""CLI entry: argparse and server bootstrap."""
from __future__ import annotations

import subprocess


import argparse
import os
import sys
import threading
import webbrowser
from pathlib import Path

from vg.config import VGDATA_DIR
from vg.drives import default_scan_root
from vg.media import find_ffmpeg
from vg.scan import start_scan
from vg.state import STATE
from vg.util import log
from vg.web import app

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

