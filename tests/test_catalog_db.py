# -*- coding: utf-8 -*-
"""SQLite catalog: replace save, row UPSERT, and cross-cache probe donor."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vg import catalog_db
from vg.catalog_db import (
    CATALOG_DB_NAME,
    facets_from_rows,
    find_probe_donor,
    load_catalog_facet_rows,
    load_catalog_videos,
    catalog_mtime,
    query_catalog_facets,
    query_catalog_page,
    read_catalog_counts,
    read_catalog_validation_time,
    save_catalog,
    upsert_catalog_videos,
    write_catalog_validation_time,
)


class CatalogDbTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.old_vg = catalog_db.VGDATA_DIR
        catalog_db.VGDATA_DIR = self.base / "preview_cache"
        catalog_db.VGDATA_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        catalog_db.VGDATA_DIR = self.old_vg
        self._tmp.cleanup()

    def _item(
        self,
        *,
        vid: str,
        rel: str,
        size: int = 5_000_000,
        file_sig: str = "",
        duration: float | None = None,
        bad: bool = False,
    ) -> dict:
        name = Path(rel).stem
        out = {
            "id": vid,
            "name": name,
            "filename": Path(rel).name,
            "rel": rel,
            "folder": str(Path(rel).parent).replace("\\", "/") if "/" in rel or "\\" in rel else "",
            "ext": Path(rel).suffix.lower() or ".mp4",
            "size": size,
        }
        if file_sig:
            out["file_sig"] = file_sig
        if duration is not None:
            out["duration"] = duration
            out["probe_duration_done"] = True
        if bad:
            out["bad"] = True
        return out

    def test_save_catalog_creates_sqlite_not_json(self) -> None:
        root = self.base / "lib"
        cache = self.base / "cache"
        root.mkdir()
        item = self._item(vid="aaaaaaaaaaaaaaaa", rel="a.mp4")
        self.assertTrue(
            save_catalog(cache, root, [item], file_count=1, folder_counts={"": 1})
        )
        self.assertTrue((cache / CATALOG_DB_NAME).is_file())
        self.assertFalse((cache / "index.json").exists())
        videos = load_catalog_videos(cache, root)
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["id"], "aaaaaaaaaaaaaaaa")
        count, folders = read_catalog_counts(cache)
        self.assertEqual(count, 1)
        self.assertEqual(folders, {"": 1})

    def test_validation_marker_does_not_touch_catalog_mtime(self) -> None:
        root = self.base / "validation-lib"
        cache = self.base / "validation-cache"
        root.mkdir()
        self.assertTrue(save_catalog(cache, root, [self._item(vid="v" * 16, rel="a.mp4")]))
        before = catalog_mtime(cache)
        marker_value = 1234567890.125
        self.assertTrue(write_catalog_validation_time(cache, marker_value))
        self.assertAlmostEqual(read_catalog_validation_time(cache), marker_value, places=5)
        self.assertEqual(catalog_mtime(cache), before)
        self.assertTrue((cache / catalog_db.CATALOG_VALIDATION_MARKER_NAME).is_file())

    def test_upsert_updates_probe_fields_without_losing_other_rows(self) -> None:
        root = self.base / "lib"
        cache = self.base / "cache"
        root.mkdir()
        a = self._item(vid="aaaaaaaaaaaaaaaa", rel="a.mp4")
        b = self._item(vid="bbbbbbbbbbbbbbbb", rel="b.mp4")
        save_catalog(cache, root, [a, b], file_count=2, folder_counts={"": 2})

        a["duration"] = 12.5
        a["probe_duration_done"] = True
        n = upsert_catalog_videos(cache, root, [a], allow_insert=False)
        self.assertEqual(n, 1)

        by_rel = {v["rel"]: v for v in load_catalog_videos(cache, root)}
        self.assertEqual(len(by_rel), 2)
        self.assertEqual(by_rel["a.mp4"]["duration"], 12.5)
        self.assertEqual(by_rel["b.mp4"]["id"], "bbbbbbbbbbbbbbbb")

    def test_upsert_repairs_zero_byte_catalog_after_cached_schema_hint(self) -> None:
        root = self.base / "lib"
        cache = self.base / "cache"
        root.mkdir()
        cache.mkdir()
        db_path = cache / CATALOG_DB_NAME
        db_path.touch()
        cache_key = str(db_path.resolve()).casefold()
        with catalog_db._schema_ready_lock:
            catalog_db._schema_ready.add(cache_key)

        item = self._item(vid="cccccccccccccccc", rel="c.mp4")
        self.assertEqual(upsert_catalog_videos(cache, root, [item], allow_insert=True), 1)
        rows = load_catalog_videos(cache, root)
        self.assertEqual([row["id"] for row in rows], [item["id"]])

    def test_find_probe_donor_matches_sig_and_skips_bad(self) -> None:
        root_a = self.base / "disk-a"
        root_b = self.base / "disk-b"
        cache_a = catalog_db.VGDATA_DIR / "A_hash"
        cache_b = catalog_db.VGDATA_DIR / "B_hash"
        root_a.mkdir()
        root_b.mkdir()
        cache_a.mkdir(parents=True)
        cache_b.mkdir(parents=True)

        donor = self._item(
            vid="dddddddddddddddd",
            rel="same.mp4",
            size=8_000_000,
            file_sig="b2:8000000:face",
            duration=100.0,
        )
        bad = self._item(
            vid="xxxxxxxxxxxxxxxx",
            rel="same.mp4",
            size=8_000_000,
            file_sig="b2:8000000:face",
            bad=True,
        )
        save_catalog(cache_a, root_a, [donor])
        save_catalog(cache_b, root_b, [bad])

        hit = find_probe_donor(file_sig="b2:8000000:face", skip_bad=True)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit["duration"], 100.0)
        self.assertFalse(hit.get("bad"))

        by_name = find_probe_donor(
            name_key="same",
            size=8_000_000,
            skip_bad=True,
        )
        self.assertIsNotNone(by_name)
        assert by_name is not None
        self.assertEqual(by_name.get("duration"), 100.0)

    def test_sql_page_filters_sorts_and_returns_only_requested_rows(self) -> None:
        root = self.base / "large-lib"
        cache = self.base / "large-cache"
        root.mkdir()
        rows = []
        for i in range(500):
            row = self._item(
                vid=f"{i:016x}",
                rel=f"电影/动作/movie-{i:04d}.mp4",
                size=5_000_000 + i,
            )
            row["mtime"] = float(i)
            row["genres"] = ["动作"] if i % 2 == 0 else ["剧情"]
            rows.append(row)
        self.assertTrue(save_catalog(cache, root, rows))

        page, total = query_catalog_page(
            cache,
            category="电影",
            folder="电影/动作",
            include_descendants=True,
            genre="动作",
            sort="mtime_desc",
            offset=10,
            limit=60,
        )
        self.assertEqual(total, 250)
        self.assertEqual(len(page), 60)
        self.assertGreater(page[0]["mtime"], page[-1]["mtime"])
        facets = query_catalog_facets(
            cache,
            category="电影",
            folder="电影/动作",
        )
        self.assertEqual(
            {row["id"]: row["count"] for row in facets["genres"]},
            {"动作": 250, "剧情": 250},
        )

    def test_facet_rows_support_ext_and_type_views_without_requery(self) -> None:
        root = self.base / "library3"
        cache = self.base / "cache3"
        root.mkdir()
        rows = [
            self._item(vid="a" * 16, rel="电影/a.mp4", size=1),
            self._item(vid="b" * 16, rel="电影/b.mkv", size=2),
        ]
        rows[0]["ext"] = ".mp4"
        rows[0]["category"] = "电影"
        rows[0]["folder"] = "电影"
        rows[0]["genres"] = ["动作"]
        rows[1]["ext"] = ".mkv"
        rows[1]["category"] = "电影"
        rows[1]["folder"] = "电影"
        rows[1]["genres"] = ["剧情"]
        self.assertTrue(save_catalog(cache, root, rows))
        loaded = load_catalog_facet_rows(cache, category="电影")
        self.assertEqual(len(loaded), 2)
        with_ext = facets_from_rows(loaded, category="电影", folder="电影", ext=".mp4")
        without_ext = facets_from_rows(loaded, category="电影", folder="电影", ext="")
        self.assertEqual({row["id"] for row in with_ext["genres"]}, {"动作"})
        self.assertEqual({row["id"] for row in without_ext["types"]}, {".mp4", ".mkv"})
        self.assertEqual(query_catalog_facets(cache, category="电影", folder="电影", ext=".mp4")["genres"], with_ext["genres"])


if __name__ == "__main__":
    unittest.main()
