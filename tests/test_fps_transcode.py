# -*- coding: utf-8 -*-
"""Frame-rate transcode helpers and player-only video-meta probe."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from vg.media import (
    classify_high_fps,
    fps_can_halve_to_30,
    fps_gate_reason,
    fps_target_choices,
    is_2k_or_4k,
    normalize_target_fps,
    parse_frame_rate,
    probe_media_info,
)


ROOT = Path(__file__).resolve().parents[1]


class FpsHelpersTests(unittest.TestCase):
    def test_parse_frame_rate(self) -> None:
        self.assertEqual(parse_frame_rate("60/1"), 60.0)
        self.assertAlmostEqual(parse_frame_rate("30000/1001") or 0, 29.97, places=2)
        self.assertIsNone(parse_frame_rate("0/0"))
        self.assertIsNone(parse_frame_rate(""))

    def test_hires_and_fps_gates(self) -> None:
        self.assertTrue(is_2k_or_4k(3840, 2160))
        self.assertTrue(is_2k_or_4k(2560, 1440))
        self.assertFalse(is_2k_or_4k(1920, 1080))
        self.assertTrue(fps_can_halve_to_30(60))
        self.assertTrue(fps_can_halve_to_30(59.94))
        self.assertTrue(fps_can_halve_to_30(90))
        self.assertTrue(fps_can_halve_to_30(120))
        self.assertTrue(fps_can_halve_to_30(119.88))
        self.assertFalse(fps_can_halve_to_30(48))
        self.assertFalse(fps_can_halve_to_30(30))
        self.assertFalse(fps_can_halve_to_30(24))
        self.assertEqual(classify_high_fps(59.94), 60)
        self.assertEqual(classify_high_fps(90), 90)
        self.assertEqual(classify_high_fps(120), 120)
        self.assertIsNone(classify_high_fps(48))
        self.assertEqual(fps_target_choices(60), [15, 20, 24, 25, 30, 48, 50])
        self.assertEqual(fps_target_choices(90), [15, 20, 24, 25, 30, 48, 50, 60, 72])
        self.assertEqual(fps_target_choices(120), [15, 20, 24, 25, 30, 48, 50, 60, 72, 90, 100])
        self.assertEqual(normalize_target_fps(60, None), 30)
        self.assertEqual(normalize_target_fps(60, 30), 30)
        self.assertEqual(normalize_target_fps(120, 60), 60)
        self.assertEqual(normalize_target_fps(60, 20), 20)
        self.assertIsNone(normalize_target_fps(60, 60))
        self.assertIsNone(normalize_target_fps(60, 90))

    def test_scale_choices_omit_already_smaller(self) -> None:
        from vg.media import scale_choices

        self.assertEqual(scale_choices(3840, 2160), [1080, 720])
        self.assertEqual(scale_choices(1920, 1080), [720])
        self.assertEqual(scale_choices(1280, 720), [])

    def test_gate_prefers_fps_too_low_over_no_ffmpeg(self) -> None:
        # 4K@30 is already the target rate — don't blame missing ffmpeg first.
        self.assertEqual(
            fps_gate_reason(width=3840, height=2160, fps=30, has_ffmpeg=False),
            "fps_too_low",
        )
        self.assertEqual(
            fps_gate_reason(width=3840, height=2160, fps=60, has_ffmpeg=False),
            "no_ffmpeg",
        )
        self.assertEqual(
            fps_gate_reason(width=3840, height=2160, fps=60, has_ffmpeg=True),
            "ok",
        )
        self.assertEqual(
            fps_gate_reason(width=3840, height=2160, fps=90, has_ffmpeg=True),
            "ok",
        )
        self.assertEqual(
            fps_gate_reason(width=3840, height=2160, fps=120, has_ffmpeg=True),
            "ok",
        )
        self.assertEqual(
            fps_gate_reason(width=3840, height=2160, fps=48, has_ffmpeg=True),
            "fps_too_low",
        )


class ProbeVideoMetaFlagTests(unittest.TestCase):
    def test_bulk_probe_does_not_request_video_meta_by_default(self) -> None:
        with mock.patch("vg.media._ffprobe_path", return_value="ffprobe"), mock.patch(
            "vg.media.subprocess.run"
        ) as run, mock.patch.object(Path, "is_file", return_value=True):
            run.return_value = mock.Mock(
                returncode=0,
                stdout='{"streams":[{"codec_type":"video"}],"format":{"duration":"1"}}',
                stderr="",
            )
            probe_media_info("ffmpeg", Path("x.mp4"), include_duration=True, include_audio=False)
            cmd = run.call_args.args[0]
            joined = " ".join(cmd)
            self.assertNotIn("width", joined)
            self.assertNotIn("r_frame_rate", joined)

    def test_player_probe_can_request_video_meta(self) -> None:
        with mock.patch("vg.media._ffprobe_path", return_value="ffprobe"), mock.patch(
            "vg.media.subprocess.run"
        ) as run, mock.patch.object(Path, "is_file", return_value=True):
            run.return_value = mock.Mock(
                returncode=0,
                stdout=(
                    '{"streams":[{"codec_type":"video","codec_name":"hevc","width":3840,"height":2160,'
                    '"r_frame_rate":"60/1","avg_frame_rate":"60/1"}],"format":{}}'
                ),
                stderr="",
            )
            info = probe_media_info(
                "ffmpeg",
                Path("x.mp4"),
                include_duration=False,
                include_audio=False,
                include_video_meta=True,
            )
            self.assertTrue(info.get("ok"))
            self.assertEqual(info.get("width"), 3840)
            self.assertEqual(info.get("height"), 2160)
            self.assertEqual(info.get("fps"), 60.0)
            self.assertEqual(info.get("video_codec"), "hevc")
            cmd = " ".join(run.call_args.args[0])
            self.assertIn("codec_name", cmd)
            self.assertIn("width", cmd)


class PlayerVideoMetaPersistTests(unittest.TestCase):
    def test_apply_probe_stores_codec_and_dims(self) -> None:
        from vg.media import _apply_probe_to_item

        item = {"id": "abc"}
        _apply_probe_to_item(
            item,
            {
                "ok": True,
                "width": 3840,
                "height": 2160,
                "fps": 60,
                "video_codec": "h264",
            },
            include_duration=False,
            include_audio=False,
            include_video_meta=True,
        )
        self.assertEqual(item["width"], 3840)
        self.assertEqual(item["height"], 2160)
        self.assertEqual(item["fps"], 60.0)
        self.assertEqual(item["video_codec"], "h264")
        self.assertTrue(item["probe_video_meta_done"])

    def test_wants_probe_until_codec_cached(self) -> None:
        from vg.media import wants_player_video_meta

        self.assertTrue(wants_player_video_meta({}, stream_like=False))
        self.assertFalse(wants_player_video_meta({}, stream_like=True))
        self.assertTrue(
            wants_player_video_meta(
                {"width": 1920, "height": 1080, "probe_video_meta_done": True},
                stream_like=False,
            )
        )
        self.assertFalse(
            wants_player_video_meta(
                {
                    "width": 1920,
                    "height": 1080,
                    "video_codec": "hevc",
                    "probe_video_meta_done": True,
                },
                stream_like=False,
            )
        )


    def test_fps30_encoder_follows_source_codec(self) -> None:
        from vg.convert import _fps30_video_encode_args

        label, args = _fps30_video_encode_args("hevc")
        self.assertEqual(label, "H.265")
        self.assertIn("libx265", args)
        label, args = _fps30_video_encode_args("h264")
        self.assertEqual(label, "H.264")
        self.assertIn("libx264", args)
        label, args = _fps30_video_encode_args("")
        self.assertEqual(label, "H.264")
        self.assertIn("libx264", args)

    def test_video_encode_and_vf_helpers(self) -> None:
        from vg.convert import (
            _vf_args,
            _video_encode_args,
            probe_ffmpeg_realmedia,
            resolve_transcode_out_ext,
        )

        label, args = _video_encode_args("auto", "hevc", "mp4")
        self.assertEqual(label, "H.265")
        self.assertIn("libx265", args)
        label, args = _video_encode_args("h264", "hevc", "mp4")
        self.assertEqual(label, "H.264")
        self.assertIn("libx264", args)
        label, args = _video_encode_args("h265", "h264", "mkv")
        self.assertEqual(label, "H.265")
        self.assertIn("libx265", args)
        label, args = _video_encode_args("auto", "h264", "webm")
        self.assertEqual(label, "VP9")
        self.assertIn("libvpx-vp9", args)
        self.assertEqual(_vf_args(30, 0), ["-vf", "fps=30"])
        self.assertEqual(_vf_args(None, 1080), ["-vf", "scale=-2:1080"])
        self.assertEqual(_vf_args(24, 720), ["-vf", "fps=24,scale=-2:720"])
        self.assertEqual(_vf_args(None, 0), [])
        ext, note = resolve_transcode_out_ext("webm", {"ext": ".mkv"}, "h264")
        self.assertEqual(ext, "mkv")
        self.assertIn("MKV", note)
        with mock.patch("vg.convert.subprocess.run") as run:
            run.return_value = mock.Mock(stdout=" D  rm              RealMedia\n", stderr="")
            self.assertTrue(probe_ffmpeg_realmedia("ffmpeg"))
            run.return_value = mock.Mock(stdout=" D  mov,mp4,m4a\n", stderr="")
            self.assertFalse(probe_ffmpeg_realmedia("ffmpeg"))
        self.assertFalse(probe_ffmpeg_realmedia(None))


class UiFpsControlsTests(unittest.TestCase):
    def test_player_has_convert_panel(self) -> None:
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="convertPanel"', html)
        self.assertIn('id="transcodeExt"', html)
        self.assertIn('id="transcodeFps"', html)
        self.assertIn('id="transcodeScale"', html)
        self.assertIn('id="transcodeEncoder"', html)
        self.assertIn('id="btnStartTranscode"', html)
        self.assertIn('id="btnConvertToggle"', html)
        self.assertIn('id="playerChips"', html)
        self.assertIn("function startTranscode(", html)
        self.assertIn("/api/transcode/", html)
        self.assertNotIn('id="btnToMp4"', html)
        self.assertNotIn('id="btnFps30"', html)


class TranscodeApiTests(unittest.TestCase):
    def test_rmvb_rejected_without_realmedia(self) -> None:
        from vg import web
        from vg.catalog_repository import catalog_repository
        from vg.config import RM_UNAVAILABLE_MSG
        from vg.state import STATE

        vid = "a" * 16
        item = {
            "id": vid,
            "ext": ".rmvb",
            "name": "clip.rmvb",
            "fps": 25,
            "width": 640,
            "height": 480,
            "_lib_root": r"D:\lib",
        }
        prev_ff = STATE.get("ffmpeg")
        prev_rm = STATE.get("ffmpeg_rm")
        prev_root = STATE.get("root")
        prev_jobs = STATE.get("convert_jobs")
        try:
            STATE["ffmpeg"] = "ffmpeg"
            STATE["ffmpeg_rm"] = False
            STATE["root"] = r"D:\lib"
            STATE["convert_jobs"] = {}
            with mock.patch.object(catalog_repository, "find_video", return_value=item), mock.patch.object(
                catalog_repository, "mounted_roots", return_value=[r"D:\lib"]
            ):
                resp = web.app.test_client().post(
                    f"/api/transcode/{vid}?root=D%3A%5Clib",
                    json={"out_ext": "mp4", "video_encoder": "h264"},
                )
            self.assertEqual(resp.status_code, 400)
            self.assertFalse(resp.get_json().get("ok"))
            self.assertIn("RealMedia", resp.get_json().get("msg") or "")
            self.assertEqual(resp.get_json().get("msg"), RM_UNAVAILABLE_MSG)
        finally:
            STATE["ffmpeg"] = prev_ff
            STATE["ffmpeg_rm"] = prev_rm
            STATE["root"] = prev_root
            STATE["convert_jobs"] = prev_jobs

    def test_rmvb_enqueued_when_realmedia_present(self) -> None:
        from vg import web
        from vg.catalog_repository import catalog_repository
        from vg.state import STATE

        vid = "b" * 16
        item = {
            "id": vid,
            "ext": ".rmvb",
            "name": "clip.rmvb",
            "fps": 25,
            "width": 640,
            "height": 480,
            "_lib_root": r"D:\lib",
        }
        prev_ff = STATE.get("ffmpeg")
        prev_rm = STATE.get("ffmpeg_rm")
        prev_root = STATE.get("root")
        prev_jobs = STATE.get("convert_jobs")
        try:
            STATE["ffmpeg"] = "ffmpeg"
            STATE["ffmpeg_rm"] = True
            STATE["root"] = r"D:\lib"
            STATE["convert_jobs"] = {}
            with mock.patch.object(catalog_repository, "find_video", return_value=item), mock.patch.object(
                catalog_repository, "mounted_roots", return_value=[r"D:\lib"]
            ), mock.patch("vg.convert.pump_convert_queue"):
                resp = web.app.test_client().post(
                    f"/api/transcode/{vid}?root=D%3A%5Clib",
                    json={"out_ext": "mp4", "video_encoder": "h264"},
                )
            data = resp.get_json()
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(data.get("ok"))
            self.assertTrue(data.get("job_id"))
            self.assertEqual(data.get("kind"), "transcode")
        finally:
            STATE["ffmpeg"] = prev_ff
            STATE["ffmpeg_rm"] = prev_rm
            STATE["root"] = prev_root
            STATE["convert_jobs"] = prev_jobs


if __name__ == "__main__":
    unittest.main()
