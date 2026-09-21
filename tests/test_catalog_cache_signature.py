# -*- coding: utf-8 -*-
"""Tree-cache signature must ignore an empty drive root."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from vg.catalog_cache import (
    _collect_catalog_signatures,
    _signature_root_key,
    _signatures_equal,
)


def _sig(catalogs: dict) -> dict:
    return {
        "schema": 1,
        "count": 6000,
        "genres_ver": 1,
        "taxonomy_ver": 1,
        "catalogs": catalogs,
    }


class CatalogSignatureRootTests(unittest.TestCase):
    def test_drive_root_keys_collapse(self) -> None:
        self.assertEqual(_signature_root_key("D:"), "D:\\")
        self.assertEqual(_signature_root_key("D:\\"), "D:\\")
        self.assertEqual(_signature_root_key("D:/"), "D:\\")
        self.assertEqual(
            _signature_root_key(r"D:\vg_bench_fixture\C_drive"),
            r"D:\vg_bench_fixture\C_drive",
        )

    def test_empty_drive_root_does_not_diverge(self) -> None:
        fixture = {
            r"D:\vg_bench_fixture\C_drive": 1.5,
            r"D:\vg_bench_fixture\D_drive": 2.5,
        }
        saved = _sig({**fixture, "D:": 0.0})
        live = _sig(dict(fixture))
        ok, why = _signatures_equal(live, saved)
        self.assertTrue(ok, why)

    def test_drive_root_forms_with_catalog_match(self) -> None:
        ok, why = _signatures_equal(
            _sig({"D:": 9.0}),
            _sig({"D:\\": 9.0}),
        )
        self.assertTrue(ok, why)

    def test_real_catalog_mtime_still_mismatches(self) -> None:
        ok, why = _signatures_equal(
            _sig({r"D:\vg_bench_fixture\C_drive": 2.0}),
            _sig({r"D:\vg_bench_fixture\C_drive": 1.0}),
        )
        self.assertFalse(ok)
        self.assertIn("catalog_mtime_mismatch", why)

    def test_collect_omits_mount_without_catalog(self) -> None:
        def mtime(cache: Path) -> float:
            return 0.0 if str(cache) in {"D:\\", "D:"} else 4.0

        with mock.patch("vg.cache.ensure_cache_dir", side_effect=lambda p: Path(p)), mock.patch(
            "vg.catalog_db.catalog_structure_mtime", side_effect=mtime
        ):
            sig = _collect_catalog_signatures(
                [r"D:\vg_bench_fixture\C_drive", "D:\\", "D:"]
            )
        self.assertEqual(sig, {r"D:\vg_bench_fixture\C_drive": 4.0})


if __name__ == "__main__":
    unittest.main()
