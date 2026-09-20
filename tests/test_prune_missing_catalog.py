# -*- coding: utf-8 -*-
"""Prune catalog rows whose source files were deleted."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vg.scan import (
    _folder_hit_by_targets,
    prune_missing_catalog_items,
)


class PruneMissingCatalogTests(unittest.TestCase):
    def test_drops_missing_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            alive = root / "keep.mp4"
            alive.write_bytes(b"1234")
            videos = [
                {
                    "id": "a",
                    "kind": "file",
                    "rel": "keep.mp4",
                    "name": "keep",
                    "ext": ".mp4",
                },
                {
                    "id": "b",
                    "kind": "file",
                    "rel": "gone.mp4",
                    "name": "gone",
                    "ext": ".mp4",
                },
            ]
            out = prune_missing_catalog_items(videos, root, log_tag="test")
            self.assertEqual([v["id"] for v in out], ["a"])

    def test_drops_missing_m3u8(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "Show"
            folder.mkdir()
            videos = [
                {
                    "id": "m",
                    "kind": "m3u8",
                    "rel": "Show/index.m3u8",
                    "folder": "Show",
                    "name": "Show",
                    "ext": ".m3u8",
                }
            ]
            out = prune_missing_catalog_items(videos, root, log_tag="test")
            self.assertEqual(out, [])

    def test_folder_hit_includes_descendants(self) -> None:
        targets = {"Movies/A"}
        self.assertTrue(_folder_hit_by_targets("Movies/A", targets))
        self.assertTrue(_folder_hit_by_targets("Movies/A/sub", targets))
        self.assertFalse(_folder_hit_by_targets("Movies/B", targets))


if __name__ == "__main__":
    unittest.main()
