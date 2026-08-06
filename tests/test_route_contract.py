# -*- coding: utf-8 -*-
"""Route modularization must preserve the public Flask API."""
from __future__ import annotations

import unittest

from vg import web
from vg.state import STATE


EXPECTED_ROUTES = {
    ("/", frozenset({"GET"})),
    ("/api/cleanup", frozenset({"GET"})),
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
        self.assertIn("scanning", status.get_json())

        share = self.client.get("/api/share")
        self.assertEqual(share.status_code, 200)
        self.assertTrue(share.get_json()["ok"])

        roots = self.client.get("/api/roots")
        self.assertEqual(roots.status_code, 200)
        self.assertTrue(roots.get_json()["ok"])

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
