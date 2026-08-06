# -*- coding: utf-8 -*-
"""Shared video schema and persistence contract tests."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from vg.cache import save_index
from vg.schema import INDEX_SCHEMA_VERSION, RUNTIME_ONLY_FIELDS, serialize_video_item


class VideoSchemaContractTests(unittest.TestCase):
    def item(self) -> dict:
        return {
            "id": "runtime-id",
            "_thumb_id": "source-id",
            "name": "demo",
            "filename": "demo.mp4",
            "rel": "电影/demo.mp4",
            "folder": "电影",
            "ext": ".mp4",
            "size": 123,
            "custom_future_field": {"kept": True},
            "_q": "search",
            "_lib_label": "D:",
            "lib_label": "D:",
            "dup": True,
            "dup_n": 2,
            "dup_reason": "同名",
            "series_id": "series",
            "series_title": "series",
            "series_n": 1,
            "cover_id": "cover",
            "is_series": True,
            "thumb_id": "source-id",
            "episodes": [{"id": "episode"}],
        }

    def test_serializer_strips_all_runtime_fields_and_preserves_unknown_fields(self) -> None:
        item = self.item()
        original = dict(item)

        saved = serialize_video_item(item)

        self.assertEqual(saved["id"], "source-id")
        self.assertEqual(saved["custom_future_field"], {"kept": True})
        self.assertEqual(saved["_folder_raw"], "电影")
        self.assertTrue(RUNTIME_ONLY_FIELDS.isdisjoint(saved))
        self.assertEqual(item, original, "serialization must not mutate runtime state")

    def test_owner_normalization_is_centralized(self) -> None:
        saved = serialize_video_item(
            self.item(),
            root=Path("D:/library"),
            cache=Path("D:/cache"),
        )
        self.assertEqual(saved["root"], str(Path("D:/library")))
        self.assertEqual(saved["_lib_root"], str(Path("D:/library")))
        self.assertEqual(saved["_lib_cache"], str(Path("D:/cache")))

    def test_raw_save_index_uses_same_runtime_field_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            cache = Path(tmp) / "cache"
            root.mkdir()

            self.assertTrue(save_index(cache, root, [self.item()]))
            payload = json.loads((cache / "index.json").read_text(encoding="utf-8"))
            row = payload["videos"][0]

            self.assertEqual(payload["schema_ver"], INDEX_SCHEMA_VERSION)
            self.assertEqual(row["id"], "source-id")
            self.assertTrue(RUNTIME_ONLY_FIELDS.isdisjoint(row))
            self.assertIn("custom_future_field", row)


if __name__ == "__main__":
    unittest.main()
