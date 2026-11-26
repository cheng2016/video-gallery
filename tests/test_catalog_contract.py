# -*- coding: utf-8 -*-
"""Behavior contract for the extracted catalog layer."""
from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from vg.catalog import (
    apply_catalog_to_state,
    build_category_facets,
    build_tree,
    compute_catalog,
    rebuild_indexes,
    video_category,
)
from vg.config import MIN_VIDEO_FILE_BYTES
from vg.state import STATE


class CatalogContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state = {
            key: STATE.get(key)
            for key in ("root", "videos", "by_id", "by_thumb_id", "by_category", "facets", "lib_gen")
        }
        STATE["root"] = Path("D:/library")
        STATE["videos"] = []
        STATE["by_id"] = {}
        STATE["by_category"] = {}
        STATE["facets"] = None
        STATE["lib_gen"] = 0

    def tearDown(self) -> None:
        for key, value in self.old_state.items():
            STATE[key] = value
        self.tmp.cleanup()

    @staticmethod
    def video(vid: str, rel: str, *, size: int = 1, root: str = "D:/library") -> dict:
        path = Path(rel)
        folder = str(path.parent).replace("\\", "/") if path.parent != Path(".") else ""
        return {
            "id": vid,
            "name": path.stem,
            "filename": path.name,
            "rel": rel,
            "folder": folder,
            "ext": path.suffix,
            "size": size,
            "root": root,
            "_lib_root": root,
            "genres": [],
        }

    def test_video_category_uses_first_normalized_folder(self) -> None:
        self.assertEqual(video_category({"folder": ""}), "")
        self.assertEqual(video_category({"folder": "电影/动作"}), "电影")
        self.assertEqual(video_category({"folder": "/电视剧/国产/"}), "电视剧")

    def test_build_tree_counts_and_order_without_rows(self) -> None:
        videos = [
            self.video("b", "综艺/b.mp4"),
            self.video("a", "电影/动作/a.mp4"),
            self.video("r", "root.mp4"),
        ]
        tree = build_tree(Path("D:/library"), videos)
        self.assertEqual(tree["count"], 3)
        self.assertEqual([child["name"] for child in tree["children"]], ["电影", "综艺"])
        self.assertEqual(tree["videos"], [])
        self.assertEqual(tree["children"][0]["count"], 1)

    def test_build_tree_can_embed_sorted_leaf_rows(self) -> None:
        videos = [
            self.video("b", "电影/B.mp4"),
            self.video("a", "电影/a.mp4"),
        ]
        tree = build_tree(Path("D:/library"), videos, with_videos=True)
        rows = tree["children"][0]["videos"]
        self.assertEqual([row["id"] for row in rows], ["a", "b"])

    def test_compute_is_pure_with_respect_to_state(self) -> None:
        videos = [self.video("a", "电影/a.mp4")]
        indexes = compute_catalog(videos)
        self.assertEqual(STATE["videos"], [])
        self.assertEqual(indexes["facets"]["count"], 1)
        self.assertEqual(indexes["by_id"]["a"], videos[0])

    def test_heavy_marks_duplicates_and_light_does_not_recompute_them(self) -> None:
        size = MIN_VIDEO_FILE_BYTES + 1
        heavy_rows = [
            self.video("a", "电影/same.mp4", size=size, root=self.tmp.name),
            self.video("b", "电影/copy.mp4", size=size, root=self.tmp.name),
        ]
        for row in heavy_rows:
            path = Path(row["root"]) / row["rel"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((b"same-content" * ((size // 12) + 1))[:size])
        compute_catalog(heavy_rows, heavy=True)
        self.assertTrue(all(row.get("dup") for row in heavy_rows))

        light_rows = [
            self.video("c", "电影/one.mp4", size=size),
            self.video("d", "电影/two.mp4", size=size),
        ]
        compute_catalog(light_rows, heavy=False)
        self.assertTrue(all("dup" not in row for row in light_rows))

    def test_apply_is_the_catalog_state_write_boundary(self) -> None:
        videos = [
            self.video("a", "电影/a.mp4", root="D:/one"),
            self.video("b", "动漫/b.mp4", root="E:/two"),
        ]
        before = STATE["lib_gen"]
        indexes = compute_catalog(videos)
        apply_catalog_to_state(videos, indexes)
        self.assertIs(STATE["videos"], videos)
        self.assertEqual(set(STATE["by_id"]), {"a", "b"})
        self.assertEqual(STATE["facets"]["count"], 2)
        self.assertEqual(STATE["lib_gen"], before + 1)
        self.assertEqual([row["_lib_root"] for row in videos], ["D:/one", "E:/two"])

    def test_rebuild_wrapper_keeps_legacy_state_behavior(self) -> None:
        videos = [self.video("a", "电影/a.mp4")]
        rebuild_indexes(videos)
        self.assertIs(STATE["videos"], videos)
        self.assertEqual(STATE["by_category"]["电影"], videos)

    def test_explicit_replacement_can_remove_rows_while_scanning(self) -> None:
        old = [self.video("a", "电影/a.mp4"), self.video("b", "电影/b.mp4")]
        for scanning in (False, True):
            for remaining in ([old[1]], []):
                with self.subTest(scanning=scanning, remaining=len(remaining)), patch.dict(
                    STATE, {"videos": old, "scanning": scanning}
                ), patch("vg.diagnostics.emit") as emit:
                    rebuild_indexes(remaining, heavy=False)
                    expected = {row["id"] for row in remaining}
                    self.assertIs(STATE["videos"], remaining)
                    self.assertEqual(set(STATE["by_id"]), expected)
                    self.assertEqual(set(STATE["by_thumb_id"]), expected)
                    self.assertEqual(STATE["facets"]["count"], len(remaining))
                    self.assertFalse(any(
                        call.args[1] == "catalog_rebuild_adopt_live_grown"
                        for call in emit.call_args_list
                    ))

    def test_current_state_rebuild_follows_concurrent_live_growth(self) -> None:
        for explicit_live in (False, True):
            old = [self.video("a", "电影/a.mp4")]
            grown = old + [self.video("b", "电影/b.mp4")]
            calls = 0

            def compute_and_grow(rows, *, heavy=True):
                nonlocal calls
                indexes = compute_catalog(rows, heavy=heavy)
                calls += 1
                if calls == 1:
                    STATE["videos"] = grown
                return indexes

            with self.subTest(explicit_live=explicit_live), patch.dict(
                STATE, {"videos": old, "scanning": True}
            ), patch("vg.catalog.compute_catalog", side_effect=compute_and_grow):
                rebuild_indexes(old if explicit_live else None, heavy=False)
                self.assertEqual([row["id"] for row in STATE["videos"]], ["a", "b"])
                self.assertEqual(set(STATE["by_id"]), {"a", "b"})
                self.assertEqual(STATE["facets"]["count"], 2)
                self.assertEqual(calls, 2)

    def test_category_order_matches_legacy_order(self) -> None:
        facets = build_category_facets({"其他": 4, "动漫": 1, "电影": 1, "自定义": 9, "": 2})
        self.assertEqual(
            [row["id"] for row in facets],
            ["电影", "动漫", "其他", "", "自定义"],
        )


if __name__ == "__main__":
    unittest.main()
