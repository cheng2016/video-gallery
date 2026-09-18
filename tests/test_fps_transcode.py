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
        self.assertEqual(fps_target_choices(60), [24, 25, 30, 48, 50])
        self.assertEqual(fps_target_choices(90), [24, 25, 30, 48, 50, 60])
        self.assertEqual(fps_target_choices(120), [24, 25, 30, 48, 50, 60])
        self.assertEqual(normalize_target_fps(60, None), 30)
        self.assertEqual(normalize_target_fps(60, 30), 30)
        self.assertEqual(normalize_target_fps(120, 60), 60)
        self.assertEqual(normalize_target_fps(60, 20), 20)
        self.assertIsNone(normalize_target_fps(60, 60))
        self.assertIsNone(normalize_target_fps(60, 90))

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


class UiFpsControlsTests(unittest.TestCase):
    def test_player_has_separate_fps_actions(self) -> None:
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="modalFpsActions"', html)
        self.assertIn('id="btnFps30"', html)
        self.assertIn("/api/fps30/", html)
        self.assertIn("function startFps30(", html)
        self.assertIn("function updateFps30Button(", html)
        self.assertIn('id="fpsTargetSelect"', html)
        self.assertIn('id="fpsTargetCustom"', html)
        self.assertIn("帧率转码", html)
        # Must stay visually/API-separate from format convert.
        self.assertIn("与转 MP4 / 修声音无关", html)


if __name__ == "__main__":
    unittest.main()
