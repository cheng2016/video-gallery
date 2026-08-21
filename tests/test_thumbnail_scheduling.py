from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import contextlib
import io
import unittest
from unittest import mock

from vg import media, scan, web
from vg.state import STATE
from vg.thumb_jobs import THUMB_PRIORITY_BATCH
from vg.util import thumb_worker_count


class ThumbnailSchedulingTests(unittest.TestCase):
    def test_worker_count_keeps_cpu_headroom_and_caps_background_work(self) -> None:
        with mock.patch("vg.util.os.cpu_count", return_value=64):
            self.assertEqual(thumb_worker_count(500), 4)
            # Burst leaves 2 cores free for waitress / UI thumb reads.
            self.assertEqual(thumb_worker_count(500, burst=True), 62)
        with mock.patch("vg.util.os.cpu_count", return_value=1):
            self.assertEqual(thumb_worker_count(500), 1)
            self.assertEqual(thumb_worker_count(500, burst=True), 1)
        with mock.patch("vg.util.os.cpu_count", return_value=64):
            self.assertEqual(thumb_worker_count(1), 1)
            self.assertEqual(thumb_worker_count(1, burst=True), 1)

    def test_meta_worker_count_matches_thumb_burst(self) -> None:
        from vg.util import meta_worker_count

        with mock.patch("vg.util.os.cpu_count", return_value=16):
            self.assertEqual(meta_worker_count(500), 14)
        with mock.patch("vg.util.os.cpu_count", return_value=2):
            self.assertEqual(meta_worker_count(500), 1)
        with mock.patch("vg.util.os.cpu_count", return_value=16):
            self.assertEqual(meta_worker_count(3), 3)

    def test_background_ffmpeg_is_single_thread_and_low_priority(self) -> None:
        with TemporaryDirectory() as td:
            base = Path(td)
            video = base / "video.mp4"
            out = base / "thumb.vgt"
            video.write_bytes(b"video")
            calls = []

            def fake_run(cmd, **kwargs):
                calls.append((cmd, kwargs))
                Path(cmd[-1]).write_bytes(b"\xff\xd8" + b"x" * 200)
                return SimpleNamespace(returncode=0)

            with (
                mock.patch.object(media.subprocess, "run", side_effect=fake_run),
                mock.patch.object(media.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True),
                mock.patch.object(
                    media.subprocess,
                    "BELOW_NORMAL_PRIORITY_CLASS",
                    0x00004000,
                    create=True,
                ),
                mock.patch.object(media.sys, "platform", "win32"),
                mock.patch.object(media, "_clear_path_attrs_windows"),
                mock.patch.object(media, "pack_thumb_bytes", side_effect=lambda raw: raw),
            ):
                self.assertTrue(media.make_thumbnail("ffmpeg", video, out, background=True))

            cmd, kwargs = calls[0]
            self.assertEqual(cmd.count("-threads"), 2)
            self.assertIn("-nostdin", cmd)
            self.assertIn("-an", cmd)
            self.assertEqual(kwargs["timeout"], 25)
            self.assertTrue(kwargs["creationflags"] & 0x00004000)

    def test_burst_ffmpeg_keeps_single_thread_and_normal_priority(self) -> None:
        with TemporaryDirectory() as td:
            base = Path(td)
            video = base / "video.mp4"
            out = base / "thumb.vgt"
            video.write_bytes(b"video")
            calls = []

            def fake_run(cmd, **kwargs):
                calls.append((cmd, kwargs))
                Path(cmd[-1]).write_bytes(b"\xff\xd8" + b"x" * 200)
                return SimpleNamespace(returncode=0)

            with (
                mock.patch.object(media.subprocess, "run", side_effect=fake_run),
                mock.patch.object(media.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True),
                mock.patch.object(
                    media.subprocess,
                    "BELOW_NORMAL_PRIORITY_CLASS",
                    0x00004000,
                    create=True,
                ),
                mock.patch.object(media.sys, "platform", "win32"),
                mock.patch.object(media, "_clear_path_attrs_windows"),
                mock.patch.object(media, "pack_thumb_bytes", side_effect=lambda raw: raw),
            ):
                self.assertTrue(
                    media.make_thumbnail("ffmpeg", video, out, background=True, burst=True)
                )

            cmd, kwargs = calls[0]
            self.assertEqual(cmd.count("-threads"), 2)
            self.assertFalse(kwargs["creationflags"] & 0x00004000)

    def test_thumbnail_failure_logs_ffmpeg_reason_and_seek_attempts(self) -> None:
        with TemporaryDirectory() as td:
            base = Path(td)
            video = base / "broken.mp4"
            out = base / "thumb.vgt"
            video.write_bytes(b"video")
            failed = SimpleNamespace(
                returncode=1,
                stderr=b"Invalid data found when processing input",
            )
            output = io.StringIO()
            with (
                contextlib.redirect_stdout(output),
                mock.patch("vg.bootlog.write"),
                mock.patch.object(media.subprocess, "run", return_value=failed),
                mock.patch.object(media, "_clear_path_attrs_windows"),
            ):
                self.assertFalse(media.make_thumbnail("ffmpeg", video, out))
            text = output.getvalue()
            self.assertIn("thumbnail_generation_failed", text)
            self.assertIn("Invalid data found", text)
            self.assertIn("seek=3.0", text)

    def test_audio_only_source_stops_retrying_seek_positions(self) -> None:
        with TemporaryDirectory() as td:
            base = Path(td)
            video = base / "audio-only.mp4"
            out = base / "thumb.vgt"
            video.write_bytes(b"audio")
            failed = SimpleNamespace(
                returncode=1,
                stderr=b"Output file does not contain any stream",
            )
            with (
                mock.patch.object(media.subprocess, "run", return_value=failed) as run,
                mock.patch.object(media, "_clear_path_attrs_windows"),
                mock.patch("vg.diagnostics.emit") as emit,
            ):
                self.assertFalse(media.make_thumbnail("ffmpeg", video, out, background=True))
            self.assertEqual(run.call_count, 1)
            events = [call.args[1] for call in emit.call_args_list if len(call.args) > 1]
            self.assertIn("thumbnail_source_no_video_stream", events)
            self.assertNotIn("thumbnail_generation_failed", events)

    def test_deferred_thumb_request_queues_without_running_ffmpeg_inline(self) -> None:
        vid = "a" * 16
        with TemporaryDirectory() as td:
            base = Path(td)
            video = base / "video.mp4"
            video.write_bytes(b"video")
            item = {"id": vid, "name": "video", "rel": "video.mp4", "root": str(base)}
            pending = Future()
            old_ffmpeg = STATE.get("ffmpeg")
            STATE["ffmpeg"] = "ffmpeg"
            try:
                with (
                    mock.patch.object(web, "find_video_by_id", return_value=item),
                    mock.patch.object(web, "cache_dir_for_item", return_value=base),
                    mock.patch.object(web, "_cache_dir_from_root_hint", return_value=base),
                    mock.patch.object(web, "read_thumb_jpeg", return_value=None),
                    mock.patch.object(web, "_video_file_for_thumb", return_value=video),
                    mock.patch.object(web, "submit_thumbnail_job", return_value=pending) as submit,
                    mock.patch.object(web, "make_thumbnail") as make,
                ):
                    response = web.app.test_client().get(
                        f"/thumb/{vid}?defer=1&root={base.as_posix()}"
                    )
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.headers.get("Retry-After"), "1")
                submit.assert_called_once()
                make.assert_not_called()
            finally:
                STATE["ffmpeg"] = old_ffmpeg

    def test_existing_thumb_serves_from_root_hint_without_catalog_lookup(self) -> None:
        vid = "c" * 16
        jpeg = b"\xff\xd8" + b"x" * 120
        with TemporaryDirectory() as td:
            cache = Path(td)
            with (
                mock.patch.object(web, "_cache_dir_from_root_hint", return_value=cache) as hint,
                mock.patch.object(web, "read_thumb_jpeg", return_value=jpeg) as read,
                mock.patch.object(web, "find_video_by_id") as lookup,
            ):
                response = web.app.test_client().get(
                    f"/thumb/{vid}?defer=1&root=E:/Movies"
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "image/jpeg")
            self.assertEqual(response.data, jpeg)
            hint.assert_called()
            read.assert_called()
            lookup.assert_not_called()

    def test_batch_generation_uses_shared_low_priority_queue(self) -> None:
        with TemporaryDirectory() as td:
            base = Path(td)
            item = {"id": "b" * 16, "name": "video", "rel": "video.mp4"}
            video = base / "video.mp4"
            video.write_bytes(b"video")
            old_ffmpeg = STATE.get("ffmpeg")
            old_cache = STATE.get("cache_dir")
            STATE["ffmpeg"] = "ffmpeg"
            STATE["cache_dir"] = base
            submitted = []

            def immediate_submit(key, work, *, priority):
                submitted.append((key, priority))
                future = Future()
                future.set_result(work())
                return future

            try:
                with (
                    mock.patch.object(scan, "submit_thumbnail_job", side_effect=immediate_submit),
                    mock.patch.object(scan, "ensure_thumbnail_workers"),
                    mock.patch.object(scan, "_video_file_for_thumb", return_value=video),
                    mock.patch.object(scan, "make_thumbnail", return_value=True) as make,
                    mock.patch.object(scan, "thumb_version", return_value=1),
                ):
                    self.assertEqual(scan.generate_thumbs_parallel([item], burst=True), (1, 0))
                self.assertEqual(submitted[0][1], THUMB_PRIORITY_BATCH)
                self.assertTrue(make.call_args.kwargs["background"])
                self.assertTrue(make.call_args.kwargs["burst"])
            finally:
                STATE["ffmpeg"] = old_ffmpeg
                STATE["cache_dir"] = old_cache

    def test_bulk_thumbnail_miss_logs_cache_evidence(self) -> None:
        videos = [{"id": f"{i:016x}"} for i in range(60)]
        old_ffmpeg = STATE.get("ffmpeg")
        STATE["ffmpeg"] = "ffmpeg"
        try:
            with TemporaryDirectory() as td:
                cache = Path(td)
                with (
                    mock.patch.object(scan, "missing_thumb_items", return_value=videos),
                    mock.patch.object(
                        scan,
                        "adopt_thumbs_from_caches",
                        return_value=([], len(videos)),
                    ),
                    mock.patch.object(scan, "list_thumb_ids", return_value={"cached"}),
                    mock.patch("vg.diagnostics.emit") as emit,
                ):
                    scan.fill_thumbs_for_videos(
                        videos,
                        burst=False,
                        cache=cache,
                        root=None,
                    )
            bulk = next(
                call
                for call in emit.call_args_list
                if len(call.args) > 1 and call.args[1] == "thumbnail_cache_bulk_miss"
            )
            self.assertEqual(bulk.kwargs["videos"], 60)
            self.assertEqual(bulk.kwargs["missing_before_reuse"], 60)
            self.assertEqual(bulk.kwargs["cache_vgt_files"], 1)
            self.assertEqual(bulk.kwargs["missing_after_reuse"], 0)
        finally:
            STATE["ffmpeg"] = old_ffmpeg

    def test_thumbnail_catalog_save_preserves_metadata_written_after_scan(self) -> None:
        from vg.cache import save_index
        from vg.scan import _preserve_catalog_probe_fields_for_thumb_save

        with TemporaryDirectory() as td:
            root = Path(td)
            cache = root / "cache"
            item = {
                "id": "same-id",
                "rel": "movie.mp4",
                "size": 200_000,
                "mtime": 100.0,
                "file_sig": "sig-1",
                "duration": 12.5,
                "duration_h": "00:00:12",
                "audio_codec": "aac",
                "probe_ver": 2,
                "probe_duration_done": True,
                "probe_audio_done": True,
            }
            self.assertTrue(save_index(cache, root, [item], file_count=1, folder_counts={"": 1}))
            stale = dict(item)
            for key in (
                "duration", "duration_h", "audio_codec", "probe_ver",
                "probe_duration_done", "probe_audio_done",
            ):
                stale.pop(key, None)
            self.assertEqual(
                _preserve_catalog_probe_fields_for_thumb_save(
                    [stale], cache=cache, root=root
                ),
                1,
            )
            self.assertEqual(stale["duration"], 12.5)
            self.assertEqual(stale["audio_codec"], "aac")
            self.assertTrue(stale["probe_duration_done"])


if __name__ == "__main__":
    unittest.main()
