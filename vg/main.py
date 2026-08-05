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
from vg.drives import default_scan_root, load_prefs, save_prefs
from vg.media import find_ffmpeg
from vg.scan import start_scan
from vg.state import STATE
from vg.util import log
from vg.web import app
from vg import bootlog


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _pause_console(hint: str = "按回车键退出…") -> None:
    """双击 exe 时错误一闪而过；frozen 下暂停以便看清原因。"""
    if not _is_frozen():
        return
    sys._vg_paused = True  # type: ignore[attr-defined]
    try:
        input(hint)
        return
    except Exception:
        pass
    try:
        import msvcrt
        print(hint)
        msvcrt.getch()
        return
    except Exception:
        pass


def fail(msg: str, detail: str = "", code: int = 1) -> None:
    """打印醒目错误提示后退出（源码靠 start.bat pause；exe 自行暂停）。"""
    print()
    print("=" * 50)
    print(f"【错误】{msg}")
    if detail:
        print(detail)
    print("=" * 50)
    bootlog.fail(msg, detail)
    if _is_frozen():
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                f"{msg}\n\n{detail}\n\n日志: {bootlog.log_path()}"[:1000],
                "本地视频库 — 错误",
                0x10,
            )
        except Exception:
            pass
    _pause_console()
    sys.exit(code)


