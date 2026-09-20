# -*- coding: utf-8 -*-
"""Concurrent catalog readers must share one SQLite load."""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vg.cache import ensure_cache_dir, save_index
from vg import disk_libs
from vg.disk_libs import read_root_library
from vg.state import STATE


class CatalogSingleFlightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "lib"
        self.root.mkdir()
        self.cache = ensure_cache_dir(self.root)
        item = {
            "id": "aaaaaaaaaaaaaaaa",
            "name": "clip",
            "filename": "clip.mp4",
            "rel": "clip.mp4",
            "folder": "",
            "ext": ".mp4",
            "size": 10,
        }
        save_index(self.cache, self.root, [item], file_count=1, folder_counts={"": 1})
        self._old_libs = STATE.get("disk_libs")
        STATE["disk_libs"] = {}
        disk_libs._catalog_flights.clear()

    def tearDown(self) -> None:
        STATE["disk_libs"] = self._old_libs if self._old_libs is not None else {}
        disk_libs._catalog_flights.clear()
        self.tmp.cleanup()

    def test_parallel_reads_open_sqlite_once(self) -> None:
        calls = {"n": 0}
        from vg.catalog_db import load_catalog_videos as real_load

        def slow_load(*args, **kwargs):
            calls["n"] += 1
            time.sleep(0.15)
            return real_load(*args, **kwargs)

        results: list = []

        def worker():
            results.append(read_root_library(self.root))

        with patch("vg.catalog_db.load_catalog_videos", side_effect=slow_load):
            threads = [threading.Thread(target=worker) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(len(results), 6)
        for rows in results:
            self.assertIsNotNone(rows)
            self.assertEqual(len(rows or []), 1)


if __name__ == "__main__":
    unittest.main()
