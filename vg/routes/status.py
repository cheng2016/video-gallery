# -*- coding: utf-8 -*-
"""Lightweight runtime status endpoint."""
from __future__ import annotations

from flask import jsonify

from vg.state import STATE


def register(app) -> None:
    @app.route("/api/status")
    def api_status():
        facets = STATE.get("facets") or {}
        live = STATE.get("scan_live")
        live_count = len(live) if isinstance(live, list) else 0
        return jsonify({
            "scanning": bool(STATE.get("scanning")),
            "updating": bool(STATE.get("updating")),
            "exporting": bool(STATE.get("exporting")),
            "export_msg": STATE.get("export_msg") or "",
            "export_path": STATE.get("export_path") or "",
            "export_ok": STATE.get("export_ok"),
            "scan_progress": STATE.get("scan_progress") or "",
            "thumb_progress": STATE.get("thumb_progress") or "",
            "meta_progress": STATE.get("meta_progress") or "",
            "count": int(
                facets.get("count")
                or len(STATE.get("videos") or [])
            ),
            "lib_gen": int(STATE.get("lib_gen") or 0),
            "scan_root": STATE.get("scan_root") or "",
            "scan_found": live_count,
            "has_ffmpeg": bool(STATE.get("ffmpeg")),
            "root": str(STATE["root"]) if STATE.get("root") else "",
            "lan_share": bool(STATE.get("lan_share")),
        })
