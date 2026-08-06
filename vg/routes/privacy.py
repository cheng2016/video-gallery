# -*- coding: utf-8 -*-
"""Privacy settings HTTP endpoint."""
from __future__ import annotations

from pathlib import Path

from flask import jsonify, request

from vg.cache import ensure_cache_dir
from vg.privacy import privacy_snapshot, set_privacy
from vg.state import STATE


def register(app) -> None:
    @app.route("/api/privacy", methods=["GET", "POST"])
    def api_privacy():
        """隐私模式：预览图加密、缓存写在程序目录还是视频盘。"""
        if request.method == "GET":
            return jsonify({"ok": True, **privacy_snapshot()})
        data = request.get_json(silent=True) or {}
        encrypt = data.get("encrypt_thumbs")
        location = data.get("cache_location")
        if encrypt is None and location is None:
            return jsonify({"ok": False, "msg": "未提供设置项"}), 400
        if (
            location is not None
            and str(location).strip().lower() not in ("program", "disk")
        ):
            return jsonify({
                "ok": False,
                "msg": "cache_location 应为 program 或 disk",
            }), 400

        before = privacy_snapshot()
        after = set_privacy(
            encrypt_thumbs=bool(encrypt) if encrypt is not None else None,
            cache_location_value=str(location) if location is not None else None,
        )
        tips = []
        if encrypt is not None and bool(encrypt) != before["encrypt_thumbs"]:
            tips.append("加密设置已保存；新截的预览图按新规则写入，旧图仍可正常显示。")
        if (
            location is not None
            and after["cache_location"] != before["cache_location"]
        ):
            tips.append("缓存位置已改，请点「重新扫描」让索引与预览图落到新位置。")
            root = STATE.get("root")
            if root:
                try:
                    STATE["cache_dir"] = ensure_cache_dir(Path(root))
                except OSError:
                    pass
        return jsonify({
            "ok": True,
            "msg": " ".join(tips) if tips else "已保存",
            **after,
        })
