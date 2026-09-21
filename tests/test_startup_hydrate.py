# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from vg.disk_libs import _adopt_catalog_item, _disk_item
from vg.genres import GENRES_VERSION
from vg.taxonomy import TAXONOMY_VERSION


class AdoptCatalogItemTests(unittest.TestCase):
    def test_adopt_mutates_in_place_without_serialize_copy(self) -> None:
        raw = {
            "id": "v1",
            "name": "clip",
            "filename": "clip.mp4",
            "ext": ".mp4",
            "folder": "A/B",
            "_q": "clip",
        }
        cache = Path("D:/preview_cache/x")
        with patch("vg.schema.serialize_video_item") as ser:
            out = _adopt_catalog_item(raw, r"D:\lib", cache)
            ser.assert_not_called()
        self.assertIs(out, raw)
        self.assertEqual(out["root"], r"D:\lib")
        self.assertEqual(out["_lib_root"], r"D:\lib")
        self.assertEqual(out["_lib_cache"], str(cache))
        self.assertEqual(out["_folder_raw"], "A/B")
        self.assertEqual(out["_q"], "clip")

    def test_adopt_rewrites_hls_index_title(self) -> None:
        raw = {
            "id": "h1",
            "name": "index",
            "filename": "index.m3u8",
            "rel": "Shows/CoolMovie/index.m3u8",
            "folder": "Shows/CoolMovie",
            "ext": ".m3u8",
            "kind": "m3u8",
        }
        renames: list[str] = []
        out = _adopt_catalog_item(raw, r"D:\lib", Path("c"), rename_acc=renames)
        self.assertEqual(out["name"], "CoolMovie")
        self.assertTrue(renames)

    def test_disk_item_still_copies(self) -> None:
        raw = {"id": "v1", "name": "clip", "folder": "A", "ext": ".mp4"}
        out = _disk_item(raw, r"D:\lib", Path("c"))
        self.assertIsNot(out, raw)
        self.assertEqual(out["root"], r"D:\lib")


class ClassifyFilterTests(unittest.TestCase):
    def test_versions_already_current_need_zero(self) -> None:
        merged = [
            {"id": "a", "taxonomy_ver": TAXONOMY_VERSION, "genres_ver": GENRES_VERSION},
            {"id": "b", "taxonomy_ver": TAXONOMY_VERSION, "genres_ver": GENRES_VERSION},
        ]
        need = [
            v
            for v in merged
            if int(v.get("taxonomy_ver") or 0) != TAXONOMY_VERSION
            or int(v.get("genres_ver") or 0) != GENRES_VERSION
        ]
        self.assertEqual(need, [])

    def test_missing_versions_are_selected(self) -> None:
        merged = [
            {"id": "a", "taxonomy_ver": 0, "genres_ver": GENRES_VERSION},
            {"id": "b", "taxonomy_ver": TAXONOMY_VERSION, "genres_ver": 0},
            {"id": "c", "taxonomy_ver": TAXONOMY_VERSION, "genres_ver": GENRES_VERSION},
        ]
        need = [
            v
            for v in merged
            if int(v.get("taxonomy_ver") or 0) != TAXONOMY_VERSION
            or int(v.get("genres_ver") or 0) != GENRES_VERSION
        ]
        self.assertEqual([v["id"] for v in need], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
