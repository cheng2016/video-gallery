# -*- coding: utf-8 -*-
"""Cleanup orchestration is testable without Flask."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vg.cleanup import build_cleanup_response, cleanup_categories_for_lib
from vg.config import MIN_VIDEO_FILE_BYTES
from vg.disk_libs import save_root_library
from vg.roots import publish_unified_library, set_mounted_roots
from vg.state import STATE


class CleanupServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "disk"
        self.cache_dir = base / "cache"
        self.root.mkdir()
        self.cache_dir.mkdir()
        self._old_state = {
            key: STATE.get(key)
            for key in (
                "root",
                "cache_dir",
                "videos",
                "by_id",
                "by_thumb_id",
                "by_category",
                "facets",
                "tree",
                "disk_libs",
                "mounted_roots",
            )
        }
        self._patchers = [
            mock.patch("vg.roots.save_prefs"),
            mock.patch("vg.roots.ensure_cache_dir", return_value=self.cache_dir),
            mock.patch("vg.disk_libs.ensure_cache_dir", return_value=self.cache_dir),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        STATE["videos"] = []
        STATE["disk_libs"] = {}
        STATE["mounted_roots"] = []
        STATE["root"] = self.root
        STATE["cache_dir"] = self.cache_dir
        set_mounted_roots([str(self.root.resolve())], primary=str(self.root))
        size = MIN_VIDEO_FILE_BYTES + 1
        save_root_library(self.root, [
            self.item("a", "电影/same.mp4", size, bad=True),
            self.item("b", "电影/copy.mp4", size),
            self.item("c", "电视剧/only.mp4", size + 1),
        ])
        publish_unified_library()

    def tearDown(self) -> None:
        for key, value in self._old_state.items():
            STATE[key] = value
        self.tmp.cleanup()

    @staticmethod
    def item(vid: str, rel: str, size: int, *, bad: bool = False) -> dict:
        path = Path(rel)
        item = {
            "id": vid,
            "name": path.stem,
            "filename": path.name,
            "rel": rel,
            "folder": str(path.parent).replace("\\", "/"),
            "ext": ".mp4",
            "size": size,
            "mtime": 1,
        }
        if bad:
            item["bad"] = True
            item["bad_reason"] = "probe failed"
        return item

    def test_duplicate_response_honors_channel_scope(self) -> None:
        response = build_cleanup_response("dup", category="电影")
        self.assertEqual(response["scope"]["video_count"], 2)
        self.assertEqual(len(response["groups"]), 1)
        self.assertEqual(response["groups"][0]["reason"], "同体积")

    def test_bad_response_is_independent_from_duplicate_detection(self) -> None:
        response = build_cleanup_response("bad", category="电影")
        self.assertEqual(response["type"], "bad")
        self.assertEqual(response["count"], 1)
        self.assertEqual(response["groups"][0]["items"][0]["reason"], "probe failed")

    def test_categories_use_cleanup_root_id(self) -> None:
        categories = cleanup_categories_for_lib("")
        self.assertEqual([row["id"] for row in categories], ["电影", "电视剧"])


if __name__ == "__main__":
    unittest.main()
