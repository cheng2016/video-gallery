# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vg import streaming, web


class PlaybackDiagnosticsTests(unittest.TestCase):
    def test_range_stream_does_not_log_successful_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "video.mp4"
            path.write_bytes(b"0123456789")
            with (
                web.app.test_request_context(
                    "/stream/test?op=play-123",
                    headers={"Range": "bytes=0-4"},
                ),
                mock.patch("vg.diagnostics.emit") as emit,
            ):
                response = streaming._stream_file(path, "video/mp4")
                self.assertEqual(response.status_code, 206)
                self.assertEqual(b"".join(response.response), b"01234")
            events = [call.args[1] for call in emit.call_args_list]
            self.assertNotIn("stream_range_open", events)
            self.assertNotIn("stream_range_closed", events)
            self.assertNotIn("stream_file_open", events)

    def test_invalid_stream_range_still_logs_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "video.mp4"
            path.write_bytes(b"0123456789")
            with (
                web.app.test_request_context(
                    "/stream/test?op=play-123",
                    headers={"Range": "bytes=abc"},
                ),
                mock.patch("vg.diagnostics.emit") as emit,
            ):
                with self.assertRaises(Exception):
                    streaming._stream_file(path, "video/mp4")
            events = [call.args[1] for call in emit.call_args_list]
            self.assertIn("stream_range_invalid", events)

    def test_missing_thumbnail_logs_why_generation_is_unavailable(self) -> None:
        vid = "a" * 16
        with (
            mock.patch.object(web, "_cache_dir_from_root_hint", return_value=None),
            mock.patch.object(web, "find_video_by_id", return_value=None),
            mock.patch.object(web, "diagnostic_emit") as emit,
        ):
            response = web.app.test_client().get(
                f"/thumb/{vid}?defer=1&root=E%3A%2FMovies&op=play-456"
            )
        self.assertEqual(response.status_code, 503)
        matches = [
            call
            for call in emit.call_args_list
            if len(call.args) > 1 and call.args[1] == "thumbnail_generation_unavailable"
        ]
        self.assertEqual(len(matches), 1)
        self.assertFalse(matches[0].kwargs["item_found"])
        self.assertEqual(matches[0].kwargs["operation_id"], "play-456")

    def test_deferred_thumbnail_does_not_load_catalog_during_scan(self) -> None:
        vid = "b" * 16
        old_scanning = web.STATE.get("scanning")
        old_updating = web.STATE.get("updating")
        web.STATE["scanning"] = True
        web.STATE["updating"] = False
        try:
            with (
                mock.patch.object(web, "_cache_dir_from_root_hint", return_value=None),
                mock.patch.object(web, "find_video_by_id") as lookup,
                mock.patch.object(web, "diagnostic_emit_rate_limited") as emit,
            ):
                response = web.app.test_client().get(
                    f"/thumb/{vid}?defer=1&root=E%3A%2FMovies&op=play-789"
                )
            self.assertEqual(response.status_code, 503)
            lookup.assert_not_called()
            events = [call.args[1] for call in emit.call_args_list if len(call.args) > 1]
            self.assertIn("thumbnail_deferred_lookup_skipped", events)
        finally:
            web.STATE["scanning"] = old_scanning
            web.STATE["updating"] = old_updating


if __name__ == "__main__":
    unittest.main()
