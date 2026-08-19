# -*- coding: utf-8 -*-
"""Rescanning one disk must not wipe other mounted disks from the UI catalog."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from vg import cache, config, disk_libs, drives
from vg.cache import ensure_cache_dir, save_index
from vg.catalog import rebuild_indexes
from vg.disk_libs import _norm_root_str, archive_current_library, sync_disk_lib_memory
from vg.roots import (
    get_mounted_roots,
    publish_unified_library,
    roots_summary,
    set_mounted_roots,
    tree_for_scope,
    videos_for_scope,
)
from vg.scan import scan_videos
from vg.state import STATE
from vg.util import format_size, video_id


class MultiDiskRescanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root_a = self.base / "disk-a"
        self.root_b = self.base / "disk-b"
        self.root_a.mkdir()
        self.root_b.mkdir()
        (self.root_a / "a1.mp4").write_bytes(b"a" * 200_000)
        (self.root_b / "b1.mp4").write_bytes(b"b" * 200_000)

        self._old_cache = (
            cache.VGDATA_DIR,
            cache.KEY_FILE,
            disk_libs.VGDATA_DIR,
            config.VGDATA_DIR,
            config.PREFS_FILE,
            drives.PREFS_FILE,
            drives.VGDATA_DIR,
        )
        cache.VGDATA_DIR = self.base / "preview_cache"
        cache.KEY_FILE = cache.VGDATA_DIR / "vault.key"
        disk_libs.VGDATA_DIR = cache.VGDATA_DIR
        config.VGDATA_DIR = cache.VGDATA_DIR
        config.PREFS_FILE = cache.VGDATA_DIR / "prefs.json"
        drives.PREFS_FILE = config.PREFS_FILE
        drives.VGDATA_DIR = cache.VGDATA_DIR
        cache.VGDATA_DIR.mkdir(parents=True, exist_ok=True)

        self._old_state = {
            key: STATE.get(key)
            for key in (
                "root",
                "cache_dir",
                "videos",
                "by_id",
                "by_category",
                "facets",
                "tree",
                "disk_libs",
                "mounted_roots",
                "scanning",
                "updating",
                "scan_live",
                "scan_root",
                "ffmpeg",
                "lib_gen",
            )
        }
        STATE.update(
            {
                "root": self.root_a,
                "cache_dir": ensure_cache_dir(self.root_a),
                "videos": [],
                "by_id": {},
                "disk_libs": {},
                "mounted_roots": [],
                "scanning": False,
                "updating": False,
                "scan_live": None,
                "scan_root": "",
                "ffmpeg": None,
                "lib_gen": 0,
            }
        )
        set_mounted_roots(
            [str(self.root_a.resolve()), str(self.root_b.resolve())],
            primary=str(self.root_a.resolve()),
        )
        self._seed_indexes()
        publish_unified_library()

    def tearDown(self) -> None:
        for key, value in self._old_state.items():
            STATE[key] = value
        (
            cache.VGDATA_DIR,
            cache.KEY_FILE,
            disk_libs.VGDATA_DIR,
            config.VGDATA_DIR,
            config.PREFS_FILE,
            drives.PREFS_FILE,
            drives.VGDATA_DIR,
        ) = self._old_cache
        self.tmp.cleanup()

    def _item(self, root: Path, name: str) -> dict:
        path = root / name
        st = path.stat()
        return {
            "id": video_id(name),
            "name": path.stem,
            "filename": name,
            "rel": name,
            "folder": "",
            "ext": ".mp4",
            "size": st.st_size,
            "size_h": format_size(st.st_size),
            "mtime": st.st_mtime,
            "mtime_h": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "has_thumb": False,
            "genres": [],
        }

    def _seed_indexes(self) -> None:
        items_a = [self._item(self.root_a, "a1.mp4")]
        items_b = [self._item(self.root_b, "b1.mp4")]
        cache_a = ensure_cache_dir(self.root_a)
        cache_b = ensure_cache_dir(self.root_b)
        save_index(cache_a, self.root_a, items_a, file_count=1, folder_counts={"": 1})
        save_index(cache_b, self.root_b, items_b, file_count=1, folder_counts={"": 1})
        sync_disk_lib_memory(self.root_a, items_a)
        sync_disk_lib_memory(self.root_b, items_b)

    def _simulate_force_start(self, root: Path) -> None:
        """Mirror start_scan(force=True, replace_mounts=False) before walk."""
        root_s = str(root.resolve())
        archive_current_library()
        mounts = get_mounted_roots()
        keep = []
        for video in list(STATE.get("videos") or []):
            tagged = (video.get("_lib_root") or "").strip()
            if not tagged:
                continue
            if _norm_root_str(tagged).lower() == root_s.lower():
                continue
            keep.append(video)
        STATE["videos"] = keep
        if keep:
            rebuild_indexes(keep)
            try:
                STATE["tree"] = tree_for_scope(None)
            except Exception:
                pass
        else:
            STATE["tree"] = {
                "name": root.name or str(root),
                "path": "",
                "count": 0,
                "children": [],
                "videos": [],
            }
        STATE["scanning"] = True
        STATE["root"] = root
        STATE["cache_dir"] = ensure_cache_dir(root)

    def test_force_rescan_keeps_other_disk_visible_mid_and_after(self) -> None:
        before = {v["rel"] for v in videos_for_scope(None)}
        self.assertEqual(before, {"a1.mp4", "b1.mp4"})
        self.assertEqual(
            {r["path"]: r["count"] for r in roots_summary()},
            {
                str(self.root_a.resolve()): 1,
                str(self.root_b.resolve()): 1,
            },
        )

        self._simulate_force_start(self.root_a)
        mid_roots = {r["path"]: r["count"] for r in roots_summary()}
        mid_rels = {v["rel"] for v in videos_for_scope(None)}
        self.assertEqual(
            mid_roots[str(self.root_b.resolve())],
            1,
            "other disk must stay visible while one disk is force-rescanning",
        )
        self.assertIn("b1.mp4", mid_rels)

        scan_videos(
            self.root_a,
            do_thumbs=False,
            incremental=True,
            quiet=False,
            burst_thumbs=False,
        )

        after_roots = {r["path"]: r["count"] for r in roots_summary()}
        after_rels = {v["rel"] for v in videos_for_scope(None)}
        self.assertEqual(after_roots[str(self.root_a.resolve())], 1)
        self.assertEqual(after_roots[str(self.root_b.resolve())], 1)
        self.assertEqual(after_rels, {"a1.mp4", "b1.mp4"})
        self.assertGreaterEqual(len(STATE.get("videos") or []), 2)

    def test_activate_mount_does_not_drop_offline_sibling(self) -> None:
        from vg.roots import activate_mount, set_mounted_roots

        online = str(self.root_a.resolve())
        offline = str(self.base / "missing-disk")
        set_mounted_roots([online, offline], primary=online, drop_offline=False)
        self.assertEqual(len(get_mounted_roots()), 2)

        # Old behavior would drop the offline sibling when re-saving mounts.
        activate_mount(self.root_a)
        mounts = get_mounted_roots()
        self.assertEqual(len(mounts), 2)
        self.assertTrue(any(m.lower() == offline.lower() for m in mounts))


if __name__ == "__main__":
    unittest.main()
