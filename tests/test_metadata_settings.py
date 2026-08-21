# -*- coding: utf-8 -*-
"""Metadata probing obeys the persistent Settings switches."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from vg import media, web
from vg.state import STATE


class MetadataSettingsTests(unittest.TestCase):
    def test_background_probe_is_disabled_when_both_switches_are_off(self) -> None:
        old_ffmpeg = STATE.get("ffmpeg")
        old_videos = STATE.get("videos")
        STATE["ffmpeg"] = "ffmpeg"
        STATE["videos"] = [{"id": "v1", "rel": "v1.mp4"}]
        try:
            with (
                mock.patch.object(media, "probe_duration_enabled", return_value=False),
                mock.patch.object(media, "probe_audio_enabled", return_value=False),
                mock.patch.object(media.threading, "Thread") as thread,
            ):
                media.start_metadata_enrichment()
            thread.assert_not_called()
        finally:
            STATE["ffmpeg"] = old_ffmpeg
            STATE["videos"] = old_videos

    def test_duration_only_probe_does_not_request_or_store_audio(self) -> None:
        with TemporaryDirectory() as td:
            video = Path(td) / "video.mp4"
            video.write_bytes(b"video")
            calls = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                return SimpleNamespace(
                    returncode=0,
                    stdout='{"streams":[{"codec_type":"video"}],"format":{"duration":"12.5"}}',
                    stderr="",
                )

            with (
                mock.patch.object(media, "_ffprobe_path", return_value="ffprobe"),
                mock.patch.object(media.subprocess, "run", side_effect=fake_run),
            ):
                info = media.probe_media_info(
                    "ffmpeg",
                    video,
                    include_duration=True,
                    include_audio=False,
                )

            command = calls[0]
            self.assertIn("format=duration", command)
            self.assertFalse(any("codec_name" in part for part in command))
            item = {}
            media._apply_probe_to_item(
                item,
                info,
                include_duration=True,
                include_audio=False,
            )
            self.assertEqual(item["duration"], 12.5)
            self.assertTrue(item["probe_duration_done"])
            self.assertNotIn("audio_codec", item)
            self.assertNotIn("probe_audio_done", item)

    def test_audio_only_probe_does_not_request_or_store_duration(self) -> None:
        with TemporaryDirectory() as td:
            video = Path(td) / "video.mp4"
            video.write_bytes(b"video")
            calls = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                return SimpleNamespace(
                    returncode=0,
                    stdout='{"streams":[{"codec_type":"video"},{"codec_type":"audio","codec_name":"aac"}]}',
                    stderr="",
                )

            with (
                mock.patch.object(media, "_ffprobe_path", return_value="ffprobe"),
                mock.patch.object(media.subprocess, "run", side_effect=fake_run),
            ):
                info = media.probe_media_info(
                    "ffmpeg",
                    video,
                    include_duration=False,
                    include_audio=True,
                )

            command = calls[0]
            self.assertFalse(any("format=duration" in part for part in command))
            self.assertTrue(any("codec_name" in part for part in command))
            item = {}
            media._apply_probe_to_item(
                item,
                info,
                include_duration=False,
                include_audio=True,
            )
            self.assertEqual(item["audio_codec"], "aac")
            self.assertTrue(item["probe_audio_done"])
            self.assertNotIn("duration", item)
            self.assertNotIn("probe_duration_done", item)

    def test_info_endpoint_does_not_probe_when_settings_are_off(self) -> None:
        item = {
            "id": "video-id",
            "name": "video",
            "filename": "video.mp4",
            "rel": "video.mp4",
            "ext": ".mp4",
            "size": 1000,
        }
        old_ffmpeg = STATE.get("ffmpeg")
        STATE["ffmpeg"] = "ffmpeg"
        try:
            with (
                mock.patch.object(web, "find_video_by_id", return_value=item),
                mock.patch.object(web, "probe_duration_enabled", return_value=False),
                mock.patch.object(web, "probe_audio_enabled", return_value=False),
                mock.patch.object(web, "probe_media_info") as probe,
                mock.patch.object(web, "_local_path_for_item", return_value=None),
            ):
                response = web.app.test_client().get("/api/info/video-id")
            self.assertEqual(response.status_code, 200)
            probe.assert_not_called()
        finally:
            STATE["ffmpeg"] = old_ffmpeg

    def test_info_endpoint_requests_only_enabled_duration(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "video.mp4"
            path.write_bytes(b"video")
            item = {
                "id": "video-id",
                "name": "video",
                "filename": "video.mp4",
                "rel": "video.mp4",
                "ext": ".mp4",
                "size": 200 * 1024,
            }
            old_ffmpeg = STATE.get("ffmpeg")
            STATE["ffmpeg"] = "ffmpeg"
            try:
                with (
                    mock.patch.object(web, "find_video_by_id", return_value=item),
                    mock.patch.object(web, "probe_duration_enabled", return_value=True),
                    mock.patch.object(web, "probe_audio_enabled", return_value=False),
                    mock.patch.object(web, "_item_probe_path", return_value=path),
                    mock.patch.object(
                        web,
                        "probe_media_info",
                        return_value={"ok": True, "duration": 9.0},
                    ) as probe,
                    mock.patch.object(web, "save_library_item"),
                    mock.patch.object(web, "_local_path_for_item", return_value=None),
                ):
                    response = web.app.test_client().get("/api/info/video-id")
                self.assertEqual(response.status_code, 200)
                probe.assert_called_once_with(
                    "ffmpeg",
                    path,
                    include_duration=True,
                    include_audio=False,
                )
                self.assertEqual(response.get_json()["duration"], 9.0)
                self.assertTrue(item["probe_duration_done"])
                self.assertNotIn("probe_audio_done", item)
            finally:
                STATE["ffmpeg"] = old_ffmpeg

    def test_info_endpoint_skips_probe_when_duration_already_cached(self) -> None:
        item = {
            "id": "video-id",
            "name": "video",
            "filename": "video.mp4",
            "rel": "video.mp4",
            "ext": ".mp4",
            "size": 200 * 1024,
            "duration": 42.0,
            "duration_h": "0:42",
        }
        old_ffmpeg = STATE.get("ffmpeg")
        STATE["ffmpeg"] = "ffmpeg"
        try:
            with (
                mock.patch.object(web, "find_video_by_id", return_value=item),
                mock.patch.object(web, "probe_duration_enabled", return_value=True),
                mock.patch.object(web, "probe_audio_enabled", return_value=False),
                mock.patch.object(web, "probe_media_info") as probe,
                mock.patch.object(web, "_local_path_for_item", return_value=None),
            ):
                response = web.app.test_client().get("/api/info/video-id")
            self.assertEqual(response.status_code, 200)
            probe.assert_not_called()
            self.assertEqual(response.get_json()["duration"], 42.0)
        finally:
            STATE["ffmpeg"] = old_ffmpeg

    def test_existing_duration_is_not_probed_again(self) -> None:
        self.assertFalse(
            media._needs_metadata_probe(
                {"duration": 12.5, "duration_h": "0:12"},
                want_duration=True,
                want_audio=False,
            )
        )
        self.assertFalse(
            media._needs_metadata_probe(
                {"probe_duration_done": True},
                want_duration=True,
                want_audio=False,
            )
        )
        self.assertTrue(
            media._needs_metadata_probe(
                {"duration": None, "duration_h": ""},
                want_duration=True,
                want_audio=False,
            )
        )

    def test_existing_audio_is_not_probed_again(self) -> None:
        self.assertFalse(
            media._needs_metadata_probe(
                {"audio_codec": "aac", "audio_hard": False},
                want_duration=False,
                want_audio=True,
            )
        )
        self.assertFalse(
            media._needs_metadata_probe(
                {"probe_audio_done": True},
                want_duration=False,
                want_audio=True,
            )
        )
        self.assertTrue(
            media._needs_metadata_probe(
                {"duration": 10.0},
                want_duration=False,
                want_audio=True,
            )
        )

    def test_background_enrichment_skips_already_probed_videos(self) -> None:
        old_ffmpeg = STATE.get("ffmpeg")
        old_videos = STATE.get("videos")
        old_running = getattr(media._state, "_meta_running", False)
        STATE["ffmpeg"] = "ffmpeg"
        STATE["videos"] = [
            {"id": "done", "rel": "done.mp4", "duration": 8.0, "duration_h": "0:08"},
            {"id": "todo", "rel": "todo.mp4", "duration": None},
        ]
        media._state._meta_running = False
        try:
            with (
                mock.patch.object(media, "probe_duration_enabled", return_value=True),
                mock.patch.object(media, "probe_audio_enabled", return_value=False),
                mock.patch.object(media, "enrich_metadata_parallel", return_value=(1, 0)) as enrich,
                mock.patch.object(media, "_persist_probed_items"),
                mock.patch.object(
                    media,
                    "adopt_metadata_from_catalog",
                    side_effect=lambda need, **_kw: (need, 0),
                ),
                mock.patch("vg.catalog.rebuild_indexes"),
            ):
                media._bg_enrich_metadata()
            enrich.assert_called_once()
            need = enrich.call_args[0][0]
            self.assertEqual([v["id"] for v in need], ["todo"])
        finally:
            STATE["ffmpeg"] = old_ffmpeg
            STATE["videos"] = old_videos
            media._state._meta_running = old_running

    def test_reuse_existing_metadata_matches_file_sig(self) -> None:
        item = {
            "id": "disk-b",
            "name": "same",
            "filename": "same.mp4",
            "size": 5_000_000,
            "file_sig": "b2:5000000:abcd",
            "duration": None,
        }
        sources = {
            "sig:b2:5000000:abcd": {
                "duration": 42.5,
                "duration_h": "0:42",
                "probe_duration_done": True,
                "audio_codec": "aac",
                "audio_hard": False,
                "probe_audio_done": True,
                "probe_ver": 1,
            }
        }
        self.assertTrue(
            media.reuse_existing_metadata(
                item,
                sources,
                want_duration=True,
                want_audio=True,
            )
        )
        self.assertEqual(item["duration"], 42.5)
        self.assertEqual(item["duration_h"], "0:42")
        self.assertEqual(item["audio_codec"], "aac")
        self.assertTrue(item["probe_duration_done"])
        self.assertTrue(item["probe_audio_done"])

    def test_adopt_metadata_from_catalog_reuses_other_disk_row(self) -> None:
        donor = {
            "id": "disk-a",
            "name": "Movie",
            "filename": "Movie.mkv",
            "size": 8_000_000,
            "file_sig": "b2:8000000:face",
            "duration": 100.0,
            "duration_h": "1:40",
            "probe_duration_done": True,
            "root": "D:/libA",
            "_lib_root": "D:/libA",
            "rel": "Movie.mkv",
        }
        need = [
            {
                "id": "disk-b",
                "name": "Movie",
                "filename": "Movie.mkv",
                "size": 8_000_000,
                "file_sig": "b2:8000000:face",
                "duration": None,
                "root": "E:/libB",
                "_lib_root": "E:/libB",
                "rel": "Movie.mkv",
            }
        ]
        with (
            mock.patch.object(media, "_persist_probed_items") as persist,
            mock.patch.object(
                media,
                "build_metadata_source_index",
                return_value={
                    "sig:b2:8000000:face": media._metadata_reuse_snapshot(
                        donor,
                        want_duration=True,
                        want_audio=False,
                    )
                },
            ),
        ):
            leftover, reused_n = media.adopt_metadata_from_catalog(
                need,
                want_duration=True,
                want_audio=False,
            )
        self.assertEqual(reused_n, 1)
        self.assertEqual(leftover, [])
        self.assertEqual(need[0]["duration"], 100.0)
        self.assertTrue(need[0]["probe_duration_done"])
        persist.assert_called_once()

    def test_adopt_metadata_does_not_query_sqlite_for_each_unmatched_item(self) -> None:
        need = [
            {
                "id": "unmatched",
                "name": "unmatched",
                "filename": "unmatched.mp4",
                "size": 8_000_000,
                "file_sig": "b2:8000000:missing",
                "duration": None,
            }
        ]
        with (
            mock.patch.object(media, "build_metadata_source_index", return_value={}),
            mock.patch(
                "vg.catalog_db.find_probe_donor",
                side_effect=AssertionError("batch lookup must not issue N+1 queries"),
            ) as donor,
        ):
            leftover, reused_n = media.adopt_metadata_from_catalog(
                need,
                want_duration=True,
                want_audio=False,
            )
        self.assertEqual(reused_n, 0)
        self.assertEqual(leftover, need)
        donor.assert_not_called()

    def test_background_enrichment_skips_ffprobe_when_all_reused(self) -> None:
        old_ffmpeg = STATE.get("ffmpeg")
        old_videos = STATE.get("videos")
        old_running = getattr(media._state, "_meta_running", False)
        STATE["ffmpeg"] = "ffmpeg"
        STATE["videos"] = [
            {
                "id": "todo",
                "rel": "todo.mp4",
                "name": "todo",
                "size": 5_000_000,
                "file_sig": "b2:5000000:zz",
                "duration": None,
            }
        ]
        media._state._meta_running = False
        try:
            with (
                mock.patch.object(media, "probe_duration_enabled", return_value=True),
                mock.patch.object(media, "probe_audio_enabled", return_value=False),
                mock.patch.object(
                    media,
                    "adopt_metadata_from_catalog",
                    return_value=([], 1),
                ),
                mock.patch.object(media, "enrich_metadata_parallel") as enrich,
                mock.patch("vg.catalog.rebuild_indexes") as rebuild,
            ):
                media._bg_enrich_metadata()
            enrich.assert_not_called()
            rebuild.assert_called_once()
            self.assertIn("复用", STATE.get("meta_progress") or "")
        finally:
            STATE["ffmpeg"] = old_ffmpeg
            STATE["videos"] = old_videos
            media._state._meta_running = old_running

    def test_enrich_metadata_uses_half_cpu_worker_count(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "v0.mp4"
            path.write_bytes(b"video")
            items = [
                {
                    "id": "v0",
                    "name": "v0",
                    "rel": path.name,
                    "root": td,
                    "_lib_root": td,
                }
            ]
            old_ffmpeg = STATE.get("ffmpeg")
            STATE["ffmpeg"] = "ffmpeg"
            try:
                with (
                    mock.patch.object(media, "probe_duration_enabled", return_value=True),
                    mock.patch.object(media, "probe_audio_enabled", return_value=False),
                    mock.patch.object(media, "_item_probe_path", return_value=path),
                    mock.patch.object(
                        media,
                        "probe_media_info",
                        return_value={"ok": True, "duration": 3.0},
                    ),
                    mock.patch.object(media, "_persist_probed_items"),
                    mock.patch.object(media, "meta_worker_count", return_value=4) as workers,
                ):
                    media.enrich_metadata_parallel(items, label="测试")
                workers.assert_called_once_with(1)
            finally:
                STATE["ffmpeg"] = old_ffmpeg

    def test_persist_probed_items_does_not_bump_lib_gen(self) -> None:
        item = {
            "id": "v1",
            "rel": "v1.mp4",
            "root": "D:/lib",
            "_lib_root": "D:/lib",
            "duration": 9.0,
        }
        with mock.patch("vg.disk_libs.save_library_items") as save:
            media._persist_probed_items([item])
        save.assert_called_once_with([item], bump_gen=False)

    def test_probe_progress_is_saved_incrementally(self) -> None:
        with TemporaryDirectory() as td:
            items = []
            for i in range(60):
                path = Path(td) / f"v{i}.mp4"
                path.write_bytes(b"video")
                items.append(
                    {
                        "id": f"v{i}",
                        "name": f"v{i}",
                        "rel": path.name,
                        "root": td,
                        "_lib_root": td,
                    }
                )

            saved_batches: list[list[str]] = []

            def fake_persist(batch):
                saved_batches.append([it["id"] for it in batch])

            old_ffmpeg = STATE.get("ffmpeg")
            STATE["ffmpeg"] = "ffmpeg"
            try:
                with (
                    mock.patch.object(media, "probe_duration_enabled", return_value=True),
                    mock.patch.object(media, "probe_audio_enabled", return_value=False),
                    mock.patch.object(media, "_item_probe_path", side_effect=lambda it: Path(td) / it["rel"]),
                    mock.patch.object(
                        media,
                        "probe_media_info",
                        return_value={"ok": True, "duration": 3.0},
                    ),
                    mock.patch.object(media, "_persist_probed_items", side_effect=fake_persist) as persist,
                    mock.patch.object(media, "meta_worker_count", return_value=1),
                ):
                    media.enrich_metadata_parallel(items, label="测试")
                self.assertGreaterEqual(persist.call_count, 2)
                self.assertEqual(sum(len(b) for b in saved_batches), 60)
                self.assertLess(max(len(b) for b in saved_batches), 60)
            finally:
                STATE["ffmpeg"] = old_ffmpeg

    def test_partial_probe_is_kept_when_later_persist_fails(self) -> None:
        with TemporaryDirectory() as td:
            items = []
            for i in range(30):
                path = Path(td) / f"v{i}.mp4"
                path.write_bytes(b"video")
                items.append(
                    {
                        "id": f"v{i}",
                        "name": f"v{i}",
                        "rel": path.name,
                        "root": td,
                        "_lib_root": td,
                    }
                )
            saved: list[str] = []

            def fake_persist(batch):
                saved.extend(it["id"] for it in batch)
                if len(saved) >= 25:
                    raise RuntimeError("disk full after first flush")

            old_ffmpeg = STATE.get("ffmpeg")
            STATE["ffmpeg"] = "ffmpeg"
            try:
                with (
                    mock.patch.object(media, "probe_duration_enabled", return_value=True),
                    mock.patch.object(media, "probe_audio_enabled", return_value=False),
                    mock.patch.object(media, "_item_probe_path", side_effect=lambda it: Path(td) / it["rel"]),
                    mock.patch.object(
                        media,
                        "probe_media_info",
                        return_value={"ok": True, "duration": 3.0},
                    ),
                    mock.patch.object(media, "_persist_probed_items", side_effect=fake_persist),
                    mock.patch.object(media, "meta_worker_count", return_value=1),
                ):
                    with self.assertRaises(RuntimeError):
                        media.enrich_metadata_parallel(items, label="测试")
                self.assertGreaterEqual(len(saved), 25)
            finally:
                STATE["ffmpeg"] = old_ffmpeg


if __name__ == "__main__":
    unittest.main()
