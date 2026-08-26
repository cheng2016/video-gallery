# -*- coding: utf-8 -*-
"""Second-open cache path: trust index, count folders, reuse thumbs."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vg.config import THUMB_EXT
from vg.cache import save_index
from vg.scan import (
    _bg_count_then_maybe_scan,
    changed_folder_keys,
    count_video_files_by_folder,
    load_or_scan,
)
from vg.state import STATE, release_scan_lock, scan_lock_status, try_acquire_scan_lock
from vg.thumbs import link_or_copy_thumb, reuse_existing_thumb


class FolderCountTests(unittest.TestCase):
    def test_count_video_files_by_folder_skips_non_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "movies").mkdir()
            (root / "a.mp4").write_bytes(b"x")
            (root / "movies" / "b.mkv").write_bytes(b"x")
            (root / "movies" / "note.txt").write_bytes(b"x")
            counts = count_video_files_by_folder(root)
            self.assertEqual(counts.get(""), 1)
            self.assertEqual(counts.get("movies"), 1)

    def test_changed_folder_keys_only_reports_mismatches(self) -> None:
        stored = {"": 1, "movies": 2, "old": 3}
        live = {"": 1, "movies": 4}
        self.assertEqual(changed_folder_keys(stored, live), {"movies", "old"})

    def test_parallel_count_matches_serial_on_users_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clip.mp4").write_bytes(b"x")
            (root / "Drivers").mkdir()
            (root / "Drivers" / "show.mp4").write_bytes(b"x")
            users = root / "Users"
            users.mkdir()
            (users / "readme.mp4").write_bytes(b"x")
            for name in ("alice", "bob", "public"):
                folder = users / name / "Videos"
                folder.mkdir(parents=True)
                (folder / f"{name}.mp4").write_bytes(b"x")
            with mock.patch("vg.scan.os.cpu_count", return_value=1):
                serial = count_video_files_by_folder(root, emit_diagnostics=False)
            with mock.patch("vg.scan.os.cpu_count", return_value=8):
                parallel = count_video_files_by_folder(root, emit_diagnostics=False)
            self.assertEqual(serial, parallel)
            self.assertEqual(serial.get(""), 1)
            self.assertEqual(serial.get("Drivers"), 1)
            self.assertEqual(serial.get("Users"), 1)
            self.assertEqual(serial.get("Users/alice/Videos"), 1)
            self.assertEqual(serial.get("Users/bob/Videos"), 1)
            self.assertEqual(serial.get("Users/public/Videos"), 1)


class CacheTrustTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "lib"
        self.cache = Path(self._tmp.name) / "cache"
        self.root.mkdir()
        self.cache.mkdir()
        self._old = {
            key: STATE.get(key)
            for key in ("videos", "root", "cache_dir", "tree", "by_id", "scanning", "lib_gen", "updating")
        }

    def tearDown(self) -> None:
        for key, value in self._old.items():
            STATE[key] = value
        self._tmp.cleanup()

    def test_load_or_scan_keeps_index_rows_without_statting_files(self) -> None:
        ghost = {
            "id": "deadbeefdeadbeef",
            "name": "gone",
            "filename": "gone.mp4",
            "rel": "gone.mp4",
            "folder": "",
            "ext": ".mp4",
            "size": 999,
            "has_thumb": True,
        }
        save_index(self.cache, self.root, [ghost], file_count=1, folder_counts={"": 1})
        with (
            mock.patch("vg.scan.ensure_cache_dir", return_value=self.cache),
            mock.patch("vg.scan.start_metadata_enrichment"),
            mock.patch("vg.scan.save_prefs"),
            mock.patch("vg.scan.save_index") as save,
            mock.patch("vg.roots.on_scan_finished"),
        ):
            ok = load_or_scan(self.root, do_thumbs=False, force=False, background=False)
        self.assertTrue(ok)
        save.assert_not_called()
        self.assertIn("deadbeefdeadbeef", {v["id"] for v in STATE.get("videos") or []})

    def test_matching_folder_counts_skip_scan_videos(self) -> None:
        item = {
            "id": "aaaaaaaaaaaaaaaa",
            "name": "clip",
            "filename": "clip.mp4",
            "rel": "clip.mp4",
            "folder": "",
            "ext": ".mp4",
            "size": 10,
            "has_thumb": True,
        }
        save_index(self.cache, self.root, [item], file_count=1, folder_counts={"": 1})
        STATE["videos"] = [item]
        STATE["root"] = self.root
        with (
            mock.patch("vg.scan.ensure_cache_dir", return_value=self.cache),
            mock.patch("vg.scan.count_video_files_by_folder", return_value={"": 1}),
            mock.patch("vg.scan.scan_videos") as scan,
            mock.patch("vg.scan.fill_thumbs_for_videos") as fill,
        ):
            _bg_count_then_maybe_scan(self.root, do_thumbs=True)
        scan.assert_not_called()
        fill.assert_called_once()
        self.assertFalse(STATE.get("updating"))

    def test_matching_folder_counts_keep_library_stable_during_count(self) -> None:
        item = {
            "id": "aaaaaaaaaaaaaaaa",
            "name": "clip",
            "filename": "clip.mp4",
            "rel": "clip.mp4",
            "folder": "",
            "ext": ".mp4",
            "size": 10,
            "has_thumb": True,
        }
        save_index(self.cache, self.root, [item], file_count=1, folder_counts={"": 1})
        STATE["videos"] = [item]
        STATE["root"] = self.root
        STATE["updating"] = False
        seen_updating: list[bool] = []
        lock_free_during_count: list[bool] = []

        def counting(_root):
            seen_updating.append(bool(STATE.get("updating")))
            # Background validation must not hold the global scan lock while
            # counting — otherwise Scan on another drive looks hung.
            status = scan_lock_status()
            lock_free_during_count.append(not status.get("held"))
            acquired = try_acquire_scan_lock("test_other_disk_scan")
            if acquired:
                release_scan_lock()
            lock_free_during_count.append(acquired)
            return {"": 1}

        with (
            mock.patch("vg.scan.ensure_cache_dir", return_value=self.cache),
            mock.patch("vg.scan.count_video_files_by_folder", side_effect=counting),
            mock.patch("vg.scan.scan_videos") as scan,
            mock.patch("vg.scan.fill_thumbs_for_videos"),
        ):
            _bg_count_then_maybe_scan(self.root, do_thumbs=True)
        scan.assert_not_called()
        self.assertEqual(seen_updating, [False])
        self.assertEqual(lock_free_during_count, [True, True])
        self.assertFalse(STATE.get("updating"))

    def test_changed_folder_runs_targeted_scan(self) -> None:
        item = {
            "id": "bbbbbbbbbbbbbbbb",
            "name": "clip",
            "filename": "clip.mp4",
            "rel": "movies/clip.mp4",
            "folder": "movies",
            "ext": ".mp4",
            "size": 10,
        }
        save_index(
            self.cache,
            self.root,
            [item],
            file_count=2,
            folder_counts={"": 1, "movies": 1},
        )
        STATE["root"] = self.root
        with (
            mock.patch("vg.scan.ensure_cache_dir", return_value=self.cache),
            mock.patch(
                "vg.scan.count_video_files_by_folder",
                return_value={"": 1, "movies": 3},
            ),
            mock.patch("vg.scan.scan_videos") as scan,
            mock.patch("vg.scan.fill_thumbs_for_videos") as fill,
        ):
            _bg_count_then_maybe_scan(self.root, do_thumbs=True)
        scan.assert_called_once()
        kwargs = scan.call_args.kwargs
        self.assertEqual(kwargs["only_folders"], {"movies"})
        self.assertFalse(kwargs["burst_thumbs"])
        fill.assert_not_called()

    def test_changed_folder_marks_updating_only_during_scan(self) -> None:
        item = {
            "id": "bbbbbbbbbbbbbbbb",
            "name": "clip",
            "filename": "clip.mp4",
            "rel": "movies/clip.mp4",
            "folder": "movies",
            "ext": ".mp4",
            "size": 10,
        }
        save_index(
            self.cache,
            self.root,
            [item],
            file_count=2,
            folder_counts={"": 1, "movies": 1},
        )
        STATE["root"] = self.root
        during_scan: list[bool] = []

        def scanning(*_args, **_kwargs):
            during_scan.append(bool(STATE.get("updating")))

        with (
            mock.patch("vg.scan.ensure_cache_dir", return_value=self.cache),
            mock.patch(
                "vg.scan.count_video_files_by_folder",
                return_value={"": 1, "movies": 3},
            ),
            mock.patch("vg.scan.scan_videos", side_effect=scanning),
            mock.patch("vg.scan.fill_thumbs_for_videos"),
        ):
            _bg_count_then_maybe_scan(self.root, do_thumbs=True)
        self.assertEqual(during_scan, [True])
        self.assertFalse(STATE.get("updating"))


class ThumbReuseTests(unittest.TestCase):
    def test_link_or_copy_reuses_bytes_without_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp) / "a"
            dest_dir = Path(tmp) / "b"
            src_dir.mkdir()
            dest_dir.mkdir()
            src = src_dir / f"src{THUMB_EXT}"
            dest = dest_dir / f"dest{THUMB_EXT}"
            src.write_bytes(b"\xff\xd8" + b"y" * 80)
            self.assertTrue(link_or_copy_thumb(src, dest))
            self.assertEqual(dest.read_bytes(), src.read_bytes())

    def test_reuse_existing_thumb_matches_file_sig(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_a = Path(tmp) / "cache_a"
            cache_b = Path(tmp) / "cache_b"
            cache_a.mkdir()
            cache_b.mkdir()
            vid_a = "1111111111111111"
            vid_b = "2222222222222222"
            src = cache_a / f"{vid_a}{THUMB_EXT}"
            src.write_bytes(b"\xff\xd8" + b"z" * 80)
            item = {
                "id": vid_b,
                "name": "same",
                "filename": "same.mp4",
                "size": 5_000_000,
                "file_sig": "b2:5000000:abcd",
            }
            sources = {"sig:b2:5000000:abcd": src}
            with mock.patch("vg.thumbs.build_thumb_source_index", return_value=sources):
                self.assertTrue(reuse_existing_thumb(item, cache_b, sources))
            dest = cache_b / f"{vid_b}{THUMB_EXT}"
            self.assertTrue(dest.is_file())
            self.assertTrue(item.get("has_thumb"))


if __name__ == "__main__":
    unittest.main()
