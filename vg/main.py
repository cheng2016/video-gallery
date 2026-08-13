# -*- coding: utf-8 -*-
"""CLI entry: argparse and server bootstrap."""
from __future__ import annotations

import subprocess


import argparse
import json
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from vg.config import VGDATA_DIR
from vg.drives import default_scan_root, load_prefs, save_prefs
from vg.media import find_ffmpeg
from vg.scan import start_scan
from vg.state import STATE
from vg.util import log
from vg.web import app
from vg import bootlog

APP_ID = "video-gallery"
DEFAULT_PORT = 8765
PORT_TRIES = 30


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


def _is_addr_in_use(exc: BaseException) -> bool:
    err = str(exc).lower()
    return (
        "address already in use" in err
        or "10048" in str(exc)
        or "通常每个套接字地址" in str(exc)
    )


def port_accepting(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def can_bind_port(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in (host or "") and host not in ("0.0.0.0",) else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if host in ("0.0.0.0", "", None):
            sock.bind(("0.0.0.0", int(port)))
        else:
            sock.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def looks_like_gallery_status(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("app") == APP_ID:
        return True
    return (
        "scanning" in data
        and "lan_share" in data
        and "lib_gen" in data
        and "thumb_progress" in data
    )


def probe_own_gallery(port: int, timeout: float = 0.6) -> bool:
    url = f"http://127.0.0.1:{int(port)}/api/status"
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(url, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return looks_like_gallery_status(json.loads(raw))
    except (OSError, URLError, ValueError, TimeoutError):
        return False


def find_free_port(host: str, start: int, attempts: int = PORT_TRIES) -> int | None:
    for port in range(int(start), int(start) + int(attempts)):
        if port_accepting(port):
            continue
        if can_bind_port(host, port):
            return port
    return None


def choose_listen_port(
    host: str,
    preferred: int,
    locked: bool = False,
    attempts: int = PORT_TRIES,
) -> tuple[int | None, str]:
    """Pick a listen port. mode: reuse | bind | occupied | none_free."""
    preferred = int(preferred)
    if probe_own_gallery(preferred):
        return preferred, "reuse"
    if can_bind_port(host, preferred) and not port_accepting(preferred):
        return preferred, "bind"
    if locked:
        return None, "occupied"
    found = find_free_port(host, preferred + 1, max(1, int(attempts) - 1))
    if found is None:
        return None, "none_free"
    return found, "bind"


def _open_when_ready(url: str, port: int, timeout: float = 8.0) -> None:
    """Open the browser only after this app's /api/status answers on the port."""

    def _go() -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if probe_own_gallery(port):
                try:
                    webbrowser.open(url)
                except Exception:
                    pass
                return
            time.sleep(0.12)
        log(f"[服务] 等待 {url} 就绪超时，未自动打开浏览器")

    threading.Thread(target=_go, daemon=True).start()


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
    parser.add_argument("root", nargs="?", help="视频根目录，例如 D:\\Videos 或 ~/Movies")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="N",
        help="端口，默认 8765；被占用时自动换下一个。指定则固定该端口",
    )
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
    preferred = int(args.port) if args.port is not None else DEFAULT_PORT
    port_locked = args.port is not None
    chosen, mode = choose_listen_port(host, preferred, locked=port_locked)
    if mode == "occupied":
        alt = (
            f"VideoGallery.exe --port {preferred + 1}"
            if _is_frozen()
            else f"python app.py --port {preferred + 1}"
        )
        fail(
            f"端口 {preferred} 已被占用",
            f"该地址上不是本视频库。请关闭占用程序，或换端口启动:\n  {alt}",
        )
    if mode == "none_free":
        fail(
            f"端口 {preferred} 起连续 {PORT_TRIES} 个端口均被占用",
            "请关闭占用程序，或用 --port 指定一个空闲端口。",
        )
    if chosen is None:
        fail("无法选择监听端口", mode)
    args.port = int(chosen)
    STATE["bind_host"] = host
    STATE["bind_port"] = int(args.port)
    STATE["lan_share"] = bool(want_lan)
    if mode == "reuse":
        url = f"http://127.0.0.1:{args.port}"
        print(f"\n已有视频库在运行 → {url}")
        print("已打开现有页面，无需再开一份。关闭原来的启动窗口即可停止。\n")
        bootlog.step("reuse_instance", url)
        if not args.no_open:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        sys.exit(0)
    if args.port != preferred:
        print(f"提示: 端口 {preferred} 已被其他程序占用，已自动改用 {args.port}。")
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
        permission_hint = (
            "请在“系统设置 → 隐私与安全性”中授予终端/Python 文件访问权限，或换一个目录。"
            if sys.platform == "darwin"
            else "请用管理员运行，或换一个目录。"
        )
        fail("没有权限读取该盘/目录", f"路径: {root}\n{permission_hint}")
    except OSError as e:
        fail("无法读取该盘/目录", f"路径: {root}\n原因: {e}")

    bootlog.step("find_ffmpeg")
    STATE["ffmpeg"] = find_ffmpeg()
    bootlog.write(f"ffmpeg={STATE['ffmpeg']!r}")
    if not STATE["ffmpeg"]:
        print("提示: 未检测到 ffmpeg，将无法生成预览图（不影响播放）。")
        if sys.platform == "darwin":
            print("  安装方式: brew install ffmpeg")
        elif sys.platform == "win32":
            print("  安装方式: winget install ffmpeg  或从 https://ffmpeg.org 下载")
        else:
            print("  请使用系统包管理器安装 ffmpeg")
    else:
        print(f"ffmpeg: {STATE['ffmpeg']}")

    print(f"扫描目录: {root}")
    from vg.privacy import privacy_snapshot

    priv = privacy_snapshot()
    enc = "已加密" if priv["encrypt_thumbs"] else "明文 JPEG"
    print(f"预览图目录: {priv['cache_hint']}（{enc}）")
    print("正在后台加载/扫描，网页会先打开，列表随后刷新…")
    bootlog.step("start_scan", f"root={root} thumbs={not args.no_thumbs} force={args.rescan}")
    try:
        from vg.disk_libs import discover_indexed_roots, ensure_library
        from vg.roots import publish_unified_library, set_mounted_roots

        saved = prefs.get("mounted_roots") or []
        discovered = discover_indexed_roots()
        valid = []
        candidates = list(saved) if isinstance(saved, list) else []
        candidates.extend(discovered)
        for p in candidates:
            try:
                pp = Path(p).expanduser().resolve()
                if pp.is_dir() and str(pp).lower() not in {v.lower() for v in valid}:
                    valid.append(str(pp))
            except OSError:
                pass
        if discovered:
            bootlog.step(
                "restore_indexed_roots",
                f"saved={len(saved) if isinstance(saved, list) else 0} "
                f"discovered={len(discovered)} online={len(valid)}",
            )
        root_s = str(root.resolve())
        if root_s.lower() not in {v.lower() for v in valid}:
            valid = [root_s] + valid
        if len(valid) > 1:
            set_mounted_roots(valid, primary=root_s)
            for m in valid:
                ensure_library(m)
            # Publish all cached disks before the background scan starts. The
            # first page therefore already contains the full restored library.
            restored_count = publish_unified_library()
            bootlog.step(
                "restore_unified_library",
                f"roots={len(valid)} videos={restored_count}",
            )
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
                print("若手机/电视打不开，请按上方提示手动放行防火墙。")
        except Exception:
            pass
    else:
        print("局域网分享：关闭（仅本机）。网页点「局域网」可立即开启。")
    print("浏览器打开上述地址即可浏览。按 Ctrl+C 停止。\n")
    bootlog.step("ready", url)

    if not args.no_open:
        bootlog.step("open_browser", url)
        _open_when_ready(url, int(args.port))

    try:
        bootlog.step("run_server", f"{args.host}:{args.port}")
        while True:
            try:
                _run_server(args.host, int(args.port))
                bootlog.step("server_stopped")
                break
            except OSError as e:
                bootlog.exception("run_server")
                if not _is_addr_in_use(e):
                    fail("无法启动网页服务", str(e))
                if port_locked:
                    alt = (
                        f"VideoGallery.exe --port {int(args.port) + 1}"
                        if _is_frozen()
                        else f"python app.py --port {int(args.port) + 1}"
                    )
                    fail(
                        f"端口 {args.port} 已被占用",
                        f"请关闭占用该端口的程序，或换端口启动:\n  {alt}",
                    )
                nxt = find_free_port(args.host, int(args.port) + 1)
                if nxt is None:
                    fail(
                        f"端口 {args.port} 起连续端口均被占用",
                        "请关闭占用程序，或用 --port 指定一个空闲端口。",
                    )
                print(f"提示: 端口 {args.port} 启动时被抢占，已改用 {nxt}。")
                args.port = nxt
                STATE["bind_port"] = nxt
                url = f"http://127.0.0.1:{nxt}"
                print(f"本地视频库地址 → {url}")
                bootlog.step("port_retry", url)
                if not args.no_open:
                    _open_when_ready(url, nxt)
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

