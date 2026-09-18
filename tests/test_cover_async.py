# -*- coding: utf-8 -*-
"""Cover seek must not block the POST thread on ffmpeg."""
from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from vg import web
from vg.state import STATE
from vg.thumb_jobs import THUMB_PRIORITY_VISIBLE


class CoverAsyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = web.app.test_client()
        self.old_ffmpeg = STATE.get("ffmpeg")
        self.old_jobs = STATE.get("cover_jobs")
        STATE["ffmpeg"] = "ffmpeg"
        STATE["cover_jobs"] = {}

    def tearDown(self) -> None:
        STATE["ffmpeg"] = self.old_ffmpeg
        STATE["cover_jobs"] = self.old_jobs if self.old_jobs is not None else {}

    def test_seek_post_queues_and_poll_completes(self) -> None:
        vid = "a" * 16
        item = {"id": vid, "name": "clip", "rel": "clip.mp4", "has_thumb": False}
        captured = []

        def capture(key, work, *, priority=10, force=False):
            captured.append({"key": key, "work": work, "priority": priority, "force": force})
            fut = Future()
            return fut

        with TemporaryDirectory() as td:
            cache = Path(td)
            src = cache / "clip.mp4"
            src.write_bytes(b"video")
            with (
                mock.patch.object(web, "find_video_by_id", return_value=item),
                mock.patch.object(web, "cache_dir_for_item", return_value=cache),
                mock.patch.object(web, "_video_file_for_thumb", return_value=src),
                mock.patch.object(web, "submit_thumbnail_job", side_effect=capture),
                mock.patch.object(web, "make_thumbnail", return_value=True) as make,
                mock.patch.object(web, "thumb_version", return_value=99),
                mock.patch.object(web, "save_library_item"),
                mock.patch.object(web, "thumb_cache_invalidate"),
                mock.patch.object(web, "clear_thumbnail_failure"),
            ):
                posted = self.client.post(
                    f"/api/thumb/{vid}",
                    json={"seek": 12.5},
                )
                body = posted.get_json()
                self.assertEqual(posted.status_code, 200)
                self.assertTrue(body["ok"])
                self.assertEqual(body["status"], "queued")
                self.assertTrue(body["job_id"])
                make.assert_not_called()
                self.assertEqual(captured[0]["priority"], THUMB_PRIORITY_VISIBLE)
                self.assertTrue(captured[0]["force"])

                queued = self.client.get(f"/api/thumb/job/{body['job_id']}")
                self.assertEqual(queued.status_code, 200)
                self.assertEqual(queued.get_json()["status"], "queued")

                self.assertTrue(captured[0]["work"]())
                make.assert_called_once()
                done = self.client.get(f"/api/thumb/job/{body['job_id']}")
                data = done.get_json()
                self.assertEqual(data["status"], "done")
                self.assertEqual(data["thumb_v"], 99)
                self.assertIn("12.5", data["msg"])

    def test_seek_job_error_is_pollable(self) -> None:
        vid = "b" * 16
        item = {"id": vid, "name": "clip", "rel": "clip.mp4"}
        captured = []

        def capture(key, work, *, priority=10, force=False):
            captured.append(work)
            return Future()

        with TemporaryDirectory() as td:
            cache = Path(td)
            src = cache / "clip.mp4"
            src.write_bytes(b"video")
            with (
                mock.patch.object(web, "find_video_by_id", return_value=item),
                mock.patch.object(web, "cache_dir_for_item", return_value=cache),
                mock.patch.object(web, "_video_file_for_thumb", return_value=src),
                mock.patch.object(web, "submit_thumbnail_job", side_effect=capture),
                mock.patch.object(web, "make_thumbnail", return_value=False),
                mock.patch.object(web, "save_library_item"),
                mock.patch.object(web, "mark_thumbnail_failure"),
                mock.patch.object(web, "clear_thumbnail_failure"),
            ):
                posted = self.client.post(f"/api/thumb/{vid}", json={"seek": 3})
                job_id = posted.get_json()["job_id"]
                captured[0]()
                data = self.client.get(f"/api/thumb/job/{job_id}").get_json()
                self.assertEqual(data["status"], "error")
                self.assertFalse(data["ok"])


if __name__ == "__main__":
    unittest.main()
