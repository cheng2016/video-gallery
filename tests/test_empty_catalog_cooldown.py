# -*- coding: utf-8 -*-
"""Empty catalog.sqlite must not be reopened on every ensure_library call."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vg import cache, catalog_db, config, disk_libs
from vg.cache import ensure_cache_dir
from vg.catalog_db import save_catalog
from vg.disk_libs import ensure_library, load_library_from_index, read_root_library
from vg.privacy import set_privacy
from vg.state import STATE


class EmptyCatalogCooldownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "disk"
        self.root.mkdir()

        self.old_vgdata = cache.VGDATA_DIR
        self.old_key = cache.KEY_FILE
        self.old_disk_vgdata = disk_libs.VGDATA_DIR
        self.old_config_vg = config.VGDATA_DIR
        self.old_catalog_vg = catalog_db.VGDATA_DIR
        cache.VGDATA_DIR = self.base / "cache"
        cache.KEY_FILE = cache.VGDATA_DIR / "vault.key"
        disk_libs.VGDATA_DIR = cache.VGDATA_DIR
        config.VGDATA_DIR = cache.VGDATA_DIR
        catalog_db.VGDATA_DIR = cache.VGDATA_DIR
        cache.VGDATA_DIR.mkdir(parents=True, exist_ok=True)
        set_privacy(cache_location_value="program")

        self.old_disk_libs = STATE.get("disk_libs")
        self.old_root = STATE.get("root")
        STATE["disk_libs"] = {}
        STATE["root"] = self.root
        disk_libs._catalog_empty_until.clear()
        disk_libs._catalog_missing_until.clear()

        self.cache = ensure_cache_dir(self.root)
        # Persist an empty catalog (file exists, 0 video rows).
        save_catalog(self.cache, self.root, [])

    def tearDown(self) -> None:
        STATE["disk_libs"] = self.old_disk_libs if self.old_disk_libs is not None else {}
        STATE["root"] = self.old_root
        cache.VGDATA_DIR = self.old_vgdata
        cache.KEY_FILE = self.old_key
        disk_libs.VGDATA_DIR = self.old_disk_vgdata
        config.VGDATA_DIR = self.old_config_vg
        catalog_db.VGDATA_DIR = self.old_catalog_vg
        disk_libs._catalog_empty_until.clear()
        self.tmp.cleanup()

    def test_empty_catalog_load_is_coooled_down(self) -> None:
        with patch("vg.catalog_db.load_catalog_videos", wraps=catalog_db.load_catalog_videos) as mocked:
            self.assertFalse(load_library_from_index(self.root))
            self.assertFalse(ensure_library(self.root))
            self.assertFalse(ensure_library(self.root))
            self.assertIsNone(read_root_library(self.root))
            # First miss opens SQLite once; cooldown skips the rest.
            self.assertEqual(mocked.call_count, 1)

    def test_writing_rows_invalidates_empty_cooldown_via_mtime(self) -> None:
        self.assertFalse(load_library_from_index(self.root))
        item = {
            "id": "abcdef0123456789",
            "rel": "a.mp4",
            "name": "a",
            "ext": ".mp4",
            "size": 1000,
            "mtime": 1.0,
            "folder": "",
            "kind": "file",
        }
        # New write changes catalog mtime → cooldown must not block reload.
        save_catalog(self.cache, self.root, [item])
        with patch("vg.catalog_db.load_catalog_videos", wraps=catalog_db.load_catalog_videos) as mocked:
            self.assertTrue(load_library_from_index(self.root))
            self.assertEqual(mocked.call_count, 1)
            self.assertTrue(ensure_library(self.root))
            self.assertEqual(mocked.call_count, 1)


if __name__ == "__main__":
    unittest.main()
