# -*- coding: utf-8 -*-
"""Scan-time live catalog must surface a new disk without wiping others."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vg.disk_libs import store_live_library, sync_disk_lib_memory
from vg.roots import _videos_from_root, set_mounted_roots, videos_for_scope
from vg.state import STATE


class ScanLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root_a = Path(self._tmp.name) / "A"
        self.root_b = Path(self._tmp.name) / "B"
        self.root_a.mkdir()
        self.root_b.mkdir()
        STATE["videos"] = []
        STATE["disk_libs"] = {}
        STATE["mounted_roots"] = []
        STATE["scan_root"] = ""
        STATE["scan_live"] = None
        STATE["lib_gen"] = 0
        STATE["root"] = self.root_a
        STATE["cache_dir"] = None
        set_mounted_roots([str(self.root_a.resolve()), str(self.root_b.resolve())], primary=str(self.root_a))

    def tearDown(self) -> None:
        STATE["scan_root"] = ""
        STATE["scan_live"] = None
        self._tmp.cleanup()

    def test_scan_live_visible_while_other_disk_stays(self) -> None:
        a_item = {
            "id": "a1",
            "name": "A1",
            "rel": "a1.mp4",
            "folder": "",
            "ext": ".mp4",
            "size": 1000,
            "_lib_root": str(self.root_a.resolve()),
            "root": str(self.root_a.resolve()),
        }
        store_live_library(self.root_a, [a_item])

        b_items = [
            {
                "id": f"b{i}",
                "name": f"B{i}",
                "rel": f"b{i}.mp4",
                "folder": "movies",
                "ext": ".mp4",
                "size": 2000 + i,
                "_lib_root": str(self.root_b.resolve()),
                "root": str(self.root_b.resolve()),
            }
            for i in range(3)
        ]
        STATE["scan_root"] = str(self.root_b.resolve())
        STATE["scan_live"] = b_items

        from_b = _videos_from_root(str(self.root_b.resolve()))
        self.assertEqual(len(from_b), 3)

        merged = videos_for_scope(None)
        ids = {v["id"] for v in merged}
        self.assertIn("a1", ids)
        self.assertTrue({"b0", "b1", "b2"}.issubset(ids))

        scoped_b = videos_for_scope(str(self.root_b.resolve()))
        self.assertEqual(len(scoped_b), 3)

    def test_sync_disk_lib_clears_live(self) -> None:
        items = [
            {
                "id": "x1",
                "name": "X",
                "rel": "x.mp4",
                "folder": "",
                "ext": ".mp4",
                "size": 10,
            }
        ]
        store_live_library(self.root_b, items)
        lib = (STATE["disk_libs"] or {}).get(str(self.root_b.resolve()))
        self.assertTrue(lib and lib.get("live"))
        with mock.patch("vg.disk_libs.ensure_cache_dir", return_value=self.root_b / ".cache"):
            (self.root_b / ".cache").mkdir(exist_ok=True)
            sync_disk_lib_memory(self.root_b, items)
        lib2 = (STATE["disk_libs"] or {}).get(str(self.root_b.resolve()))
        self.assertTrue(lib2 and not lib2.get("live"))


if __name__ == "__main__":
    unittest.main()
