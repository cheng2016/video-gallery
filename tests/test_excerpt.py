# -*- coding: utf-8 -*-
"""GIF and clip excerpt helpers."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vg.convert import convert_kind_label, enqueue_convert_job, pump_convert_queue
from vg.excerpt import (
    GIF_FPS_CHOICES,
    GIF_WIDTH_CHOICES,
    clip_ffmpeg_tail,
    excerpt_output_stem,
    gif_ffmpeg_tail,
    normalize_excerpt_request,
    output_under_library,
    run_excerpt_job,
)
from vg.state import STATE

ROOT = Path(__file__).resolve().parents[1]
VID = "abcdef0123456789"


class ExcerptRequestTests(unittest.TestCase):
    def test_gif_range_and_limits(self) -> None:
        spec, err = normalize_excerpt_request("gif", 1.2, 4.7, duration=30, fps=10, width=480)
        self.assertEqual(err, "")
        self.assertEqual(spec["start"], 1.2)
        self.assertEqual(spec["end"], 4.7)
        self.assertEqual(spec["fps"], 10)
        self.assertEqual(spec["width"], 480)
        self.assertAlmostEqual(spec["span"], 3.5)

        spec, err = normalize_excerpt_request("gif", 0, 21, fps=10, width=320)
        self.assertIsNone(spec)
        self.assertIn("20", err)

        spec, err = normalize_excerpt_request("gif", 8, 12, duration=10, fps=15, width=240)
        self.assertEqual(spec["end"], 10.0)

        spec, err = normalize_excerpt_request("gif", 9.9, 12, duration=10, fps=8, width=640)
        self.assertIsNone(spec)
        self.assertIn("片段太短", err)

        spec, err = normalize_excerpt_request("gif", 9.97, 12, duration=10, fps=8, width=640)
        self.assertIsNone(spec)
        self.assertIn("片尾", err)

        spec, err = normalize_excerpt_request("gif", 1, 3, fps=7, width=480)
        self.assertIsNone(spec)
        self.assertIn("帧率", err)

        spec, err = normalize_excerpt_request("gif", 1, 3, fps=10, width=100)
        self.assertIsNone(spec)
        self.assertIn("宽度", err)

    def test_clip_range(self) -> None:
        spec, err = normalize_excerpt_request("clip", 0, 90, accurate=True)
        self.assertEqual(err, "")
        self.assertTrue(spec["accurate"])
        self.assertAlmostEqual(spec["span"], 90.0)

        spec, err = normalize_excerpt_request("clip", 5, 5, accurate=False)
        self.assertIsNone(spec)
        self.assertIn("终点", err)

        spec, err = normalize_excerpt_request("nope", 0, 1)
        self.assertIsNone(spec)

    def test_ffmpeg_args_keep_paths_as_their_own_tokens(self) -> None:
        src = Path(r"D:\lib\clip.mp4")
        gif_out = Path(r"D:\lib\clip_gif.gif")
        gif_tail = gif_ffmpeg_tail(src, gif_out, 1.5, 2.25, 12, 320)
        self.assertEqual(gif_tail[gif_tail.index("-i") + 1], str(src))
        self.assertEqual(gif_tail[-1], str(gif_out))
        self.assertIn("palettegen", gif_tail[gif_tail.index("-filter_complex") + 1])
        self.assertIn("scale=320:-2", gif_tail[gif_tail.index("-filter_complex") + 1])
        plain = gif_ffmpeg_tail(src, gif_out, 1.5, 2.25, 12, 320, palette=False)
        self.assertIn("-vf", plain)
        self.assertNotIn("-filter_complex", plain)

        clip_out = Path(r"D:\lib\clip_cut.mp4")
        fast = clip_ffmpeg_tail(src, clip_out, 12.5, 4, accurate=False)
        self.assertEqual(fast[fast.index("-c") + 1], "copy")
        self.assertLess(fast.index("-ss"), fast.index("-i"))

        precise = clip_ffmpeg_tail(src, clip_out, 12.5, 4, accurate=True)
        self.assertEqual(precise[precise.index("-ss") + 1], "10.500")
        self.assertEqual(precise[precise.index("-i") + 1], str(src))
        after_input = precise[precise.index("-i") + 2 :]
        self.assertEqual(after_input[after_input.index("-ss") + 1], "2.000")
        self.assertIn("libx264", precise)
        self.assertNotIn("copy", precise)

    def test_output_names_and_library_guard(self) -> None:
        self.assertEqual(
            excerpt_output_stem("花絮", "gif", 1.25, 3.5),
            "花絮_gif_1p2-3p5",
        )
        self.assertEqual(
            excerpt_output_stem("花絮", "clip", 0, 8),
            "花絮_clip_0p0-8p0",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inside = root / "a.gif"
            outside = root.parent / "b.gif"
            self.assertTrue(output_under_library(inside, [str(root)]))
            self.assertFalse(output_under_library(outside, [str(root)]))

    def test_player_toolbar_exposes_both_actions(self) -> None:
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        toolbar = html.split('id="modalActions"', 1)[1].split('id="convertPanel"', 1)[0]
        self.assertIn('id="btnGif"', toolbar)
        self.assertIn('id="btnClip"', toolbar)
        self.assertLess(toolbar.index('id="btnCover"'), toolbar.index('id="btnGif"'))
        self.assertLess(toolbar.index('id="btnGif"'), toolbar.index('id="btnClip"'))
        self.assertLess(toolbar.index('id="btnClip"'), toolbar.index('id="btnCopyPath"'))
        panel = html.split('id="excerptPanel"', 1)[1].split('id="coverPanel"', 1)[0]
        for fps in GIF_FPS_CHOICES:
            self.assertIn(f'value="{fps}"', panel)
        for width in GIF_WIDTH_CHOICES:
            self.assertIn(f'value="{width}"', panel)
        self.assertIn('id="btnStartGif"', panel)
        self.assertIn('id="btnStartClip"', panel)
        self.assertIn("/api/excerpt/", html)
        self.assertIn("function startExcerpt(", html)
        self.assertIn('kind === "gif"', html)
        self.assertIn('kind === "clip"', html)


class ExcerptQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self._jobs = STATE.get("convert_jobs")
        self._ffmpeg = STATE.get("ffmpeg")
        STATE["convert_jobs"] = {}

    def tearDown(self) -> None:
        STATE["convert_jobs"] = self._jobs if self._jobs is not None else {}
        STATE["ffmpeg"] = self._ffmpeg

    def test_duplicate_excerpt_reuses_the_queued_job(self) -> None:
        spec = {"start": 1, "end": 4, "span": 3, "fps": 10, "width": 480}
        with mock.patch("vg.convert.pump_convert_queue"):
            ok, _msg, first = enqueue_convert_job(VID, kind="gif", name="demo", excerpt=spec)
            ok2, msg2, second = enqueue_convert_job(VID, kind="gif", name="demo", excerpt=spec)
            other_ok, _msg3, other = enqueue_convert_job(
                VID,
                kind="gif",
                name="demo",
                excerpt={"start": 2, "end": 5, "fps": 10, "width": 480},
            )
        self.assertTrue(ok and ok2 and other_ok)
        self.assertEqual(first, second)
        self.assertIn("队列", msg2)
        self.assertNotEqual(first, other)
        self.assertEqual(STATE["convert_jobs"][first]["excerpt"]["fps"], 10)
        self.assertEqual(convert_kind_label({"kind": "gif"}), "制作GIF")
        self.assertEqual(convert_kind_label({"kind": "clip"}), "截取片段")

        missing_ok, missing_msg, missing_id = enqueue_convert_job(VID, kind="gif", name="demo")
        self.assertFalse(missing_ok)
        self.assertEqual(missing_id, "")
        self.assertIn("起止", missing_msg)

    def test_pump_dispatches_excerpt_worker(self) -> None:
        STATE["convert_jobs"] = {
            "jobexcerpt01": {
                "id": "jobexcerpt01",
                "vid": VID,
                "root": "",
                "kind": "clip",
                "name": "demo",
                "status": "queued",
                "msg": "",
                "percent": 0,
                "cancel": False,
                "created": 1,
                "excerpt": {"start": 0, "end": 2, "accurate": False},
            }
        }
        with mock.patch("vg.convert.threading.Thread") as thread_cls:
            pump_convert_queue()
        target = thread_cls.call_args.kwargs["args"][0]
        self.assertIs(target, run_excerpt_job)
        self.assertEqual(STATE["convert_jobs"]["jobexcerpt01"]["status"], "running")


class ExcerptWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._jobs = STATE.get("convert_jobs")
        self._ffmpeg = STATE.get("ffmpeg")
        STATE["ffmpeg"] = "ffmpeg"
        STATE["convert_jobs"] = {}

    def tearDown(self) -> None:
        STATE["convert_jobs"] = self._jobs if self._jobs is not None else {}
        STATE["ffmpeg"] = self._ffmpeg

    def _job(self, job_id: str, kind: str, excerpt: dict) -> None:
        STATE["convert_jobs"][job_id] = {
            "id": job_id,
            "vid": VID,
            "kind": kind,
            "status": "running",
            "msg": "",
            "percent": 0,
            "cancel": False,
            "out_path": "",
            "added_id": "",
            "proc": None,
            "excerpt": excerpt,
        }

    def test_gif_worker_writes_palette_command_and_skips_library(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "clip.mp4"
            src.write_bytes(b"x" * 300)
            self._job("gifjob000001", "gif", {"start": 1.25, "end": 3.5, "fps": 12, "width": 320})
            captured: dict = {}

            def fake_run(job_id, ffmpeg, attempts, out_path, duration_hint, log_tag="", src_ext=""):
                captured["attempts"] = attempts
                captured["duration"] = duration_hint
                captured["out"] = out_path
                out_path.write_bytes(b"GIF89a")
                return True, "ok"

            item = {
                "id": VID,
                "name": "clip",
                "filename": "clip.mp4",
                "rel": "clip.mp4",
                "kind": "file",
                "ext": ".mp4",
            }
            with mock.patch("vg.excerpt.find_video_by_id", return_value=item), \
                    mock.patch("vg.excerpt.resolve_item_rel", return_value=src), \
                    mock.patch("vg.excerpt.root_for_item", return_value=root), \
                    mock.patch("vg.excerpt.probe_duration", return_value=30.0), \
                    mock.patch("vg.excerpt._run_ffmpeg_attempts", side_effect=fake_run), \
                    mock.patch("vg.excerpt._register_converted_mp4") as register:
                run_excerpt_job("gifjob000001", VID, str(root))
            register.assert_not_called()
            job = STATE["convert_jobs"]["gifjob000001"]
            self.assertEqual(job["status"], "done")
            self.assertTrue(str(captured["out"]).endswith(".gif"))
            self.assertAlmostEqual(captured["duration"], 2.25)
            labels = [label for label, _cmd in captured["attempts"]]
            self.assertEqual(labels, ["制作 GIF", "制作 GIF"])
            palette = captured["attempts"][0][1]
            self.assertIn("palettegen", palette[palette.index("-filter_complex") + 1])
            self.assertEqual(palette[palette.index("-t") + 1], "2.250")

    def test_fast_clip_falls_back_to_reencode_and_joins_library(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "clip.mp4"
            src.write_bytes(b"x" * 300)
            self._job("clipjob00001", "clip", {"start": 0, "end": 2, "accurate": False})
            captured: dict = {}

            def fake_run(job_id, ffmpeg, attempts, out_path, duration_hint, log_tag="", src_ext=""):
                captured["attempts"] = attempts
                out_path.write_bytes(b"x" * 80)
                return True, "ok"

            item = {
                "id": VID,
                "name": "clip",
                "filename": "clip.mp4",
                "rel": "clip.mp4",
                "kind": "file",
                "ext": ".mp4",
            }
            with mock.patch("vg.excerpt.find_video_by_id", return_value=item), \
                    mock.patch("vg.excerpt.resolve_item_rel", return_value=src), \
                    mock.patch("vg.excerpt.root_for_item", return_value=root), \
                    mock.patch("vg.excerpt.probe_duration", return_value=20.0), \
                    mock.patch("vg.excerpt._run_ffmpeg_attempts", side_effect=fake_run), \
                    mock.patch("vg.excerpt._register_converted_mp4", return_value={"id": "newvid"}) as register:
                run_excerpt_job("clipjob00001", VID, str(root))
            self.assertEqual(register.call_args.kwargs.get("allow_small"), True)
            labels = [label for label, _cmd in captured["attempts"]]
            self.assertEqual(labels, ["快速剪切", "精确截取"])
            self.assertIn("-c", captured["attempts"][0][1])
            self.assertIn("libx264", captured["attempts"][1][1])
            job = STATE["convert_jobs"]["clipjob00001"]
            self.assertEqual(job["status"], "done")
            self.assertEqual(job["added_id"], "newvid")
            self.assertIn("片库", job["msg"])
            self.assertTrue(job["out_path"].endswith(".mp4"))

    def test_stream_source_is_rejected_before_ffmpeg(self) -> None:
        self._job("streamjob0001", "gif", {"start": 0, "end": 2, "fps": 10, "width": 480})
        item = {"id": VID, "kind": "m3u8", "ext": ".m3u8", "rel": "index.m3u8", "name": "show"}
        with mock.patch("vg.excerpt.find_video_by_id", return_value=item), \
                mock.patch("vg.excerpt._run_ffmpeg_attempts") as run:
            run_excerpt_job("streamjob0001", VID, "")
        run.assert_not_called()
        job = STATE["convert_jobs"]["streamjob0001"]
        self.assertEqual(job["status"], "error")
        self.assertIn("流媒体", job["msg"])


if __name__ == "__main__":
    unittest.main()
