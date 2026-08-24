# -*- coding: utf-8 -*-
"""Narrow service interfaces support substitutes without global catalog setup."""
from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from flask import Flask

from vg.cleanup import build_cleanup_response
from vg.config import MIN_VIDEO_FILE_BYTES
from vg.routes.convert import register as register_convert
from vg.state import STATE


class FakeCatalogRepository:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        size = MIN_VIDEO_FILE_BYTES + 1
        self.videos = [
            {
                "id": "a" * 16,
                "name": "same",
                "filename": "same.mp4",
                "rel": "电影/a.mp4",
                "folder": "电影",
                "ext": ".mp4",
                "size": size,
                "root": str(self.root),
            },
            {
                "id": "b" * 16,
                "name": "same",
                "filename": "same-copy.mp4",
                "rel": "电影/b.mp4",
                "folder": "电影",
                "ext": ".mp4",
                "size": size,
                "root": str(self.root),
            },
        ]
        for video in self.videos:
            path = self.root / video["rel"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((b"same-content" * ((size // 12) + 1))[:size])
        self.lookup_calls: list[tuple[str, str | None]] = []

    def videos_for_scope(self, lib: str | None = None) -> list[dict]:
        return list(self.videos)

    def roots_summary(self) -> list[dict]:
        return [{"path": "D:/library", "label": "D:", "count": 2, "categories": []}]

    def mounted_roots(self) -> list[str]:
        return ["D:/library", "E:/library"]

    def find_video(self, vid: str, prefer_root: str | None = None) -> dict | None:
        self.lookup_calls.append((vid, prefer_root))
        return next((video for video in self.videos if video["id"] == vid), None)


class ServiceInterfaceTests(unittest.TestCase):
    def test_cleanup_depends_on_scope_reader_interface(self) -> None:
        repository = FakeCatalogRepository()
        self.addCleanup(repository.tmp.cleanup)
        response = build_cleanup_response(
            "dup",
            category="电影",
            reader=repository,
        )
        self.assertEqual(response["scope"]["video_count"], 2)
        self.assertEqual(response["groups"][0]["reason"], "同内容")
        self.assertEqual(response["roots"][0]["label"], "D:")

    def test_convert_route_accepts_repository_substitute(self) -> None:
        repository = FakeCatalogRepository()
        self.addCleanup(repository.tmp.cleanup)
        app = Flask("convert-interface-test")
        register_convert(app, repository=repository)
        previous_ffmpeg = STATE.get("ffmpeg")
        previous_root = STATE.get("root")
        try:
            STATE["ffmpeg"] = "ffmpeg"
            STATE["root"] = "D:/library"
            response = app.test_client().post(
                f"/api/convert-mp4/{'a' * 16}",
            )
        finally:
            STATE["ffmpeg"] = previous_ffmpeg
            STATE["root"] = previous_root

        self.assertEqual(response.status_code, 400)
        self.assertIn("必须指定 root", response.get_json()["msg"])
        self.assertEqual(repository.lookup_calls, [])


if __name__ == "__main__":
    unittest.main()
