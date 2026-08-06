# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import run
from vg import drives, lan, media, trash
from vg.export import _write_macos_launcher
from vg.routes import export_static


class MacLauncherTests(unittest.TestCase):
    def test_venv_python_path_uses_posix_layout(self):
        root = Path("/project")
        self.assertEqual(
            run.venv_python_path(root, "darwin"),
            root / ".venv" / "bin" / "python",
        )

    def test_static_export_writes_executable_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_macos_launcher(Path(tmp))
            text = path.read_text(encoding="utf-8")
            self.assertIn('python3 "_cache/static_bridge.py"', text)
            if os.name != "nt":
                self.assertTrue(path.stat().st_mode & stat.S_IXUSR)

    def test_static_site_routes_open_and_reveal_through_bridge(self):
        template = (
            Path(__file__).resolve().parents[1] / "templates" / "static_site.html"
        ).read_text(encoding="utf-8")
        self.assertIn('tryBridgePost("open", path, v.id)', template)
        self.assertIn('tryBridgePost("reveal", path, v.id)', template)


class MacDriveTests(unittest.TestCase):
    def test_darwin_lists_user_media_and_volumes_without_root_folders(self):
        seen: list[str] = []

        def fake_entry(path: Path, label=None):
            key = path.as_posix()
            seen.append(key)
            return {
                "letter": label or path.name,
                "path": key,
                "label": label or path.name,
                "type": "目录",
                "free_h": "",
                "total_h": "",
                "display": label or path.name,
            }

        def fake_is_dir(path: Path):
            return path.as_posix() == "/Volumes"

        def fake_iterdir(path: Path):
            if path.as_posix() == "/Volumes":
                return iter([Path("/Volumes/Media")])
            return iter(())

        with patch.object(drives.sys, "platform", "darwin"), \
             patch.object(drives.Path, "home", return_value=Path("/Users/demo")), \
             patch.object(drives.Path, "is_dir", fake_is_dir), \
             patch.object(drives.Path, "iterdir", fake_iterdir), \
             patch.object(drives, "_mounted_entry", side_effect=fake_entry):
            rows = drives.list_drives_info()

        self.assertEqual(len(rows), 3)
        self.assertIn("/Users/demo/Movies", seen)
        self.assertIn("/Users/demo/Videos", seen)
        self.assertIn("/Volumes/Media", seen)
        self.assertNotIn("/Applications", seen)


class MacSystemIntegrationTests(unittest.TestCase):
    def test_ffmpeg_finds_apple_silicon_homebrew_path(self):
        target = "/opt/homebrew/bin/ffmpeg"
        with patch.object(media.sys, "platform", "darwin"), \
             patch.object(media.shutil, "which", return_value=None), \
             patch.object(media.os.path, "isfile", side_effect=lambda p: p == target):
            self.assertEqual(media.find_ffmpeg(), target)

    def test_ffmpeg_finds_intel_homebrew_path(self):
        target = "/usr/local/bin/ffmpeg"
        with patch.object(media.sys, "platform", "darwin"), \
             patch.object(media.shutil, "which", return_value=None), \
             patch.object(media.os.path, "isfile", side_effect=lambda p: p == target):
            self.assertEqual(media.find_ffmpeg(), target)

    def test_firewall_returns_macos_instructions(self):
        with patch.object(lan.sys, "platform", "darwin"):
            ok, message = lan.ensure_firewall_allow(8765)
        self.assertTrue(ok)
        self.assertIn("系统设置", message)
        self.assertIn("Python", message)

    def test_trash_passes_special_path_as_osascript_argument(self):
        path = Path('/tmp/a "quoted" video.mp4')
        with patch.object(trash.sys, "platform", "darwin"), \
             patch.object(trash.Path, "exists", side_effect=[True, False]), \
             patch.object(trash.subprocess, "run") as run_process:
            ok, _ = trash.move_to_trash(path)
        self.assertTrue(ok)
        args = run_process.call_args.args[0]
        self.assertEqual(args[0], "osascript")
        self.assertEqual(args[-1], str(path.resolve()))
        self.assertNotIn(str(path.resolve()), " ".join(args[1:-1]))

    def test_trash_does_not_report_success_when_file_remains(self):
        path = Path("/tmp/video.mp4")
        result = SimpleNamespace(stderr="", stdout="")
        with patch.object(trash.sys, "platform", "darwin"), \
             patch.object(trash.Path, "exists", side_effect=[True, True]), \
             patch.object(trash.subprocess, "run", return_value=result):
            ok, message = trash.move_to_trash(path)
        self.assertFalse(ok)
        self.assertIn("Finder", message)

    def test_export_folder_uses_macos_open(self):
        with patch.object(export_static.sys, "platform", "darwin"), \
             patch.object(export_static.subprocess, "Popen") as popen:
            export_static._open_folder("/tmp/export")
        popen.assert_called_once_with(["open", "/tmp/export"])


if __name__ == "__main__":
    unittest.main()
