# -*- coding: utf-8 -*-
"""Mounted-root administration endpoint."""
from __future__ import annotations

from pathlib import Path

from flask import jsonify, request

from vg.roots import (
    add_mount,
    publish_unified_library,
    remove_mount,
    roots_summary,
    set_mounted_roots,
)
from vg.state import STATE


def register(app) -> None:
    @app.route("/api/roots", methods=["GET", "POST"])
    def api_roots():
        """GET roots; POST add/remove/set/publish."""
        if request.method == "GET":
            mounts = roots_summary()
            primary = ""
            try:
                if STATE.get("root"):
                    primary = str(Path(STATE["root"]).resolve())
            except OSError:
                primary = str(STATE.get("root") or "")
            for mount in mounts:
                mount["current"] = (
                    bool(primary)
                    and mount.get("path", "").lower() == primary.lower()
                )
            return jsonify({
                "ok": True,
                "roots": mounts,
                "count": len(mounts),
                "multi": len(mounts) > 1,
                "primary": primary,
            })

        data = request.get_json(silent=True) or {}
        action = (data.get("action") or "add").strip().lower()
        if action == "publish":
            count = publish_unified_library()
            return jsonify({
                "ok": True,
                "msg": f"已刷新统一片库（{count}）",
                "count": count,
            })
        if action == "set":
            paths = data.get("paths") or []
            if not isinstance(paths, list):
                return jsonify({"ok": False, "msg": "paths 无效"}), 400
            cleaned = set_mounted_roots([str(path) for path in paths])
            count = publish_unified_library() if cleaned else 0
            return jsonify({
                "ok": True,
                "msg": f"已设置 {len(cleaned)} 个目录",
                "roots": cleaned,
                "count": count,
            })

        path = (data.get("path") or data.get("drive") or "").strip().strip('"')
        if not path:
            return jsonify({"ok": False, "msg": "请提供 path"}), 400
        if len(path) == 2 and path[1] == ":":
            path += "\\"
        if action == "remove":
            ok, msg = remove_mount(path)
            return jsonify({"ok": ok, "msg": msg, "roots": roots_summary()})
        ok, msg = add_mount(path)
        return jsonify({"ok": ok, "msg": msg, "roots": roots_summary()})
