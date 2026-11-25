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
            # Burst follows logical CPUs minus 2. A 200-file batch was fastest there.
            self.assertEqual(thumb_worker_count(500, burst=True), 62)
        with mock.patch("vg.util.os.cpu_count", return_value=1):
            self.assertEqual(thumb_worker_count(500), 1)
            self.assertEqual(thumb_worker_count(500, burst=True), 1)
        with mock.patch("vg.util.os.cpu_count", return_value=64):
            self.assertEqual(thumb_worker_count(1), 1)
            self.assertEqual(thumb_worker_count(1, burst=True), 1)

    def test_meta_worker_count_reserves_cpu_for_scanning_and_thumbnails(self) -> None:
        from vg.util import meta_worker_count

        with mock.patch("vg.util.os.cpu_count", return_value=16):
            self.assertEqual(meta_worker_count(500), 4)
        with mock.patch("vg.util.os.cpu_count", return_value=2):
            self.assertEqual(meta_worker_count(500), 1)
        with mock.patch("vg.util.os.cpu_count", return_value=16):
            self.assertEqual(meta_worker_count(3), 3)
        with mock.patch("vg.util.os.cpu_count", return_value=12):
            self.assertEqual(meta_worker_count(500), 3)
        with mock.patch("vg.util.os.cpu_count", return_value=64):
            self.assertEqual(meta_worker_count(500), 4)

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
                    mock.patch.object(scan, "make_item_thumbnail", return_value=True) as make,
                ):
                    self.assertEqual(scan.generate_thumbs_parallel([item], burst=True), (1, 0))
                self.assertEqual(submitted[0][1], THUMB_PRIORITY_BATCH)
                self.assertEqual(make.call_args.args[0], item)
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
                    mock.patch.object(scan, "media_job_snapshot", side_effect=lambda item, _path: dict(item)),
                    mock.patch.object(
                        scan,
                        "adopt_thumbs_from_caches",
                        return_value=([], len(videos)),
                    ),
                    mock.patch.object(scan, "list_thumb_ids", return_value={"cached"}),
                    mock.patch("vg.diagnostics.emit") as emit,
                ):
                    scan._fill_thumbs_background(
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

    def test_thumbnail_stage_summary_separates_prework_from_generation(self) -> None:
        item = {"id": "stage-id", "rel": "movie.mp4"}
        with TemporaryDirectory() as td, mock.patch.dict(STATE, {"ffmpeg": "ffmpeg"}):
            cache = Path(td)
            with (
                mock.patch.object(scan, "missing_thumb_items", return_value=[item]),
                mock.patch.object(scan, "media_job_snapshot", return_value=dict(item)),
                mock.patch.object(scan, "adopt_thumbs_from_caches", return_value=([item], 0)),
                mock.patch.object(scan, "generate_thumbs_parallel", return_value=(1, 0)),
                mock.patch("vg.diagnostics.perf") as perf,
            ):
                self.assertEqual(scan._fill_thumbs_background([item], burst=False, cache=cache, root=None), (1, 0))
            summary = next(call for call in perf.call_args_list if call.args[0] == "fill_thumbs_for_videos")
            stages = ["snapshot_ms", "missing_scan_ms", "source_stat_ms", "reuse_ms", "generate_ms", "patch_ms", "other_ms"]
            for stage in stages:
                self.assertGreaterEqual(summary.kwargs[stage], 0)
            self.assertAlmostEqual(sum(summary.kwargs[stage] for stage in stages), summary.args[1], delta=0.5)

    def test_thumbnail_source_index_logs_collection_and_registration_stages(self) -> None:
        from vg import thumbs
        with TemporaryDirectory() as td:
            cache = Path(td)
            item = {"id": "source-id", "file_sig": "source-sig", "has_thumb": True}
            path = cache / "source.vgt"
            path.write_bytes(b"x" * 40)
            with (
                mock.patch.object(thumbs, "_iter_memory_videos", return_value=[item]),
                mock.patch.object(thumbs, "_iter_preview_cache_index_videos", return_value=[(item, cache)]),
                mock.patch.object(thumbs, "_cache_for_item", return_value=cache),
                mock.patch.object(thumbs, "thumb_path", return_value=path),
                mock.patch("vg.diagnostics.perf") as perf,
            ):
                self.assertEqual(thumbs.build_thumb_source_index(), {"sig:source-sig": path})
            summary = perf.call_args
            self.assertEqual(summary.args[0], "thumbnail_source_index")
            self.assertEqual(summary.kwargs["checked_paths"], 2)
            self.assertEqual(summary.kwargs["ready_paths"], 2)
            self.assertEqual(summary.kwargs["candidate_rows"], 2)
            self.assertEqual(summary.kwargs["filtered_rows"], 0)
            self.assertEqual(summary.kwargs["path_cache_hits"], 1)
            self.assertEqual(summary.kwargs["stat_calls"], 1)
            self.assertEqual(summary.kwargs["is_file_calls"], 0)
            self.assertEqual(summary.kwargs["size_stat_calls"], 1)
            self.assertEqual(summary.kwargs["cache_resolve_calls"], 1)
            for stage in ["memory_collect_ms", "memory_register_ms", "catalog_load_ms", "disk_register_ms"]:
                self.assertGreaterEqual(summary.kwargs[stage], 0)

    def test_source_index_unwanted_rows_do_not_resolve_or_stat(self) -> None:
        from vg import thumbs

        with TemporaryDirectory() as td:
            cache = Path(td)
            path = cache / "source.vgt"
            path.write_bytes(b"x" * 40)
            item = {"id": "source", "file_sig": "unwanted", "has_thumb": True}
            for wanted in ({"sig:missing"}, set()):
                with (
                    self.subTest(wanted=wanted),
                    mock.patch.object(thumbs, "_iter_memory_videos", return_value=[item]),
                    mock.patch.object(thumbs, "_iter_preview_cache_index_videos", return_value=[(item, cache)]),
                    mock.patch.object(thumbs, "_cache_for_item", side_effect=AssertionError("unwanted cache resolution")) as resolve,
                    mock.patch.object(thumbs, "thumb_path", side_effect=AssertionError("unwanted thumbnail path")) as thumb_path,
                    mock.patch.object(Path, "stat", side_effect=AssertionError("unwanted stat")) as source_stat,
                    mock.patch("vg.diagnostics.perf") as perf,
                ):
                    self.assertEqual(thumbs.build_thumb_source_index(wanted_keys=wanted), {})
                resolve.assert_not_called()
                thumb_path.assert_not_called()
                source_stat.assert_not_called()
                self.assertEqual(perf.call_args.kwargs["candidate_rows"], 0)
                self.assertEqual(perf.call_args.kwargs["filtered_rows"], 2 if wanted else 1)
                self.assertEqual(perf.call_args.kwargs["catalog_lookup_skipped"], not wanted)
                self.assertEqual(perf.call_args.kwargs["stat_calls"], 0)

    def test_source_index_complete_memory_hits_skip_catalog_reads(self) -> None:
        from vg import thumbs

        with TemporaryDirectory() as td:
            cache = Path(td)
            item = {"id": "source", "file_sig": "source-sig", "has_thumb": True}
            path = cache / "source.vgt"
            path.write_bytes(b"x" * 40)
            with (
                mock.patch.object(thumbs, "_iter_memory_videos", return_value=[item]),
                mock.patch.object(thumbs, "_iter_preview_cache_index_videos", side_effect=AssertionError("redundant catalog read")) as catalogs,
                mock.patch.object(thumbs, "_cache_for_item", return_value=cache),
                mock.patch.object(thumbs, "thumb_path", return_value=path),
                mock.patch("vg.diagnostics.perf") as perf,
            ):
                self.assertEqual(thumbs.build_thumb_source_index({"sig:source-sig"}), {"sig:source-sig": path})
            catalogs.assert_not_called()
            self.assertTrue(perf.call_args.kwargs["catalog_lookup_skipped"])
            self.assertEqual(perf.call_args.kwargs["catalog_rows"], 0)
            self.assertEqual(perf.call_args.kwargs["stat_calls"], 1)

    def test_source_index_wanted_preserves_memory_then_disk_priority(self) -> None:
        from vg import thumbs

        with TemporaryDirectory() as td:
            base = Path(td)
            memory_cache = base / "memory"
            disk_cache = base / "disk"
            memory_cache.mkdir()
            disk_cache.mkdir()
            memory = [{"id": f"memory-{i}", "file_sig": "shared"} for i in range(2)]
            disk = [{"id": f"disk-{i}", "file_sig": "shared"} for i in range(2)]
            for cache, videos in ((memory_cache, memory), (disk_cache, disk)):
                for item in videos:
                    (cache / f"{item['id']}.vgt").write_bytes(b"x" * 40)
            for memory_rows, winner in ((memory, memory_cache / "memory-0.vgt"), ([], disk_cache / "disk-0.vgt")):
                with (
                    self.subTest(memory=bool(memory_rows)),
                    mock.patch.object(thumbs, "_iter_memory_videos", return_value=memory_rows),
                    mock.patch.object(thumbs, "_iter_preview_cache_index_videos", return_value=[(item, disk_cache) for item in disk]),
                    mock.patch.object(thumbs, "_cache_for_item", return_value=memory_cache) as resolve,
                    mock.patch.object(thumbs, "thumb_path", side_effect=lambda cache, vid: cache / f"{vid}.vgt"),
                    mock.patch.object(Path, "is_file", side_effect=AssertionError("use a single stat")),
                    mock.patch.object(Path, "stat", autospec=True, side_effect=Path.stat) as source_stat,
                    mock.patch("vg.diagnostics.perf"),
                ):
                    self.assertEqual(thumbs.build_thumb_source_index({"sig:shared"}), {"sig:shared": winner})
                self.assertEqual(resolve.call_count, len(memory_rows))
                self.assertEqual(source_stat.call_count, len(memory_rows) if memory_rows else len(disk))

    def test_source_index_shared_path_registers_each_wanted_key_with_one_stat(self) -> None:
        from vg import thumbs

        with TemporaryDirectory() as td:
            cache = Path(td)
            path = cache / "shared.vgt"
            path.write_bytes(b"x" * 40)
            memory = {"id": "shared", "file_sig": "memory-sig", "name": "shared.mp4", "size": thumbs.MIN_VIDEO_FILE_BYTES}
            disk = dict(memory, file_sig="disk-sig")
            wanted = {"sig:memory-sig", "sig:disk-sig"}
            with (
                mock.patch.object(thumbs, "_iter_memory_videos", return_value=[memory]),
                mock.patch.object(thumbs, "_iter_preview_cache_index_videos", return_value=[(disk, cache)]),
                mock.patch.object(thumbs, "_cache_for_item", return_value=cache),
                mock.patch.object(thumbs, "thumb_path", return_value=path),
                mock.patch.object(Path, "is_file", side_effect=AssertionError("use a single stat")),
                mock.patch.object(Path, "stat", autospec=True, side_effect=Path.stat) as source_stat,
                mock.patch("vg.diagnostics.perf") as perf,
            ):
                self.assertEqual(thumbs.build_thumb_source_index(wanted), {key: path for key in wanted})
            source_stat.assert_called_once_with(path)
            self.assertEqual(perf.call_args.kwargs["checked_paths"], 2)
            self.assertEqual(perf.call_args.kwargs["ready_paths"], 2)
            self.assertEqual(perf.call_args.kwargs["path_cache_hits"], 1)
            self.assertEqual(perf.call_args.kwargs["stat_calls"], 1)

    def test_source_index_has_thumb_false_skips_existing_stale_file(self) -> None:
        from vg import thumbs

        with TemporaryDirectory() as td:
            cache = Path(td)
            (cache / "stale.vgt").write_bytes(b"x" * 40)
            item = {"id": "stale", "file_sig": "stale-sig", "has_thumb": False}
            for wanted in (None, {"sig:stale-sig"}):
                with (
                    self.subTest(wanted=wanted),
                    mock.patch.object(thumbs, "_iter_memory_videos", return_value=[item]),
                    mock.patch.object(thumbs, "_iter_preview_cache_index_videos", return_value=[(item, cache)]),
                    mock.patch.object(thumbs, "_cache_for_item", side_effect=AssertionError("stale cache resolution")) as resolve,
                    mock.patch.object(Path, "stat", side_effect=AssertionError("stale stat")) as source_stat,
                    mock.patch("vg.diagnostics.perf") as perf,
                ):
                    self.assertEqual(thumbs.build_thumb_source_index(wanted), {})
                resolve.assert_not_called()
                source_stat.assert_not_called()
                self.assertEqual(perf.call_args.kwargs["checked_paths"], 0)
                self.assertEqual(perf.call_args.kwargs["stat_calls"], 0)

    def test_source_index_without_wanted_builds_all_content_keys(self) -> None:
        from vg import thumbs

        with TemporaryDirectory() as td:
            cache = Path(td)
            memory = {"id": "memory", "file_sig": "memory-sig", "name": "memory.mp4", "size": thumbs.MIN_VIDEO_FILE_BYTES}
            disk = {"id": "disk", "file_sig": "disk-sig", "name": "disk.mp4", "size": thumbs.MIN_VIDEO_FILE_BYTES}
            expected = {}
            for item in (memory, disk):
                path = cache / f"{item['id']}.vgt"
                path.write_bytes(b"x" * 40)
                keys = thumbs.thumb_content_keys(item)
                self.assertEqual(len(keys), 2)
                expected.update({key: path for key in keys})
            with (
                mock.patch.object(thumbs, "_iter_memory_videos", return_value=[memory]),
                mock.patch.object(thumbs, "_iter_preview_cache_index_videos", return_value=[(disk, cache)]),
                mock.patch.object(thumbs, "_cache_for_item", return_value=cache),
                mock.patch.object(thumbs, "thumb_path", side_effect=lambda cache, vid: cache / f"{vid}.vgt"),
                mock.patch.object(Path, "is_file", side_effect=AssertionError("use a single stat")),
                mock.patch.object(Path, "stat", autospec=True, side_effect=Path.stat) as source_stat,
                mock.patch("vg.diagnostics.perf") as perf,
            ):
                self.assertEqual(thumbs.build_thumb_source_index(), expected)
            self.assertEqual(source_stat.call_count, 2)
            self.assertEqual(perf.call_args.kwargs["candidate_rows"], 2)
            self.assertEqual(perf.call_args.kwargs["filtered_rows"], 0)

    def test_source_index_stat_memo_is_build_local(self) -> None:
        from vg import thumbs

        with TemporaryDirectory() as td:
            cache = Path(td)
            path = cache / "source.vgt"
            path.write_bytes(b"x" * 24)
            item = {"id": "source", "file_sig": "source-sig"}
            with (
                mock.patch.object(thumbs, "_iter_memory_videos", return_value=[item]),
                mock.patch.object(thumbs, "_iter_preview_cache_index_videos", return_value=[(item, cache)]),
                mock.patch.object(thumbs, "_cache_for_item", return_value=cache),
                mock.patch.object(thumbs, "thumb_path", return_value=path),
                mock.patch.object(Path, "stat", autospec=True, side_effect=Path.stat) as source_stat,
                mock.patch("vg.diagnostics.perf") as perf,
            ):
                self.assertEqual(thumbs.build_thumb_source_index({"sig:source-sig"}), {})
                path.write_bytes(b"x" * 40)
                self.assertEqual(thumbs.build_thumb_source_index({"sig:source-sig"}), {"sig:source-sig": path})
            self.assertEqual(source_stat.call_count, 2)
            for summary in perf.call_args_list:
                self.assertEqual(summary.kwargs["stat_calls"], 1)
            self.assertEqual(perf.call_args_list[0].kwargs["path_cache_hits"], 1)
            self.assertEqual(perf.call_args_list[1].kwargs["path_cache_hits"], 0)
            self.assertEqual(perf.call_args_list[0].kwargs["ready_paths"], 0)
            self.assertEqual(perf.call_args_list[1].kwargs["ready_paths"], 1)

    def test_source_index_stat_misses_and_failures_keep_diagnostics(self) -> None:
        from vg import thumbs

        with TemporaryDirectory() as td:
            cache = Path(td)
            (cache / "small.vgt").write_bytes(b"x" * 24)
            (cache / "directory.vgt").mkdir()
            denied = cache / "denied.vgt"
            denied.write_bytes(b"x" * 40)
            items = [{"id": vid, "file_sig": vid} for vid in ("small", "directory", "missing", "denied")]
            real_stat = Path.stat
            failure = PermissionError("thumbnail stat denied")

            def stat_source(path, *args, **kwargs):
                if path == denied:
                    raise failure
                return real_stat(path, *args, **kwargs)

            with (
                mock.patch.object(thumbs, "_iter_memory_videos", return_value=items),
                mock.patch.object(thumbs, "_iter_preview_cache_index_videos", return_value=[(item, cache) for item in items]),
                mock.patch.object(thumbs, "_cache_for_item", return_value=cache),
                mock.patch.object(thumbs, "thumb_path", side_effect=lambda cache, vid: cache / f"{vid}.vgt"),
                mock.patch.object(Path, "is_file", side_effect=AssertionError("use a single stat")),
                mock.patch.object(Path, "stat", autospec=True, side_effect=stat_source) as source_stat,
                mock.patch("vg.diagnostics.error") as error,
                mock.patch("vg.diagnostics.perf") as perf,
            ):
                self.assertEqual(thumbs.build_thumb_source_index({f"sig:{item['id']}" for item in items}), {})
            self.assertEqual(source_stat.call_count, 4)
            error.assert_called_once_with("thumbnail_source_stat_failed", failure, cache=cache, video_id="denied")
            self.assertEqual(perf.call_args.kwargs["stat_errors"], 1)
            self.assertEqual(perf.call_args.kwargs["path_cache_hits"], 4)
            self.assertEqual(perf.call_args.kwargs["checked_paths"], 8)
            self.assertTrue(perf.call_args.kwargs["force"])

    def test_adopt_thumbs_uses_missing_keys_and_reuses_real_thumbnail(self) -> None:
        from vg import thumbs

        with TemporaryDirectory() as td:
            base = Path(td)
            source_cache = base / "source"
            target_cache = base / "target"
            source_cache.mkdir()
            target_cache.mkdir()
            source = source_cache / "source.vgt"
            source.write_bytes(b"x" * 40)
            (source_cache / "unwanted.vgt").write_bytes(b"y" * 40)
            wanted = {"id": "source", "file_sig": "wanted"}
            unwanted = {"id": "unwanted", "file_sig": "unwanted"}
            missing = [{"id": "target", "file_sig": "wanted", "has_thumb": False}, {"id": "absent", "file_sig": "absent"}]

            def resolve(item):
                self.assertIs(item, wanted)
                return source_cache

            with (
                mock.patch.object(thumbs, "_iter_memory_videos", return_value=[unwanted, wanted]),
                mock.patch.object(thumbs, "_iter_preview_cache_index_videos", return_value=[(unwanted, source_cache), (wanted, source_cache)]),
                mock.patch.object(thumbs, "_cache_for_item", side_effect=resolve) as cache_for_item,
                mock.patch.object(thumbs, "thumb_path", side_effect=lambda cache, vid: cache / f"{vid}.vgt"),
                mock.patch.object(thumbs, "thumb_file_ready", return_value=False),
                mock.patch.object(thumbs, "build_thumb_source_index", wraps=thumbs.build_thumb_source_index) as build,
                mock.patch.object(thumbs, "log"),
                mock.patch("vg.diagnostics.perf") as perf,
            ):
                self.assertEqual(thumbs.adopt_thumbs_from_caches(missing, target_cache), ([missing[1]], 1))
            build.assert_called_once_with(wanted_keys={"sig:wanted", "sig:absent"})
            cache_for_item.assert_called_once_with(wanted)
            self.assertEqual((target_cache / "target.vgt").read_bytes(), source.read_bytes())
            self.assertTrue(missing[0]["has_thumb"])
            self.assertEqual(missing[0]["thumb"], "target.vgt")
            self.assertNotIn("has_thumb", missing[1])
            summary = next(call for call in perf.call_args_list if call.args[0] == "thumbnail_source_index")
            self.assertEqual(summary.kwargs["candidate_rows"], 2)
            self.assertEqual(summary.kwargs["filtered_rows"], 2)
            self.assertEqual(summary.kwargs["stat_calls"], 1)

    def test_thumbnail_catalog_save_preserves_metadata_written_after_scan(self) -> None:
        from vg.cache import save_index
        from vg.catalog_db import load_catalog_videos

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
            self.assertTrue(save_index(cache, root, [stale], file_count=1, folder_counts={"": 1}))
            saved = load_catalog_videos(cache, root)[0]
            self.assertEqual(saved["duration"], 12.5)
            self.assertEqual(saved["audio_codec"], "aac")
            self.assertTrue(saved["probe_duration_done"])


if __name__ == "__main__":
    unittest.main()
