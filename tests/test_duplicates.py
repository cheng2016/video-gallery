# -*- coding: utf-8 -*-
"""Duplicate badges and cleanup groups must use exactly one rule set."""
from __future__ import annotations

import unittest

from vg.config import MIN_VIDEO_FILE_BYTES
from vg.duplicates import find_duplicate_groups, mark_duplicates


class DuplicateRuleTests(unittest.TestCase):
    def video(self, vid: str, name: str, size: int, *, folder: str = "电影", kind: str = "") -> dict:
        item = {
            "id": vid,
            "name": name,
            "filename": f"{name}.mp4",
            "rel": f"{folder}/{vid}.mp4",
            "folder": folder,
            "root": "D:/library",
            "ext": ".mp4",
            "size": size,
        }
        if kind:
            item["kind"] = kind
        return item

    def test_groups_and_runtime_badges_share_reasons(self) -> None:
        size = MIN_VIDEO_FILE_BYTES + 1
        videos = [
            self.video("a", "Same", size),
            self.video("b", " same ", size),
            self.video("c", "Other", size + 1),
        ]

        groups = find_duplicate_groups(videos)
        mark_duplicates(videos)

        reasons_by_id: dict[str, set[str]] = {}
        for group in groups:
            for item in group["items"]:
                reasons_by_id.setdefault(item["id"], set()).add(group["reason"])
        for item in videos:
            marked = set((item.get("dup_reason") or "").split("+")) - {""}
            self.assertEqual(marked, reasons_by_id.get(item["id"], set()))

        self.assertEqual({group["reason"] for group in groups}, {"同名", "同体积"})
        self.assertEqual(videos[0]["dup_n"], 2)
        self.assertEqual(videos[1]["dup_n"], 2)
        self.assertNotIn("dup", videos[2])

    def test_small_same_size_and_synthetic_kinds_are_excluded(self) -> None:
        small = MIN_VIDEO_FILE_BYTES - 1
        videos = [
            self.video("a", "One", small),
            self.video("b", "Two", small),
            self.video("c", "Playlist", MIN_VIDEO_FILE_BYTES, kind="m3u8"),
            self.video("d", "Playlist", MIN_VIDEO_FILE_BYTES, kind="m3u8"),
        ]
        self.assertEqual(find_duplicate_groups(videos), [])

    def test_same_physical_entry_is_not_duplicated(self) -> None:
        item = self.video("a", "Same", MIN_VIDEO_FILE_BYTES)
        self.assertEqual(find_duplicate_groups([item, item]), [])


if __name__ == "__main__":
    unittest.main()
