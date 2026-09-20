# -*- coding: utf-8 -*-
"""Hot-path root compare must stay filesystem-free (by-ids stall regression)."""
from __future__ import annotations

import unittest
from unittest import mock

from vg.catalog_repository import (
    RuntimeCatalogRepository,
    _root_compare_key,
    _same_root,
)
from vg.state import STATE


class CatalogRootCompareTests(unittest.TestCase):
    def test_drive_root_forms_compare_equal(self):
        self.assertEqual(_root_compare_key("E:"), _root_compare_key("E:\\"))
        self.assertTrue(_same_root("E:\\", "E:"))
        self.assertTrue(_same_root("E:/Videos", "e:\\Videos"))
        self.assertFalse(_same_root("E:\\", "C:\\"))

    def test_find_video_busy_skips_disk_libs(self):
        repo = RuntimeCatalogRepository()
        STATE["scanning"] = True
        STATE["updating"] = False
        STATE["meta_progress"] = ""
        STATE["by_id"] = {}
        STATE["by_thumb_id"] = {}
        STATE["videos"] = [
            {"id": "other", "root": "E:\\", "_lib_root": "E:\\"},
        ]
        try:
            with mock.patch(
                "vg.catalog_repository.find_in_disk_libs"
            ) as find_disk, mock.patch(
                "vg.catalog_repository.read_root_library"
            ) as read_lib:
                hit = repo.find_video("missing", prefer_root="C:\\")
                self.assertIsNone(hit)
                find_disk.assert_not_called()
                read_lib.assert_not_called()
        finally:
            STATE["scanning"] = False
            STATE["videos"] = []
            STATE["by_id"] = {}
            STATE["by_thumb_id"] = {}

    def test_find_video_prefer_uses_memory_without_resolve(self):
        repo = RuntimeCatalogRepository()
        item = {"id": "abc", "root": "E:\\", "_lib_root": "E:\\", "name": "x"}
        STATE["scanning"] = False
        STATE["updating"] = False
        STATE["meta_progress"] = ""
        STATE["by_id"] = {"abc": item}
        STATE["by_thumb_id"] = {}
        STATE["videos"] = [item]
        try:
            with mock.patch("pathlib.Path.resolve", side_effect=AssertionError("resolve")):
                hit = repo.find_video("abc", prefer_root="E:")
                self.assertIs(hit, item)
        finally:
            STATE["by_id"] = {}
            STATE["videos"] = []


if __name__ == "__main__":
    unittest.main()
