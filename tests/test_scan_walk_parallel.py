# -*- coding: utf-8 -*-
"""Parallel directory walk must find the same videos as a single-thread walk."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vg.scan import expand_scan_walk_jobs, scan_videos
from vg.state import STATE


class ScanWalkParallelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "lib"
        self.cache = Path(self.tmp.name) / "cache"
        self.root.mkdir()
        self.cache.mkdir()
        for name in ("alpha", "beta", "gamma"):
            folder = self.root / name
            folder.mkdir()
            (folder / f"{name}.mp4").write_bytes(b"x" * 200_000)
        self._old = {key: STATE.get(key) for key in ("cache_dir", "ffmpeg", "videos", "scanning")}
        STATE.update({"cache_dir": self.cache, "ffmpeg": None, "videos": [], "scanning": False})

    def tearDown(self) -> None:
        STATE.update(self._old)
        self.tmp.cleanup()

    def _names(self) -> set[str]:
        return {str(v.get("name") or "") for v in (STATE.get("videos") or [])}

    def test_parallel_walk_finds_all_top_level_videos(self) -> None:
        with mock.patch("vg.scan.os.cpu_count", return_value=8):
            scan_videos(self.root, do_thumbs=False, incremental=False, quiet=True)
        self.assertEqual(self._names(), {"alpha", "beta", "gamma"})

    def test_serial_walk_finds_the_same_videos(self) -> None:
        with mock.patch("vg.scan.os.cpu_count", return_value=1):
            scan_videos(self.root, do_thumbs=False, incremental=False, quiet=True)
        self.assertEqual(self._names(), {"alpha", "beta", "gamma"})

    def test_expand_splits_users_shaped_tree(self) -> None:
        users = self.root / "Users"
        other = self.root / "Drivers"
        users.mkdir()
        other.mkdir()
        for name in ("alice", "bob", "public"):
            folder = users / name / "Videos"
            folder.mkdir(parents=True)
            (folder / f"{name}.mp4").write_bytes(b"x" * 200_000)
        (other / "clip.mp4").write_bytes(b"x" * 200_000)
        shallow, leaves = expand_scan_walk_jobs(
            self.root,
            ["Drivers", "Users"],
            target_jobs=8,
        )
        labels = {name for name, _path in leaves}
        self.assertIn("Users", {name for name, _path in shallow})
        self.assertTrue(any(name.startswith("Users/") for name in labels))
        self.assertIn("Drivers", labels | {name for name, _path in shallow})

    def test_expanded_walk_finds_videos_under_users(self) -> None:
        users = self.root / "Users"
        drivers = self.root / "Drivers"
        users.mkdir()
        drivers.mkdir()
        for name in ("alice", "bob"):
            folder = users / name / "Documents"
            folder.mkdir(parents=True)
            (folder / f"{name}.mp4").write_bytes(b"x" * 200_000)
        (drivers / "show.mp4").write_bytes(b"x" * 200_000)
        with mock.patch("vg.scan.os.cpu_count", return_value=8):
            scan_videos(self.root, do_thumbs=False, incremental=False, quiet=True)
        self.assertEqual(self._names(), {"alpha", "beta", "gamma", "alice", "bob", "show"})
