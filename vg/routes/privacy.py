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
        """设置：隐私、缓存位置和可选的视频元数据探测。"""
        if request.method == "GET":
            return jsonify({"ok": True, **privacy_snapshot()})
        data = request.get_json(silent=True) or {}
        encrypt = data.get("encrypt_thumbs")
        location = data.get("cache_location")
        probe_duration = data.get("probe_video_duration")
        probe_audio = data.get("probe_video_audio")
        if (
            encrypt is None
            and location is None
            and probe_duration is None
            and probe_audio is None
        ):
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
            probe_video_duration=(
                bool(probe_duration) if probe_duration is not None else None
            ),
            probe_video_audio=bool(probe_audio) if probe_audio is not None else None,
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
        if (
            probe_duration is not None
            and after["probe_video_duration"] != before["probe_video_duration"]
        ):
            tips.append(
                "视频时长探测已开启；将在本次或下次扫描时补全。"
                if after["probe_video_duration"]
                else "视频时长探测已关闭。"
            )
        if (
            probe_audio is not None
            and after["probe_video_audio"] != before["probe_video_audio"]
        ):
            tips.append(
                "视频音频探测已开启；将在本次或下次扫描时补全。"
                if after["probe_video_audio"]
                else "视频音频探测已关闭。"
            )
        return jsonify({
            "ok": True,
            "msg": " ".join(tips) if tips else "已保存",
            **after,
        })
