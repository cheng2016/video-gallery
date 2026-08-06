# -*- coding: utf-8 -*-
"""LAN access helpers: firewall + loopback checks."""
from __future__ import annotations

import base64
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


def is_local_client(addr: str | None) -> bool:
    """True if the HTTP client is this machine (loopback or our own LAN IP)."""
    if is_loopback_addr(addr):
        return True
    if not addr:
        return False
    host = addr.split("%")[0]
    try:
        from vg.drives import list_lan_ipv4

        return host in set(list_lan_ipv4())
    except Exception:
        return False


def ensure_firewall_allow(port: int) -> tuple[bool, str]:
    """
    确保 Windows 入站规则存在。普通权限无法添加时弹出一次 UAC。

    规则覆盖公用网络，但把来源限制为本地子网，避免在不可信网络上
    向所有远端地址暴露服务。仅在用户主动开启局域网分享时调用。
    """
    if sys.platform == "darwin":
        return True, (
            "macOS 若弹出网络连接提示，请选择“允许”。若其它设备仍无法连接，"
            "请前往“系统设置 → 网络 → 防火墙 → 选项”，允许 Python 接收传入连接。"
        )
    if sys.platform != "win32":
        return True, ""
    port = int(port)
    name = f"VideoGallery LAN TCP {port}"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    rule_args = [
        "advfirewall", "firewall", "add", "rule",
        f"name={name}",
        "dir=in",
        "action=allow",
        "protocol=TCP",
        f"localport={port}",
        "profile=private,domain,public",
        "remoteip=LocalSubnet",
    ]
    try:
        # 已创建过就不再申请管理员权限，避免每次启动都弹 UAC。
        existing = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=flags,
        )
        if existing.returncode == 0:
            return True, f"防火墙已放行端口 {port}（仅限本地子网）"

        direct = subprocess.run(
            ["netsh", *rule_args],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=flags,
        )
        if direct.returncode == 0:
            return True, f"已放行端口 {port}（含公用网络，仅限本地子网）"

        # 以管理员身份执行短 PowerShell 脚本；应用自身仍保持普通权限运行。
        # 两层 EncodedCommand 避免规则名中的空格被 Start-Process 错误拆分。
        netsh_args = ",".join("'" + arg.replace("'", "''") + "'" for arg in rule_args)
        admin_script = f"$a=@({netsh_args}); & netsh.exe @a; exit $LASTEXITCODE"
        admin_encoded = base64.b64encode(admin_script.encode("utf-16le")).decode("ascii")
        ps_script = (
            "$p=Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -PassThru "
            f"-ArgumentList @('-NoProfile','-NonInteractive','-EncodedCommand','{admin_encoded}');"
            "exit $p.ExitCode"
        )
        encoded = base64.b64encode(ps_script.encode("utf-16le")).decode("ascii")
        elevated = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive",
                "-EncodedCommand", encoded,
            ],
            capture_output=True,
            text=True,
            timeout=90,
            creationflags=flags,
        )
        if elevated.returncode == 0:
            return True, f"已通过管理员确认放行端口 {port}（仅限本地子网）"

        err = (elevated.stderr or elevated.stdout or direct.stderr or direct.stdout or "").strip()
        return False, (
            f"未能自动放行防火墙（可能取消了管理员确认）。\n"
            f"请手动：Windows 安全中心 → 防火墙 → 允许应用，或管理员运行：\n"
            f'  netsh advfirewall firewall add rule name="{name}" dir=in action=allow '
            f"protocol=TCP localport={port} profile=private,domain,public remoteip=LocalSubnet\n"
            f"{err[:200]}"
        )
    except Exception as e:
        return False, f"防火墙配置失败: {e}"