def main():
    bootlog.step("main_begin")
    if _is_frozen():
        try:
            from multiprocessing import freeze_support
            freeze_support()
        except Exception:
            pass
        try:
            os.chdir(Path(sys.executable).resolve().parent)
            bootlog.write(f"chdir -> {os.getcwd()!r}")
        except OSError as e:
            bootlog.write(f"chdir failed: {e}")

    parser = argparse.ArgumentParser(description="本地视频库 — 浏览器分类浏览播放")
    parser.add_argument("root", nargs="?", help="视频根目录，例如 D:\\Videos")
    parser.add_argument("--port", type=int, default=8765, help="端口，默认 8765")
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="绑定地址，默认 0.0.0.0（局域网开关用访问控制，无需改绑定）",
    )
    parser.add_argument("--lan", action="store_true", help="启动时开启局域网访问")
    parser.add_argument("--no-lan", action="store_true", help="启动时关闭局域网访问（仅本机）")
    parser.add_argument("--no-thumbs", action="store_true", help="不生成预览图")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--rescan", action="store_true", help="忽略缓存强制重扫")
    args = parser.parse_args()

    prefs = load_prefs()
    want_lan = bool(prefs.get("lan_share"))
    if args.lan:
        want_lan = True
        save_prefs(lan_share=True)
    if args.no_lan:
        want_lan = False
        save_prefs(lan_share=False)
    # 始终监听所有网卡；「仅本机」由 Flask 访问控制实现，开关可即时切换
    host = args.host or "0.0.0.0"
    if host in ("127.0.0.1", "localhost"):
        # 显式本机绑定时无法被局域网访问；仍允许本地使用
        want_lan = False
    args.host = host
    STATE["bind_host"] = host
    STATE["bind_port"] = int(args.port)
    STATE["lan_share"] = bool(want_lan)
    try:
        cp = int(prefs.get("convert_parallel") or 1)
        STATE["convert_parallel"] = max(1, min(4, cp))
    except (TypeError, ValueError):
        STATE["convert_parallel"] = 1
    bootlog.step(
        "args",
        f"root={args.root!r} host={args.host} port={args.port} lan={STATE['lan_share']} "
        f"no_thumbs={args.no_thumbs} no_open={args.no_open} rescan={args.rescan}",
    )

    try:
        if args.root:
            root = Path(args.root).expanduser().resolve()
            bootlog.step("root_from_argv", str(root))
        else:
            bootlog.step("root_default_begin")
            root = default_scan_root().resolve()
            bootlog.step("root_default", str(root))
    except SystemExit as e:
        msg = e.code if isinstance(e.code, str) else "启动失败"
        fail(str(msg))
    except OSError as e:
        fail("无法访问指定路径", str(e))

    if not root.is_dir():
        fail("目录不存在或不是文件夹", str(root))

    try:
        next(root.iterdir(), None)
        bootlog.step("root_readable", str(root))
    except PermissionError:
        fail("没有权限读取该盘/目录", f"路径: {root}\n请用管理员运行，或换一个目录。")
    except OSError as e:
        fail("无法读取该盘/目录", f"路径: {root}\n原因: {e}")

    bootlog.step("find_ffmpeg")
    STATE["ffmpeg"] = find_ffmpeg()
    bootlog.write(f"ffmpeg={STATE['ffmpeg']!r}")
    if not STATE["ffmpeg"]:
        print("提示: 未检测到 ffmpeg，将无法生成预览图（不影响播放）。")
        print("  安装方式: winget install ffmpeg  或从 https://ffmpeg.org 下载")
    else:
        print(f"ffmpeg: {STATE['ffmpeg']}")

    print(f"扫描目录: {root}")
    print(f"预览图目录: {VGDATA_DIR.resolve()}（程序根目录，文件已加密）")
    print("正在后台加载/扫描，网页会先打开，列表随后刷新…")
    bootlog.step("start_scan", f"root={root} thumbs={not args.no_thumbs} force={args.rescan}")
    try:
        from vg.disk_libs import ensure_library
        from vg.roots import set_mounted_roots

        saved = prefs.get("mounted_roots") or []
        valid = []
        if isinstance(saved, list):
            for p in saved:
                try:
                    pp = Path(p).expanduser().resolve()
                    if pp.is_dir():
                        valid.append(str(pp))
                except OSError:
                    pass
        root_s = str(root.resolve())
        if root_s.lower() not in {v.lower() for v in valid}:
            valid = [root_s] + valid
        if len(valid) > 1:
            set_mounted_roots(valid, primary=root_s)
            for m in valid:
                if m.lower() != root_s.lower():
                    ensure_library(m)
            ok, msg = start_scan(root, do_thumbs=not args.no_thumbs, force=args.rescan, replace_mounts=False)
        else:
            ok, msg = start_scan(root, do_thumbs=not args.no_thumbs, force=args.rescan, replace_mounts=True)
        bootlog.step("start_scan_done", f"ok={ok} msg={msg!r}")
        if not ok:
            print(f"提示: {msg}")
    except Exception as e:
        bootlog.exception("start_scan")
        fail("扫描视频时出错", f"{type(e).__name__}: {e}")

    url = f"http://127.0.0.1:{args.port}"
    print(f"\n本地视频库已启动 → {url}")
    print(f"监听: {args.host}:{args.port}（局域网开关可在网页即时切换，无需重启）")
    if STATE.get("lan_share"):
        print("局域网分享：已开启。同一 WiFi 下可用：")
        try:
            from vg.drives import list_lan_ipv4
            from vg.lan import ensure_firewall_allow
            for ip in list_lan_ipv4():
                print(f"  http://{ip}:{args.port}")
            ok, msg = ensure_firewall_allow(int(args.port))
            if msg:
                print(msg)
            if not ok:
                print("若手机/电视打不开，请用管理员权限运行一次或手动放行防火墙。")
        except Exception:
            pass
    else:
        print("局域网分享：关闭（仅本机）。网页点「局域网」可立即开启。")
    print("浏览器打开上述地址即可浏览。按 Ctrl+C 停止。\n")
    bootlog.step("ready", url)

    if not args.no_open:
        bootlog.step("open_browser", url)
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        bootlog.step("run_server", f"{args.host}:{args.port}")
        _run_server(args.host, args.port)
        bootlog.step("server_stopped")
    except OSError as e:
        bootlog.exception("run_server")
        err = str(e).lower()
        if "address already in use" in err or "10048" in str(e) or "通常每个套接字地址" in str(e):
            alt = (
                f"VideoGallery.exe --port 8766"
                if _is_frozen()
                else f'python app.py --port 8766'
            )
            fail(
                f"端口 {args.port} 已被占用",
                f"可能已有一个视频库在运行（请关掉旧的黑窗口）。\n"
                f"或换端口启动:\n  {alt}",
            )
        fail("无法启动网页服务", str(e))
    except KeyboardInterrupt:
        bootlog.step("keyboard_interrupt")
        print("\n已停止。")
        sys.exit(0)


def _run_server(host: str, port: int) -> None:
    """本机浏览用 waitress；未安装时回退 Flask（并关闭开发服务器提示）。"""
    serve = None
    try:
        from waitress import serve as _serve
        serve = _serve
    except ImportError:
        if _is_frozen():
            serve = None
        else:
            try:
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", "waitress", "-q"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                from waitress import serve as _serve
                serve = _serve
            except Exception:
                serve = None
    if serve is not None:
        log(f"[服务] waitress 监听 http://{host}:{port}")
        bootlog.step("waitress_serve")
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
    bootlog.step("flask_serve")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        fail("程序异常退出", f"{type(e).__name__}: {e}")

