#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
静态图库本地小助手：服务本目录，并提供系统播放器 / 打开位置。
由「打开图库.bat」启动；启动后自动打开浏览器。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CFG = HERE / "bridge.json"
PATHS_FILE = HERE / "paths.json"
PORT = 8767
PATHS: dict[str, str] = {}
ALLOWED_ROOT = ROOT.parent.resolve()


def load_cfg() -> dict:
    try:
        data = json.loads(CFG.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def boot() -> None:
    global PORT, PATHS, ALLOWED_ROOT
    cfg = load_cfg()
    PORT = int(os.environ.get("VG_STATIC_PORT") or cfg.get("port") or 8767)
    root = Path(cfg.get("root") or "")
    if root.is_dir():
        ALLOWED_ROOT = root.resolve()
    else:
        ALLOWED_ROOT = ROOT.parent.resolve()
    PATHS = {}
    try:
        raw = json.loads(PATHS_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            PATHS = {str(k): str(v) for k, v in raw.items() if v}
    except Exception:
        pass


def resolve_target(vid: str | None, path_str: str | None) -> tuple[Path | None, str]:
    """优先用导出时的 id→绝对路径表，避免浏览器传来的路径对不上。"""
    vid = (vid or "").strip()
    path_str = (path_str or "").strip()
    if vid and vid in PATHS:
        p = Path(PATHS[vid])
        return p, str(p)
    if path_str:
        return Path(path_str), path_str
    return None, ""


def path_ok(path: Path) -> bool:
    try:
        if not path.exists():
            return False
        # 索引里的路径直接信任（导出时写入）
        for indexed in PATHS.values():
            try:
                if os.path.normcase(os.path.abspath(str(path))) == os.path.normcase(
                    os.path.abspath(indexed)
                ):
                    return True
            except OSError:
                continue
        rp = os.path.normcase(os.path.abspath(str(path.resolve())))
        root = os.path.normcase(os.path.abspath(str(ALLOWED_ROOT)))
        if rp == root:
            return True
        if not root.endswith(os.sep):
            root += os.sep
        return rp.startswith(root)
    except OSError:
        return False


def do_local_action(action: str, path_str: str | None = None, vid: str | None = None) -> tuple[int, dict]:
    action = (action or "path").strip().lower()
    path, path_str = resolve_target(vid, path_str)
    if not path_str or path is None:
        return 400, {"ok": False, "msg": "缺少路径（请重新导出静态站）"}

    if not path_ok(path):
        return 403, {
            "ok": False,
            "msg": f"文件不存在或不在视频盘内:\n{path_str}",
            "path": path_str,
        }

    if action == "path":
        return 200, {"ok": True, "path": str(path)}

    if action == "open":
        try:
            if sys.platform == "win32":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
            return 200, {"ok": True, "path": str(path), "msg": "已调用系统播放器"}
        except Exception as e:
            return 500, {"ok": False, "msg": f"打开失败: {e}", "path": str(path)}

    if action == "reveal":
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", str(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path.parent)])
            return 200, {"ok": True, "path": str(path), "msg": "已在文件夹中显示"}
        except Exception as e:
            return 500, {"ok": False, "msg": f"定位失败: {e}", "path": str(path)}

    return 400, {"ok": False, "msg": "未知操作"}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/")
        if route == "/api/ping":
            self._json(200, {
                "ok": True,
                "port": PORT,
                "root": str(ALLOWED_ROOT),
                "paths": len(PATHS),
            })
            return
        if route == "/api/local":
            qs = parse_qs(parsed.query)
            action = (qs.get("action") or ["path"])[0]
            path_str = unquote((qs.get("path") or [""])[0])
            vid = unquote((qs.get("id") or [""])[0])
            code, obj = do_local_action(action, path_str, vid)
            self._json(code, obj)
            return
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") != "/api/local":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            data = {}
        code, obj = do_local_action(
            data.get("action") or "path",
            data.get("path") or "",
            data.get("id") or "",
        )
        self._json(code, obj)

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        sys.stdout.write("[静态站] " + (fmt % args) + "\n")
        sys.stdout.flush()


def main() -> None:
    boot()
    os.chdir(ROOT)
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        print(f"端口 {PORT} 无法使用: {e}")
        print("请关闭占用该端口的程序，或修改 _cache\\bridge.json 的 port")
        input("按回车退出…")
        sys.exit(1)

    url = f"http://127.0.0.1:{PORT}/"
    print("=" * 50)
    print("  静态视频库已启动")
    print(f"  {url}")
    print(f"  视频根目录: {ALLOWED_ROOT}")
    print(f"  可打开文件: {len(PATHS)} 个")
    print("  关闭本窗口即停止")
    print("=" * 50)
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
