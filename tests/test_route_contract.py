# -*- coding: utf-8 -*-
"""Route modularization must preserve the public Flask API."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vg import web
from vg.state import STATE


EXPECTED_ROUTES = {
    ("/", frozenset({"GET"})),
    ("/api/cleanup", frozenset({"GET"})),
    ("/api/client-log", frozenset({"POST"})),
    ("/api/convert-mp4/<vid>", frozenset({"POST"})),
    ("/api/convert-mp4/job/<job_id>", frozenset({"GET"})),
    ("/api/convert-mp4/job/<job_id>/cancel", frozenset({"POST"})),
    ("/api/convert/batch", frozenset({"POST"})),
    ("/api/convert/queue", frozenset({"GET"})),
    ("/api/delete", frozenset({"POST"})),
    ("/api/drives", frozenset({"GET"})),
    ("/api/export-static", frozenset({"POST"})),
    ("/api/export-static/reveal", frozenset({"POST"})),
    ("/api/export-static/status", frozenset({"GET"})),
    ("/api/fix-audio/<vid>", frozenset({"POST"})),
    ("/api/info/<vid>", frozenset({"GET"})),
    ("/api/local/<vid>", frozenset({"POST"})),
    ("/api/privacy", frozenset({"GET", "POST"})),
    ("/api/rescan", frozenset({"POST"})),
    ("/api/roots", frozenset({"GET", "POST"})),
    ("/api/scan", frozenset({"POST"})),
    ("/api/series/<sid>", frozenset({"GET"})),
    ("/api/share", frozenset({"GET", "POST"})),
    ("/api/status", frozenset({"GET"})),
    ("/api/thumb/<vid>", frozenset({"POST"})),
    ("/api/tree", frozenset({"GET"})),
    ("/api/videos", frozenset({"GET"})),
    ("/api/videos-by-ids", frozenset({"POST"})),
    ("/hls/<vid>/file", frozenset({"GET"})),
    ("/playlist/<vid>.m3u8", frozenset({"GET"})),
    ("/stream/<vid>", frozenset({"GET"})),
    ("/stream/<vid>/seg/<int:idx>", frozenset({"GET"})),
    ("/thumb/<vid>", frozenset({"GET"})),
}


class RouteContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = web.app.test_client()

    def test_public_rule_and_method_set_is_unchanged(self) -> None:
        actual = {
            (
                rule.rule,
                frozenset(rule.methods - {"HEAD", "OPTIONS"}),
            )
            for rule in web.app.url_map.iter_rules()
            if rule.rule != "/static/<path:filename>"
        }
        self.assertEqual(actual, EXPECTED_ROUTES)

    def test_extracted_leaf_routes_are_registered_on_web_app(self) -> None:
        privacy = self.client.get("/api/privacy")
        self.assertEqual(privacy.status_code, 200)
        self.assertTrue(privacy.get_json()["ok"])

        queue = self.client.get("/api/convert/queue")
        self.assertEqual(queue.status_code, 200)
        self.assertTrue(queue.get_json()["ok"])

        export_status = self.client.get("/api/export-static/status")
        self.assertEqual(export_status.status_code, 200)
        self.assertIn("exporting", export_status.get_json())

        status = self.client.get("/api/status")
        self.assertEqual(status.status_code, 200)
        payload = status.get_json()
        self.assertEqual(payload.get("app"), "video-gallery")
        self.assertIn("scanning", payload)

        share = self.client.get("/api/share")
        self.assertEqual(share.status_code, 200)
        self.assertTrue(share.get_json()["ok"])

        roots = self.client.get("/api/roots")
        self.assertEqual(roots.status_code, 200)
        self.assertTrue(roots.get_json()["ok"])

    def test_client_action_log_keeps_operation_id_and_request_header(self) -> None:
        from unittest import mock

        with mock.patch.object(web, "diagnostic_emit") as emit:
            response = self.client.post(
                "/api/client-log",
                headers={"X-VG-Operation-ID": "play-test-1"},
                json={
                    "event": "player_error",
                    "operation_id": "play-test-1",
                    "level": "ERROR",
                    "fields": {"video_id": "abc", "error_code": 4},
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("X-VG-Operation-ID"), "play-test-1")
        call = emit.call_args
        self.assertEqual(call.args[:2], ("ERROR", "client_action"))
        self.assertEqual(call.kwargs["action"], "player_error")
        self.assertEqual(call.kwargs["operation_id"], "play-test-1")
        self.assertEqual(call.kwargs["video_id"], "abc")

    def test_client_log_fields_do_not_collide_with_emit_kwargs(self) -> None:
        response = self.client.post(
            "/api/client-log",
            json={
                "event": "page_load_completed",
                "operation_id": "page-test-1",
                "level": "INFO",
                "fields": {
                    "request_id": "related-1",
                    "action": "open",
                    "video_id": "xyz",
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

    def test_scan_route_delegates_mounting_to_background_scan(self) -> None:
        old_scanning = STATE.get("scanning")
        STATE["scanning"] = False
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with (
                    mock.patch.object(
                        web,
                        "start_scan",
                        return_value=(True, "开始加载缓存"),
                    ) as start,
                    mock.patch.object(web, "roots_summary", return_value=[]) as summaries,
                    mock.patch("vg.roots.add_mount") as synchronous_mount,
                ):
                    root = Path(tmp).resolve()
                    response = self.client.post(
                        "/api/scan",
                        json={"path": str(root), "thumbs": True, "force": False},
                    )
        finally:
            STATE["scanning"] = old_scanning

        self.assertEqual(response.status_code, 200)
        start.assert_called_once_with(
            root,
            do_thumbs=True,
            force=False,
            replace_mounts=False,
        )
        summaries.assert_called_once_with()
        synchronous_mount.assert_not_called()

    def test_convert_parallel_only_request_keeps_legacy_response(self) -> None:
        previous = STATE.get("convert_parallel")
        try:
            response = self.client.post(
                "/api/convert/batch",
                json={"parallel": 1, "items": []},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["queued"], [])
            self.assertEqual(payload["parallel"], 1)
        finally:
            STATE["convert_parallel"] = previous


if __name__ == "__main__":
    unittest.main()
