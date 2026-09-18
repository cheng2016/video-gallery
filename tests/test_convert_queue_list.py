# -*- coding: utf-8 -*-
"""Regression: convert queue listing must return job dicts, not job-id strings."""
from __future__ import annotations

import unittest

from vg.convert import list_convert_jobs
from vg.state import STATE


class ConvertQueueListTests(unittest.TestCase):
    def test_list_jobs_returns_dicts_when_queue_nonempty(self) -> None:
        prev = STATE.get("convert_jobs")
        try:
            STATE["convert_jobs"] = {
                "job1": {
                    "id": "job1",
                    "vid": "abcdef0123456789",
                    "root": "",
                    "kind": "fps30",
                    "name": "demo",
                    "status": "running",
                    "msg": "帧率转码中… 40%",
                    "percent": 40,
                    "out_path": "",
                    "added_id": "",
                    "created": 100.0,
                },
                "job0": {
                    "id": "job0",
                    "vid": "0123456789abcdef",
                    "root": "",
                    "kind": "mp4",
                    "name": "older",
                    "status": "done",
                    "msg": "完成",
                    "percent": 100,
                    "out_path": "",
                    "added_id": "",
                    "created": 50.0,
                },
            }
            jobs = list_convert_jobs(10)
            self.assertEqual(len(jobs), 2)
            self.assertIsInstance(jobs[0], dict)
            self.assertEqual(jobs[0]["id"], "job1")
            self.assertEqual(jobs[0]["kind"], "fps30")
            self.assertEqual(jobs[0]["percent"], 40)
        finally:
            STATE["convert_jobs"] = prev if prev is not None else {}
