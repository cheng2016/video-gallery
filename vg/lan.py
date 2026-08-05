# -*- coding: utf-8 -*-
"""LAN access helpers: firewall + loopback checks."""
from __future__ import annotations

import ipaddress
import subprocess
import sys


def is_loopback_addr(addr: str | None) -> bool:
    if not addr:
        return False
    try:
        return ipaddress.ip_address(addr.split("%")[0]).is_loopback
    except ValueError:
        return addr in ("127.0.0.1", "::1", "localhost")


def ensure_firewall_allow(port: int) -> tuple[bool, str]:
    """
    尝试添加 Windows 入站规则（专用/域网络）。失败不抛错，返回提示文案。
    """
    if sys.platform != "win32":
        return True, ""
    port = int(port)
    name = f"VideoGallery TCP {port}"
    try:
        subprocess.run(
            ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        r = subprocess.run(
            [
                "netsh", "advfirewall", "firewall", "add", "rule",
                f"name={name}",
                "dir=in",
                "action=allow",
                "protocol=TCP",
                f"localport={str(port)}",
                "profile=private,domain",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if r.returncode == 0:
            return True, f"已添加防火墙放行规则（端口 {port}）"
        err = (r.stderr or r.stdout or "").strip()
        return False, (
            f"未能自动放行防火墙（可能需要管理员权限）。\n"
            f"请手动：Windows 安全中心 → 防火墙 → 允许应用，或管理员运行：\n"
            f'  netsh advfirewall firewall add rule name="{name}" dir=in action=allow protocol=TCP localport={port}\n'
            f"{err[:200]}"
        )
    except Exception as e:
        return False, f"防火墙配置失败: {e}"
