# -*- coding: utf-8 -*-
"""Empty mount must not reuse a unified snapshot that has no rows for that root."""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from vg import cache, catalog_db, config, disk_libs
from vg.cache import ensure_cache_dir
from vg.catalog import rebuild_indexes
from vg.privacy import set_privacy
from vg.scan import start_scan
from vg.state import STATE


class EmptyRootScanReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root_c = self.base / "C_disk"
        self.root_e = self.base / "E_disk"
        self.root_c.mkdir()
        self.root_e.mkdir()
        (self.root_e / "movie.mp4").write_bytes(b"x" * 200_000)

        self.old = {
            "vgdata": cache.VGDATA_DIR,
            "key": cache.KEY_FILE,
            "disk_vg": disk_libs.VGDATA_DIR,
            "cfg_vg": config.VGDATA_DIR,
            "cat_vg": catalog_db.VGDATA_DIR,
            "videos": STATE.get("videos"),
            "root": STATE.get("root"),
            "scanning": STATE.get("scanning"),
            "disk_libs": STATE.get("disk_libs"),
        }
        cache.VGDATA_DIR = self.base / "cache"
        cache.KEY_FILE = cache.VGDATA_DIR / "vault.key"
        disk_libs.VGDATA_DIR = cache.VGDATA_DIR
        config.VGDATA_DIR = cache.VGDATA_DIR
        catalog_db.VGDATA_DIR = cache.VGDATA_DIR
        cache.VGDATA_DIR.mkdir(parents=True, exist_ok=True)
        set_privacy(cache_location_value="program")
        ensure_cache_dir(self.root_e)

        # Unified snapshot looks like startup restore: STATE.root points at E,
        # but every video belongs to C. Old bug treated this as "E covered".
        STATE["root"] = self.root_e
        STATE["scanning"] = False
        STATE["disk_libs"] = {}
        rebuild_indexes(
            [
                {
                    "id": "cccccccccccccccc",
                    "rel": "c.mp4",
                    "name": "c",
                    "ext": ".mp4",
                    "size": 200_000,
                    "mtime": 1.0,
                    "folder": "",
                    "root": str(self.root_c),
                    "_lib_root": str(self.root_c),
                }
            ]
        )

    def tearDown(self) -> None:
        STATE["videos"] = self.old["videos"] if self.old["videos"] is not None else []
        STATE["root"] = self.old["root"]
        STATE["scanning"] = self.old["scanning"]
        STATE["disk_libs"] = self.old["disk_libs"] if self.old["disk_libs"] is not None else {}
        cache.VGDATA_DIR = self.old["vgdata"]
        cache.KEY_FILE = self.old["key"]
        disk_libs.VGDATA_DIR = self.old["disk_vg"]
        config.VGDATA_DIR = self.old["cfg_vg"]
        catalog_db.VGDATA_DIR = self.old["cat_vg"]
        self.tmp.cleanup()

    def test_empty_root_does_not_reuse_foreign_snapshot(self) -> None:
        seen = {"reuse": False, "rejected": False, "load_or_scan": False}

        def _emit(level, event, **fields):
            if event == "scan_cache_reuse_auto":
                seen["reuse"] = True
            if event == "scan_cache_reuse_rejected":
                seen["rejected"] = True

        real_thread = threading.Thread

        def _immediate_thread(target=None, args=(), kwargs=None, daemon=None):
            kwargs = kwargs or {}

            class _T:
                def start(self_inner):
                    if target:
                        target(*args, **kwargs)

            return _T()

        with patch("vg.diagnostics.emit", side_effect=_emit), patch(
            "vg.scan.load_or_scan",
            side_effect=lambda *a, **k: seen.__setitem__("load_or_scan", True) or True,
        ), patch("vg.scan.threading.Thread", side_effect=_immediate_thread), patch(
            "vg.scan.release_scan_lock"
        ), patch(
            "vg.scan.try_acquire_scan_lock", return_value=True
        ), patch(
            "vg.scan._bg_count_then_maybe_scan"
        ), patch(
            "vg.scan.archive_current_library"
        ), patch(
            "vg.scan.save_prefs"
        ), patch(
            "vg.scan.ensure_cache_dir", return_value=self.base / "cache" / "E"
        ):
            ok, _msg = start_scan(self.root_e, do_thumbs=False, force=False)

        self.assertTrue(ok)
        self.assertFalse(seen["reuse"])
        self.assertTrue(seen["rejected"])
        self.assertTrue(seen["load_or_scan"])


if __name__ == "__main__":
    unittest.main()
