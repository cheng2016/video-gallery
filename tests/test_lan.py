# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import vg.lan as lan


def _result(code: int, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


class LanFirewallTests(unittest.TestCase):
    def test_existing_firewall_rule_does_not_request_elevation(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return _result(0)

        with patch.object(lan.sys, "platform", "win32"), \
             patch.object(lan.subprocess, "run", side_effect=fake_run):
            ok, message = lan.ensure_firewall_allow(8765)

        self.assertTrue(ok)
        self.assertTrue("已放行" in message or "防火墙已放行" in message)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-1], "name=VideoGallery LAN TCP 8765")

    def test_missing_rule_requests_uac_and_limits_access_to_local_subnet(self):
        calls = []
        results = iter([_result(1), _result(1, stderr="requires elevation"), _result(0)])

        def fake_run(args, **kwargs):
            calls.append(args)
            return next(results)

        with patch.object(lan.sys, "platform", "win32"), \
             patch.object(lan.subprocess, "run", side_effect=fake_run):
            ok, message = lan.ensure_firewall_allow(8765)

        self.assertTrue(ok)
        self.assertIn("管理员确认", message)
        self.assertIn("profile=private,domain,public", calls[1])
        self.assertIn("remoteip=LocalSubnet", calls[1])
        self.assertEqual(calls[2][0], "powershell.exe")

        outer_script = base64.b64decode(calls[2][-1]).decode("utf-16le")
        self.assertIn("-Verb RunAs", outer_script)
        self.assertIn("EncodedCommand", outer_script)

    def test_is_local_client_accepts_loopback_and_own_lan_ip(self):
        self.assertTrue(lan.is_local_client("127.0.0.1"))
        self.assertTrue(lan.is_local_client("::1"))
        with patch("vg.drives.list_lan_ipv4", return_value=["192.168.1.103"]):
            self.assertTrue(lan.is_local_client("192.168.1.103"))
            self.assertFalse(lan.is_local_client("192.168.1.101"))


class RemoteLocalOpenTests(unittest.TestCase):
    def test_remote_local_open_returns_play_url_instead_of_host_startfile(self):
        from vg import web
        from vg.state import STATE

        item = {
            "id": "abcd1234abcd1234",
            "name": "demo",
            "filename": "demo.mp4",
            "rel": "demo.mp4",
            "kind": "file",
            "ext": "mp4",
        }
        STATE["lan_share"] = True
        with patch("vg.web.find_video_by_id", return_value=item), \
             patch("vg.web._local_path_for_item", return_value=Path("D:/demo.mp4")), \
             patch("vg.lan.is_local_client", return_value=False), \
             patch("os.startfile") as startfile:
            client = web.app.test_client()
            resp = client.post(
                "/api/local/abcd1234abcd1234",
                json={"action": "open"},
                environ_base={"REMOTE_ADDR": "192.168.1.101"},
            )
        data = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(data["remote"])
        self.assertTrue(data["url"].endswith("/stream/abcd1234abcd1234"))
        startfile.assert_not_called()

if __name__ == "__main__":
    unittest.main()
