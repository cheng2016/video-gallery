import json
import tempfile
import unittest
from pathlib import Path

from vg import cache
from vg import disk_libs
from vg import web
from vg.cache import ensure_cache_dir, thumb_cache_get, thumb_cache_put
from vg.catalog import rebuild_indexes
from vg.catalog_repository import find_video_by_id
from vg.disk_libs import (
    root_for_item,
    save_libraries_by_root,
    save_library_item,
    save_root_library,
)
from vg.roots import publish_unified_library, videos_for_scope
from vg.state import STATE
from vg.util import video_id


class MultiDiskPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root_a = self.base / "disk-a"
        self.root_b = self.base / "disk-b"
        self.root_a.mkdir()
        self.root_b.mkdir()
        (self.root_a / "same.mp4").write_bytes(b"a")
        (self.root_b / "same.mp4").write_bytes(b"b")

        self.old_vgdata = cache.VGDATA_DIR
        self.old_key = cache.KEY_FILE
        self.old_disk_vgdata = disk_libs.VGDATA_DIR
        cache.VGDATA_DIR = self.base / "cache"
        cache.KEY_FILE = cache.VGDATA_DIR / "vault.key"
        disk_libs.VGDATA_DIR = cache.VGDATA_DIR

        self.old_state = {
            key: STATE.get(key)
            for key in (
                "root",
                "cache_dir",
                "videos",
                "by_id",
                "by_category",
                "facets",
                "tree",
                "disk_libs",
                "mounted_roots",
            )
        }
        STATE["root"] = self.root_a
        STATE["cache_dir"] = ensure_cache_dir(self.root_a)
        STATE["videos"] = []
        STATE["by_id"] = {}
        STATE["disk_libs"] = {}
        STATE["mounted_roots"] = [str(self.root_a), str(self.root_b)]

    def tearDown(self):
        for key, value in self.old_state.items():
            STATE[key] = value
        cache.VGDATA_DIR = self.old_vgdata
        cache.KEY_FILE = self.old_key
        disk_libs.VGDATA_DIR = self.old_disk_vgdata
        self.tmp.cleanup()

    def item(self, root: Path, *, runtime_id=None, source_id=None):
        source_id = source_id or video_id("same.mp4")
        item = {
            "id": runtime_id or source_id,
            "name": "same",
            "filename": "same.mp4",
            "rel": "same.mp4",
            "folder": "",
            "ext": ".mp4",
            "size": 1,
            "_lib_root": str(root.resolve()),
            "_lib_cache": str(ensure_cache_dir(root)),
            "root": str(root.resolve()),
        }
        if runtime_id and runtime_id != source_id:
            item["_thumb_id"] = source_id
        return item

    def read_index(self, root: Path):
        path = ensure_cache_dir(root) / "index.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_unified_catalog_is_split_and_runtime_id_is_not_persisted(self):
        source_id = video_id("same.mp4")
        a = self.item(self.root_a, source_id=source_id)
        b = self.item(
            self.root_b,
            runtime_id="f" * 16,
            source_id=source_id,
        )

        saved = save_libraries_by_root([a, b])

        self.assertEqual(saved[str(self.root_a.resolve())], 1)
        self.assertEqual(saved[str(self.root_b.resolve())], 1)
        for root in (self.root_a, self.root_b):
            payload = self.read_index(root)
            self.assertEqual(payload["schema_ver"], 2)
            self.assertEqual(payload["root"], str(root.resolve()))
            self.assertEqual(len(payload["videos"]), 1)
            self.assertEqual(payload["videos"][0]["id"], source_id)
            self.assertEqual(payload["videos"][0]["_lib_root"], str(root.resolve()))
            self.assertNotIn("_thumb_id", payload["videos"][0])

    def test_foreign_item_cannot_be_written_to_another_root(self):
        a = self.item(self.root_a)
        b = self.item(self.root_b)

        persisted = save_root_library(self.root_a, [a, b])

        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0]["_lib_root"], str(self.root_a.resolve()))

    def test_single_item_update_only_changes_owning_index(self):
        a = self.item(self.root_a)
        b = self.item(self.root_b)
        save_libraries_by_root([a, b])
        before_a = self.read_index(self.root_a)

        b["duration"] = 123.0
        self.assertTrue(save_library_item(b))

        self.assertEqual(self.read_index(self.root_a), before_a)
        self.assertEqual(self.read_index(self.root_b)["videos"][0]["duration"], 123.0)

    def test_delete_uses_root_and_only_updates_that_disk(self):
        source_id = video_id("same.mp4")
        a = self.item(self.root_a, source_id=source_id)
        b = self.item(self.root_b, runtime_id="f" * 16, source_id=source_id)
        save_libraries_by_root([a, b])
        STATE["videos"] = [a, b]
        rebuild_indexes(STATE["videos"])

        old_move = web.move_to_trash
        web.move_to_trash = lambda _path: (True, "ok")
        try:
            response = web.app.test_client().post(
                "/api/delete",
                json={
                    "items": [
                        {
                            "id": b["id"],
                            "root": str(self.root_b.resolve()),
                            "rel": b["rel"],
                        }
                    ],
                    "trash": True,
                },
            )
        finally:
            web.move_to_trash = old_move

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["removed"], [b["id"]])
        self.assertEqual(len(self.read_index(self.root_a)["videos"]), 1)
        self.assertEqual(len(self.read_index(self.root_b)["videos"]), 0)

    def test_thumbnail_memory_cache_is_isolated_by_disk_cache(self):
        vid = video_id("same.mp4")
        cache_a = ensure_cache_dir(self.root_a)
        cache_b = ensure_cache_dir(self.root_b)

        thumb_cache_put(vid, b"a-jpeg", cache_a)
        thumb_cache_put(vid, b"b-jpeg", cache_b)

        self.assertEqual(thumb_cache_get(vid, cache_a), b"a-jpeg")
        self.assertEqual(thumb_cache_get(vid, cache_b), b"b-jpeg")

    def test_offline_item_never_falls_back_to_active_root(self):
        offline = self.base / "offline-disk"
        item = self.item(self.root_b)
        item["_lib_root"] = str(offline)
        item["root"] = str(offline)

        self.assertEqual(root_for_item(item), offline)

    def test_complete_disk_index_beats_partial_scan_state(self):
        first = self.item(self.root_a)
        second = dict(first)
        second["id"] = "e" * 16
        second["rel"] = "second.mp4"
        second["filename"] = "second.mp4"
        save_root_library(self.root_a, [first, second])
        STATE["videos"] = [first]

        scoped = videos_for_scope(str(self.root_a))

        self.assertEqual({v["rel"] for v in scoped}, {"same.mp4", "second.mp4"})

    def test_preferred_root_never_falls_back_to_foreign_disk(self):
        item = self.item(self.root_a)
        save_root_library(self.root_a, [item])
        STATE["videos"] = [item]
        rebuild_indexes(STATE["videos"])

        self.assertIsNone(find_video_by_id(item["id"], prefer_root=str(self.root_b)))

    def test_video_api_attaches_thumbnails_before_stripping_private_fields(self):
        source_id = video_id("same.mp4")
        a = self.item(self.root_a, source_id=source_id)
        b = self.item(self.root_b, source_id=source_id)
        save_libraries_by_root([a, b])
        thumb_cache_put(source_id, b"a-jpeg", ensure_cache_dir(self.root_a))
        thumb_cache_put(source_id, b"b-jpeg", ensure_cache_dir(self.root_b))
        STATE["videos"] = []
        publish_unified_library()

        response = web.app.test_client().get("/api/videos?view=flat&limit=20")
        rows = response.get_json()["videos"]

        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["has_thumb"] for row in rows))
        self.assertTrue(all(row["thumb_id"] == source_id for row in rows))
        self.assertEqual({row["root"] for row in rows}, {str(self.root_a), str(self.root_b)})


if __name__ == "__main__":
    unittest.main()
