# -*- coding: utf-8 -*-
"""Diagnostics must remain useful without becoming a hot-path bottleneck."""
from __future__ import annotations

import contextlib
import io
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from vg import bootlog, diagnostics


class DiagnosticsTests(unittest.TestCase):
    def tearDown(self) -> None:
        diagnostics.set_full_logging(False)

    def test_error_always_prints_when_full_logging_is_off(self) -> None:
        diagnostics.set_full_logging(False)
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            mock.patch("vg.bootlog.write") as write,
        ):
            diagnostics.error("disk_read_failed", OSError("boom"), path="E:/Movies")
        text = output.getvalue()
        self.assertIn("[ERROR] disk_read_failed", text)
        self.assertIn("E:/Movies", text)
        write.assert_called_once()
        self.assertTrue(write.call_args.kwargs["urgent"])

    def test_success_detail_is_call_trace_not_loop_info(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch("vg.bootlog.write"):
            diagnostics.set_full_logging(False)
            diagnostics.emit("INFO", "thumb_hit", video_id="v1")
            diagnostics.call("api_videos", folder="电影", offset=0)
            self.assertEqual(output.getvalue(), "")
            diagnostics.set_full_logging(True)
            diagnostics.emit("INFO", "thumb_hit", video_id="v1")
            diagnostics.call("api_videos", folder="电影", offset=0)
        text = output.getvalue()
        self.assertNotIn("thumb_hit", text)
        self.assertIn("[CALL] api_videos", text)
        self.assertIn("folder=电影", text)

    def test_bootlog_batches_normal_lines_and_syncs_urgent_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_path = bootlog._LOG_PATH
            old_init = bootlog._INIT
            try:
                bootlog._LOG_PATH = Path(tmp) / "runtime.log"
                bootlog._INIT = True
                bootlog._PENDING.clear()
                bootlog.write("normal")
                self.assertFalse(bootlog.log_path().exists())
                bootlog.flush(sync=False)
                self.assertIn(
                    "normal",
                    bootlog.log_path().read_text(encoding="utf-8"),
                )
                bootlog.write("fatal", urgent=True)
                self.assertIn(
                    "fatal",
                    bootlog.log_path().read_text(encoding="utf-8"),
                )
            finally:
                bootlog._PENDING.clear()
                bootlog._LOG_PATH = old_path
                bootlog._INIT = old_init

    def test_aggregate_summary_includes_p50_and_p95(self) -> None:
        diagnostics._aggregates.clear()
        for elapsed in range(1, 101):
            diagnostics.aggregate("api_videos_test", float(elapsed))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch("vg.bootlog.write"):
            diagnostics.flush_aggregates()
        text = output.getvalue()
        self.assertIn("p50_ms=50.0", text)
        self.assertIn("p95_ms=95.0", text)

    def test_timed_lock_reports_waiting_and_acquisition(self) -> None:
        lock = threading.Lock()
        lock.acquire()

        def release_later() -> None:
            time.sleep(0.08)
            lock.release()

        thread = threading.Thread(target=release_later)
        thread.start()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch("vg.bootlog.write"):
            with diagnostics.timed_lock(
                lock,
                "catalog_test",
                warn_after=0.01,
                cache="test-cache",
            ):
                pass
        thread.join(timeout=1)
        text = output.getvalue()
        self.assertIn("lock_waiting", text)
        self.assertIn("lock_acquired", text)
        self.assertIn("lock=catalog_test", text)


if __name__ == "__main__":
    unittest.main()
