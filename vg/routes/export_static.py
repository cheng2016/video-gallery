# -*- coding: utf-8 -*-
"""Static export HTTP endpoints."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from flask import jsonify, request

from vg.config import STATIC_EXPORT_DIRNAME
from vg.export import export_static_site
from vg.state import STATE
from vg.util import log


def register(app) -> None:
    @app.route("/api/export-static", methods=["POST"])
    def api_export_static():
        """导出纯静态站到视频盘根目录（后台线程，不挡浏览）。"""
        if STATE.get("exporting"):
            return jsonify({
                "ok": False,
                "msg": "正在导出中，请稍候…",
                "exporting": True,
            })
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
                    except Exception as error:
                        log(f"[静态导出] 打开目录失败: {error}")
            except Exception as error:
                STATE["export_ok"] = False
                STATE["export_msg"] = f"导出失败: {error}"
                log(f"[静态导出] 异常: {error}")
            finally:
                STATE["exporting"] = False

        threading.Thread(
            target=job,
            daemon=True,
            name="export-static",
        ).start()
        return jsonify({
            "ok": True,
            "msg": "已开始导出静态站，完成后会打开文件夹",
            "exporting": True,
        })

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
        except Exception as error:
            return jsonify({"ok": False, "msg": str(error), "path": path}), 500
