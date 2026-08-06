# -*- coding: utf-8 -*-
"""LAN sharing state and URL presentation."""
from __future__ import annotations

from vg.drives import list_lan_ipv4
from vg.state import STATE


def lan_urls() -> list[str]:
    port = int(STATE.get("bind_port") or 8765)
    urls = [f"http://127.0.0.1:{port}"]
    if STATE.get("lan_share"):
        for ip in list_lan_ipv4():
            url = f"http://{ip}:{port}"
            if url not in urls:
                urls.append(url)
    return urls
