# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from vg.export import obscure_thumb_name, static_site_readme


ROOT = Path(__file__).resolve().parents[1]


def _load_bridge():
    path = ROOT / "templates" / "static_bridge.py"
    spec = importlib.util.spec_from_file_location("vg_static_bridge", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load static_bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class StaticExportPortableTests(unittest.TestCase):
    def test_thumbs_keep_opaque_vgj_extension(self) -> None:
        name = obscure_thumb_name("abc", "salt")
        self.assertTrue(name.endswith(".vgj"))
        self.assertFalse(name.endswith(".jpg"))

    def test_readme_covers_computers_and_phones(self) -> None:
        text = static_site_readme(Path("E:/Movies"), "2026-08-13T20:00:00")
        self.assertIn("index.html", text)
        self.assertIn("Android", text)
        self.assertIn("iPhone", text)
        self.assertIn("同一 WiFi", text)

    def test_static_site_keeps_mobile_channel_drawer(self) -> None:
        html = (ROOT / "templates" / "static_site.html").read_text(encoding="utf-8")
        self.assertIn('id="navToggle"', html)
        self.assertIn('id="navScrim"', html)
        self.assertIn("webkit-playsinline", html)
        self.assertNotIn("aside { display: none; }", html)
        self.assertIn("viewport-fit=cover", html)

    def test_bridge_treats_loopback_and_jpeg_covers(self) -> None:
        bridge = _load_bridge()
        self.assertTrue(bridge._is_loopback("127.0.0.1"))
        self.assertTrue(bridge._is_loopback("::1"))
        self.assertFalse(bridge._is_loopback("192.168.1.8"))
        handler = object.__new__(bridge.Handler)
        self.assertEqual(bridge.Handler.guess_type(handler, "cover.jpg"), "image/jpeg")
        self.assertEqual(bridge.Handler.guess_type(handler, "cover.vgj"), "image/jpeg")

    def test_bridge_binds_all_interfaces(self) -> None:
        text = (ROOT / "templates" / "static_bridge.py").read_text(encoding="utf-8")
        self.assertIn('ThreadingHTTPServer(("0.0.0.0", port), Handler)', text)
        self.assertIn("lan_browse_urls", text)
        self.assertIn("仅本机可调用系统播放器", text)


if __name__ == "__main__":
    unittest.main()
