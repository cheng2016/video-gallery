# -*- coding: utf-8 -*-
"""Large-library contracts: pagination must stay bounded and SQL-backed."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vg import web
from vg.catalog_db import query_catalog_page, save_catalog
from vg.state import STATE


class LargeLibraryPerformanceTests(unittest.TestCase):
    def test_fifty_thousand_rows_return_one_bounded_sql_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "library"
            cache = base / "cache"
            root.mkdir()
            rows = [
                {
                    "id": f"{i:016x}",
                    "name": f"movie-{i:05d}",
                    "filename": f"movie-{i:05d}.mp4",
                    "rel": f"电影/movie-{i:05d}.mp4",
                    "folder": "电影",
                    "ext": ".mp4",
                    "size": 1_000_000_000 + i,
                    "mtime": float(i),
                    "genres": ["电影"],
                }
                for i in range(50_000)
            ]
            self.assertTrue(save_catalog(cache, root, rows))
            page, total = query_catalog_page(
                cache,
                category="电影",
                sort="mtime_desc",
                offset=5_000,
                limit=60,
            )
            self.assertEqual(total, 50_000)
            self.assertEqual(len(page), 60)
            self.assertGreater(page[0]["mtime"], page[-1]["mtime"])

    def test_api_sql_path_never_calls_full_memory_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "library"
            cache = base / "cache"
            root.mkdir()
            item = {
                "id": "a" * 16,
                "name": "movie",
                "filename": "movie.mp4",
                "rel": "电影/movie.mp4",
                "folder": "电影",
                "ext": ".mp4",
                "size": 1_000_000_000,
                "mtime": 1.0,
            }
            save_catalog(cache, root, [item])
            old = {
                key: STATE.get(key)
                for key in ("root", "mounted_roots", "lan_share", "lib_gen")
            }
            STATE.update({
                "root": root,
                "mounted_roots": [str(root)],
                "lan_share": False,
                "lib_gen": 123,
            })
            try:
                with (
                    mock.patch.object(web, "_cache_dir_from_root_hint", return_value=cache),
                    mock.patch.object(web, "videos_for_scope") as full_scope,
                ):
                    response = web.app.test_client().get(
                        f"/api/videos?lib={root.as_posix()}&category=电影&limit=60"
                    )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["count"], 1)
                full_scope.assert_not_called()
            finally:
                STATE.update(old)
                web.invalidate_response_caches()


if __name__ == "__main__":
    unittest.main()
