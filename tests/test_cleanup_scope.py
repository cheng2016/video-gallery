# -*- coding: utf-8 -*-
"""Cleanup duplicate scan must honor lib/category scope."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vg import web
from vg.config import MIN_VIDEO_FILE_BYTES
from vg.disk_libs import save_root_library
from vg.roots import publish_unified_library, set_mounted_roots
from vg.state import STATE


class CleanupScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "disk"
        self.cache_dir = base / "cache"
        self.root.mkdir()
        self.cache_dir.mkdir()
        (self.root / "电影").mkdir()
        (self.root / "电视剧").mkdir()
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
        set_mounted_roots([str(self.root.resolve())], primary=str(self.root.resolve()))

        big = max(MIN_VIDEO_FILE_BYTES, 200 * 1024)
        items = [
            {
                "id": "m1",
                "name": "同名片",
                "filename": "同名片.mp4",
                "rel": "电影/同名片.mp4",
                "folder": "电影",
                "ext": ".mp4",
                "size": big,
                "size_h": "200KB",
                "mtime": 1,
                "mtime_h": "",
            },
            {
                "id": "m2",
                "name": "同名片",
                "filename": "同名片_copy.mp4",
                "rel": "电影/同名片_copy.mp4",
                "folder": "电影",
                "ext": ".mp4",
                "size": big + 1,
                "size_h": "201KB",
                "mtime": 2,
                "mtime_h": "",
            },
            {
                "id": "t1",
                "name": "同名片",
                "filename": "同名片.mp4",
                "rel": "电视剧/同名片.mp4",
                "folder": "电视剧",
                "ext": ".mp4",
                "size": big + 2,
                "size_h": "202KB",
                "mtime": 3,
                "mtime_h": "",
            },
        ]
        save_root_library(self.root, items)
        publish_unified_library()
        self.client = web.app.test_client()

    def tearDown(self) -> None:
        for key, value in self._old_state.items():
            STATE[key] = value
        self.tmp.cleanup()

    def test_all_channels_sees_cross_folder_name_dup(self) -> None:
        data = self.client.get("/api/cleanup?type=dup").get_json()
        self.assertTrue(data["ok"])
        name_groups = [g for g in data["groups"] if g["reason"] == "同名"]
        self.assertEqual(len(name_groups), 1)
        self.assertEqual(len(name_groups[0]["items"]), 3)
        self.assertEqual(data["scope"]["category"], "")

    def test_category_limits_dup_to_channel(self) -> None:
        data = self.client.get("/api/cleanup?type=dup&category=电影").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["scope"]["category"], "电影")
        self.assertEqual(data["scope"]["video_count"], 2)
        name_groups = [g for g in data["groups"] if g["reason"] == "同名"]
        self.assertEqual(len(name_groups), 1)
        ids = {it["id"] for it in name_groups[0]["items"]}
        self.assertEqual(ids, {"m1", "m2"})
        # 电视剧里的同名不应进入本组
        self.assertTrue(all(it["folder"].startswith("电影") for it in name_groups[0]["items"]))

    def test_tv_channel_alone_has_no_dup_pair(self) -> None:
        data = self.client.get("/api/cleanup?type=dup&category=电视剧").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["scope"]["video_count"], 1)
        self.assertEqual(data["groups"], [])


if __name__ == "__main__":
    unittest.main()
