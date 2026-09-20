# -*- coding: utf-8 -*-
"""HLS index.m3u8 should display as parent folder name."""
from __future__ import annotations

import unittest

from vg.segments import (
    apply_hls_display_name,
    collapse_segment_sets,
    hls_entry_display_name,
    make_m3u8_entry,
)


class HlsDisplayNameTests(unittest.TestCase):
    def test_index_m3u8_uses_parent_folder(self) -> None:
        item = {
            "id": "a1",
            "name": "index",
            "filename": "index.m3u8",
            "rel": "Shows/CoolMovie/index.m3u8",
            "folder": "Shows/CoolMovie",
            "ext": ".m3u8",
            "kind": "m3u8",
            "size": 6400,
        }
        self.assertEqual(hls_entry_display_name(item), "CoolMovie")
        entry = make_m3u8_entry(item)
        self.assertEqual(entry["name"], "CoolMovie")

    def test_collapse_rewrites_ready_m3u8_stem(self) -> None:
        # Previously collapse kept raw stem "index" when kind was already m3u8.
        videos = [
            {
                "id": "a1",
                "name": "index",
                "filename": "index.m3u8",
                "rel": "Shows/CoolMovie/index.m3u8",
                "folder": "Shows/CoolMovie",
                "ext": ".m3u8",
                "kind": "m3u8",
                "size": 6400,
                "mtime": 1,
            }
        ]
        out = collapse_segment_sets(videos)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "CoolMovie")
        self.assertEqual(out[0]["kind"], "m3u8")

    def test_apply_fixes_persisted_row(self) -> None:
        item = {
            "id": "a1",
            "name": "index",
            "filename": "index.m3u8",
            "rel": "E盘片库/某剧/index.m3u8",
            "folder": "E盘片库/某剧",
            "ext": ".m3u8",
            "kind": "m3u8",
        }
        self.assertTrue(apply_hls_display_name(item))
        self.assertEqual(item["name"], "某剧")


if __name__ == "__main__":
    unittest.main()
