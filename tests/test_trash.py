# -*- coding: utf-8 -*-
"""Recycle-bin helpers must not turn a logging failure into a failed delete."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vg import trash


class _Result:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.stderr = ""
        self.stdout = ""


@unittest.skipUnless(sys.platform == "win32", "Windows recycle-bin path")
class TrashTests(unittest.TestCase):
    def test_log_failure_does_not_undo_a_successful_recycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.mp4"
            path.write_bytes(b"x")

            def fake_run(_cmd, **_kwargs):
                path.unlink()
                return _Result(0)

            with (
                patch("vg.trash.subprocess.run", side_effect=fake_run),
                patch("vg.trash.diagnostic_perf", side_effect=RuntimeError("log broke")),
            ):
                ok, msg = trash.move_to_trash(path)
            self.assertTrue(ok)
            self.assertEqual(msg, "已移到回收站")
            self.assertFalse(path.exists())

    def test_file_fallback_to_directory_is_one_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.mp4"
            path.write_bytes(b"x")
            calls: list[tuple] = []

            def fake_run(cmd, **_kwargs):
                command = cmd[-1]
                if "DeleteDirectory" in command:
                    path.unlink()
                    return _Result(0)
                return _Result(1)

            def capture(*args, **kwargs):
                calls.append((args, kwargs))

            with (
                patch("vg.trash.subprocess.run", side_effect=fake_run),
                patch("vg.trash.diagnostic_perf", side_effect=capture),
            ):
                ok, msg = trash.move_to_trash(path)

        self.assertTrue(ok)
        self.assertEqual(msg, "已移到回收站")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][0], "trash_ps")
        self.assertEqual(calls[0][1]["op"], "dir")
        self.assertTrue(calls[0][1]["ok"])
        self.assertIn("file_ms", calls[0][1])
        self.assertIn("dir_ms", calls[0][1])


if __name__ == "__main__":
    unittest.main()
