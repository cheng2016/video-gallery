# -*- coding: utf-8 -*-
"""Bounded memory/disk cache behavior for long-running large libraries."""
from __future__ import annotations

import tempfile
import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

from vg import cache
from vg.cache import cleanup_thumb_files, thumb_cache_get, thumb_cache_invalidate, thumb_cache_put
from vg.config import THUMB_EXT
from vg.state import _thumb_jpeg_cache


class CacheLimitTests(unittest.TestCase):
    def tearDown(self) -> None:
        thumb_cache_invalidate()

    def test_jpeg_lru_obeys_byte_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            with mock.patch.object(cache, "THUMB_JPEG_CACHE_MAX_BYTES", 150):
                thumb_cache_put("a", b"a" * 100, cache_dir)
                thumb_cache_put("b", b"b" * 100, cache_dir)
            self.assertIsNone(thumb_cache_get("a", cache_dir))
            self.assertEqual(thumb_cache_get("b", cache_dir), b"b" * 100)
            self.assertEqual(len(_thumb_jpeg_cache), 1)

    def test_disk_cleanup_removes_only_orphans_below_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            keep = cache_dir / f"keep{THUMB_EXT}"
            orphan = cache_dir / f"orphan{THUMB_EXT}"
            keep.write_bytes(b"k" * 100)
            orphan.write_bytes(b"o" * 100)
            removed, freed = cleanup_thumb_files(
                cache_dir,
                {"keep"},
                max_bytes=1_000,
            )
            self.assertEqual(removed, 1)
            self.assertEqual(freed, 100)
            self.assertTrue(keep.exists())
            self.assertFalse(orphan.exists())

    def test_corrupt_thumbnail_logs_path_and_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            corrupt = cache_dir / f"broken{THUMB_EXT}"
            corrupt.write_bytes(b"x" * 25)
            output = io.StringIO()
            with (
                contextlib.redirect_stdout(output),
                mock.patch("vg.bootlog.write"),
            ):
                self.assertIsNone(cache.read_thumb_jpeg(cache_dir, "broken"))
            text = output.getvalue()
            self.assertIn("thumb_cache_invalid", text)
            self.assertIn("decrypt_or_jpeg_validation_failed", text)
            self.assertIn(str(corrupt), text)


if __name__ == "__main__":
    unittest.main()
