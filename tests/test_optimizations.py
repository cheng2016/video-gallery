# -*- coding: utf-8 -*-
"""Regression tests for index, scan and API hot-path hardening."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from vg.cache import save_index
from vg.scan import _file_fingerprint
from vg.state import STATE


class IndexWriteTests(unittest.TestCase):
    def test_concurrent_writers_leave_one_complete_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            cache = Path(tmp) / "cache"
            root.mkdir()
            barrier = threading.Barrier(8)
            results: list[bool] = []
            result_lock = threading.Lock()

            def write(i: int) -> None:
                barrier.wait()
                ok = save_index(
                    cache,
                    root,
                    [{"id": f"v{i}", "name": f"video-{i}", "rel": f"v{i}.mp4"}],
                )
                with result_lock:
                    results.append(ok)

            threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(len(results), 8)
            self.assertTrue(all(results))
            payload = json.loads((cache / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(len(payload["videos"]), 1)
            self.assertFalse(list(cache.glob("*.tmp")))


class IncrementalFingerprintTests(unittest.TestCase):
    def test_fingerprint_detects_same_size_content_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.mp4"
            path.write_bytes(b"A" * 200_000)
            before = path.stat()
            first = _file_fingerprint(path, before)
            path.write_bytes(b"B" * 200_000)
            path.touch()
            after = path.stat()
            self.assertEqual(before.st_size, after.st_size)
            self.assertNotEqual(first, _file_fingerprint(path, after))


class VideoResponseCacheTests(unittest.TestCase):
    def test_same_query_uses_cached_response_until_catalog_generation_changes(self) -> None:
        from vg import web

        item = {
            "id": "v1",
            "name": "video",
            "filename": "video.mp4",
            "rel": "video.mp4",
            "folder": "",
            "ext": ".mp4",
            "size": 100,
            "mtime": 1,
            "duration": None,
            "genres": [],
        }
        old = {
            key: STATE.get(key)
            for key in ("videos", "root", "mounted_roots", "lan_share", "lib_gen")
        }
        web._video_response_cache.clear()
        STATE.update({"videos": [item], "root": None, "mounted_roots": [], "lan_share": False, "lib_gen": 9001})
        calls = 0

        def scoped(_lib=None):
            nonlocal calls
            calls += 1
            return [item]

        try:
            with mock.patch.object(web, "videos_for_scope", side_effect=scoped):
                client = web.app.test_client()
                self.assertEqual(client.get("/api/videos?limit=1").status_code, 200)
                # A different page reuses the filtered/sorted query result;
                # only pagination/serialization runs again.
                self.assertEqual(client.get("/api/videos?offset=1&limit=1").status_code, 200)
                self.assertEqual(client.get("/api/videos?limit=1").status_code, 200)
                self.assertEqual(calls, 1)
                STATE["lib_gen"] += 1
                self.assertEqual(client.get("/api/videos?limit=1").status_code, 200)
                self.assertEqual(calls, 2)
        finally:
            for key, value in old.items():
                STATE[key] = value
            web._video_response_cache.clear()


if __name__ == "__main__":
    unittest.main()
