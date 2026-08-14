# -*- coding: utf-8 -*-
"""Independent theme/background classification contracts."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from vg.catalog import compute_catalog, video_category
from vg.search import parse_search_query, video_matches_query
from vg.taxonomy import TAXONOMY_VERSION, ensure_video_taxonomy


def video(vid: str, rel: str, name: str) -> dict:
    folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
    return {
        "id": vid,
        "name": name,
        "filename": rel.rsplit("/", 1)[-1],
        "rel": rel,
        "folder": folder,
        "ext": ".mp4",
        "size": 100,
        "mtime": 1,
        "genres": [],
    }


class TaxonomyTests(unittest.TestCase):
    def test_taxonomy_does_not_change_folder_channel(self) -> None:
        item = video("one", "电影/科幻/星际救援.mp4", "星际救援")
        original_folder = item["folder"]

        themes, backgrounds = ensure_video_taxonomy(item)

        self.assertEqual(item["folder"], original_folder)
        self.assertEqual(video_category(item), "电影")
        self.assertIn("科幻", themes)
        self.assertIn("太空", backgrounds)

    def test_theme_and_background_are_independent_axes(self) -> None:
        item = video("two", "电视剧/校园/浪漫爱情故事.mp4", "浪漫爱情故事")
        themes, backgrounds = ensure_video_taxonomy(item)
        self.assertIn("爱情", themes)
        self.assertIn("校园", backgrounds)
        self.assertNotIn("校园", themes)

    def test_old_taxonomy_is_refreshed_by_rule_version(self) -> None:
        item = video("three", "电影/自然纪录片.mp4", "自然纪录片")
        item.update({"themes": ["错误"], "backgrounds": ["错误"], "taxonomy_ver": 0})
        themes, backgrounds = ensure_video_taxonomy(item)
        self.assertEqual(item["taxonomy_ver"], TAXONOMY_VERSION)
        self.assertNotIn("错误", themes + backgrounds)
        self.assertIn("纪录片", themes)
        self.assertIn("自然", backgrounds)

    def test_catalog_exposes_facets_without_changing_categories(self) -> None:
        videos = [
            video("a", "电影/科幻/星际远征.mp4", "星际远征"),
            video("b", "电影/校园/青春爱情.mp4", "青春爱情"),
        ]
        indexes = compute_catalog(videos)
        self.assertEqual(set(indexes["by_category"]), {"电影"})
        self.assertIn("科幻", {row["id"] for row in indexes["facets"]["themes"]})
        self.assertIn("校园", {row["id"] for row in indexes["facets"]["backgrounds"]})

    def test_search_and_api_filters_support_both_axes(self) -> None:
        from vg import web

        space = video("space", "电影/科幻/星际远征.mp4", "星际远征")
        school = video("school", "电影/校园/青春爱情.mp4", "青春爱情")
        parsed = parse_search_query("theme:科幻 background:太空")
        self.assertEqual(parsed["theme"], "科幻")
        self.assertEqual(parsed["background"], "太空")
        self.assertTrue(video_matches_query(space, parsed, lambda item: item.get("name", "")))
        self.assertFalse(video_matches_query(school, parsed, lambda item: item.get("name", "")))

        with mock.patch.object(web, "videos_for_scope", return_value=[space, school]):
            result = web._prepare_video_query(
                "", "电影", "", False, "", "科幻", "太空", "", {}, "", "flat", "name"
            )
        self.assertEqual([item["id"] for item in result[0]], ["space"])

    def test_frontend_renders_independent_filter_rows(self) -> None:
        html = (Path(__file__).resolve().parents[1] / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('id="themeRow"', html)
        self.assertIn('id="backgroundRow"', html)
        self.assertIn('params.set("theme", state.theme)', html)
        self.assertIn('params.set("background", state.background)', html)


if __name__ == "__main__":
    unittest.main()
