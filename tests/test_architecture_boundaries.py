# -*- coding: utf-8 -*-
"""Import-boundary guards for the architecture refactor."""
from __future__ import annotations

import subprocess
import sys
import unittest


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_roots_import_does_not_pull_scan_module(self) -> None:
        code = (
            "import sys; import vg.roots; "
            "assert 'vg.scan' not in sys.modules, "
            "'vg.roots eagerly imported vg.scan'"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_scan_temporarily_reexports_catalog_entry_points(self) -> None:
        from vg import catalog, scan

        self.assertIs(scan.build_tree, catalog.build_tree)
        self.assertIs(scan.rebuild_indexes, catalog.rebuild_indexes)
        self.assertIs(scan._video_category, catalog.video_category)
        self.assertIs(scan._video_search_text, catalog.video_search_text)


if __name__ == "__main__":
    unittest.main()
