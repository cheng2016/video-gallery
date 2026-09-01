# -*- coding: utf-8 -*-
"""LAN share settings endpoint."""
from __future__ import annotations

from flask import jsonify, request

from vg.diagnostics import emit
from vg.drives import load_prefs, save_prefs
from vg.lan import ensure_firewall_allow
from vg.lan_service import lan_urls
from vg.state import STATE


def register(app) -> None:
    @app.route("/api/share", methods=["GET", "POST"])
    def api_share():
        """局域网分享开关（服务常驻绑定，由访问控制即时开关）。"""
        port = int(STATE.get("bind_port") or 8765)
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            enabled = bool(data.get("lan"))
            before = bool(STATE.get("lan_share"))
            STATE["lan_share"] = enabled
            save_prefs(lan_share=enabled)
            firewall_ok, firewall_msg = True, ""
            if enabled:
                firewall_ok, firewall_msg = ensure_firewall_allow(port)
            urls = lan_urls()
            lan_only = [url for url in urls if "127.0.0.1" not in url]
            emit(
                "INFO",
                "lan_share_api",
                force=True,
                method="POST",
                lan_before=before,
                lan_after=enabled,
                url_count=len(urls),
                lan_url_count=len(lan_only),
                firewall_ok=firewall_ok,
                port=port,
            )
            if enabled:
                msg = "已开启局域网分享（立即生效）"
                if lan_only:
                    msg += "。其它设备请打开：\n" + "\n".join(lan_only)
                else:
                    msg += "。未检测到局域网 IP，请确认电脑已连 WiFi。"
                if firewall_msg:
                    msg += "\n\n" + firewall_msg
                if not firewall_ok:
                    msg += "\n若仍「拒绝连接」，多半是防火墙拦截。"
            else:
                msg = "已关闭局域网分享（立即生效），仅本机可访问"
            return jsonify({
                "ok": True,
                "lan": enabled,
                "active": enabled,
                "need_restart": False,
                "firewall_ok": firewall_ok,
                "urls": urls,
                "msg": msg,
            })

        prefs = load_prefs()
        lan = bool(STATE.get("lan_share"))
        pref_lan = bool(prefs.get("lan_share"))
        urls = lan_urls()
        emit(
            "INFO",
            "lan_share_api",
            force=True,
            method="GET",
            lan=lan,
            pref_lan=pref_lan,
            url_count=len(urls),
            lan_url_count=len([u for u in urls if "127.0.0.1" not in u]),
            port=port,
        )
        return jsonify({
            "ok": True,
            "lan": lan,
            "pref_lan": pref_lan,
            "need_restart": False,
            "urls": urls,
            "host": STATE.get("bind_host") or "0.0.0.0",
            "port": port,
        })
