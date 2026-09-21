# -*- coding: utf-8 -*-
"""6000+ library HTTP smoke / local stress harness.

CI (unittest discover): builds a synthetic catalog and asserts loose budgets so
a slow runner does not flake.

Local stress (SQLite-only, no disk walk / ffmpeg)::

    set PYTHONPATH=D:\\video-gallery
    python tests/test_large_library_http_smoke.py --count 6000

Full PC simulation (real files + scan + optional thumbs)::

    python tests/bench_large_library_live.py --count 6000
    python tests/bench_large_library_live.py --count 6000 --thumbs 80
"""
from __future__ import annotations

import argparse
import os
import statistics
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from unittest import mock

from vg import web
from vg.cache import thumb_path
from vg.catalog import rebuild_indexes
from vg.catalog_db import save_catalog
from vg.state import STATE


DEFAULT_COUNT = 6000
# Loose enough for CI VMs; VG_PERF=1 tightens below.
CI_BUDGETS_MS = {
    "status": 80,
    "tree_miss": 1500,
    "videos_offset0": 1200,
    "videos_deep": 400,
    "by_ids_20": 250,
    "by_ids_100": 600,
    "by_ids_20_with_ensure": 250,
    "thumb_ready": 120,
    "thumb_missing": 120,
    "churn_videos_p95": 2000,
}
PERF_BUDGETS_MS = {
    "status": 20,
    "tree_miss": 400,
    "videos_offset0": 250,
    "videos_deep": 80,
    "by_ids_20": 50,
    "by_ids_100": 150,
    "by_ids_20_with_ensure": 50,
    "thumb_ready": 30,
    "thumb_missing": 40,
    "churn_videos_p95": 500,
}


def _perf_mode() -> bool:
    return os.environ.get("VG_PERF", "").strip() in {"1", "true", "True", "yes"}


def _budgets() -> dict[str, float]:
    return PERF_BUDGETS_MS if _perf_mode() else CI_BUDGETS_MS


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _p95(samples: list[float]) -> float:
    if not samples:
        return 0.0
    if len(samples) == 1:
        return samples[0]
    ordered = sorted(samples)
    idx = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
    return ordered[idx]


def build_rows(count: int, *, has_thumb: bool = True) -> list[dict]:
    rows: list[dict] = []
    for i in range(count):
        folder = "电影" if i % 3 else "综艺"
        name = f"clip-{i:05d}"
        rows.append(
            {
                "id": f"{i:016x}",
                "name": name,
                "filename": f"{name}.mp4",
                "rel": f"{folder}/{name}.mp4",
                "folder": folder,
                "ext": ".mp4",
                "size": 1_000_000 + i,
                "mtime": float(1_700_000_000 + i),
                "duration": 60.0 + (i % 120),
                "duration_h": "1:00",
                "genres": [folder],
                "has_thumb": has_thumb,
                "thumb_v": 1 if has_thumb else 0,
            }
        )
    return rows


class LargeLibraryFixture:
    """Temp root + SQLite catalog + STATE mount for Flask test_client."""

    def __init__(self, count: int = DEFAULT_COUNT) -> None:
        self.count = count
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "library"
        self.cache = self.base / "cache"
        self.root.mkdir()
        self.cache.mkdir()
        self.rows = build_rows(count)
        self.old_state: dict = {}
        self._hint_patch = None

    def __enter__(self) -> "LargeLibraryFixture":
        t0 = time.perf_counter()
        assert save_catalog(self.cache, self.root, self.rows)
        self.save_catalog_ms = _ms(t0)

        # Minimal ready .vgt so /thumb hits L2 without ffmpeg.
        sample = self.rows[0]["id"]
        out = thumb_path(self.cache, sample)
        # VG1 header + padding — enough for thumb_file_ready (>24 bytes).
        out.write_bytes(b"VG1\x00" + (b"\x00" * 64) + b"\xff\xd8\xff\xd9")

        keys = (
            "root",
            "mounted_roots",
            "lan_share",
            "lib_gen",
            "videos",
            "by_id",
            "facets",
            "tree",
            "disk_libs",
            "scanning",
            "updating",
            "meta_progress",
            "scan_root",
            "cache_dir",
            "ffmpeg",
        )
        self.old_state = {k: STATE.get(k) for k in keys}
        root_s = str(self.root.resolve())
        t1 = time.perf_counter()
        videos = [dict(r) for r in self.rows]
        for v in videos:
            v["root"] = root_s
            v["_lib_root"] = root_s
            v["_lib_cache"] = str(self.cache)
        rebuild_indexes(videos, heavy=True)
        self.rebuild_indexes_ms = _ms(t1)

        STATE.update(
            {
                "root": self.root,
                "mounted_roots": [root_s],
                "lan_share": False,
                "lib_gen": int(STATE.get("lib_gen") or 0) + 1,
                "disk_libs": {
                    root_s: {
                        "root": root_s,
                        "cache_dir": str(self.cache),
                        "by_id": {v["id"]: v for v in videos},
                        "updated": time.time(),
                        "index_mtime": time.time(),
                        "live": False,
                    }
                },
                "scanning": False,
                "updating": False,
                "meta_progress": "",
                "scan_root": "",
                "cache_dir": self.cache,
                "ffmpeg": None,
            }
        )
        self._hint_patch = mock.patch.object(
            web, "_cache_dir_from_root_hint", return_value=self.cache
        )
        self._hint_patch.start()
        web.invalidate_response_caches()
        self.client = web.app.test_client()
        self.root_s = root_s
        self.sample_id = sample
        self.missing_id = "f" * 16
        return self

    def __exit__(self, *exc) -> None:
        if self._hint_patch is not None:
            self._hint_patch.stop()
        STATE.update(self.old_state)
        web.invalidate_response_caches()
        self._tmp.cleanup()

    def time_get(self, path: str, repeats: int = 1, *, ok_statuses: set[int] | None = None) -> list[float]:
        allowed = ok_statuses or {200}
        out: list[float] = []
        for _ in range(repeats):
            started = time.perf_counter()
            resp = self.client.get(path)
            elapsed = _ms(started)
            if resp.status_code not in allowed:
                raise AssertionError(f"GET {path} -> {resp.status_code}")
            out.append(elapsed)
        return out

    def time_post_json(self, path: str, payload: dict, repeats: int = 1) -> list[float]:
        out: list[float] = []
        for _ in range(repeats):
            started = time.perf_counter()
            resp = self.client.post(path, json=payload)
            elapsed = _ms(started)
            if resp.status_code >= 500:
                raise AssertionError(f"POST {path} -> {resp.status_code}")
            out.append(elapsed)
        return out


def run_suite(count: int = DEFAULT_COUNT) -> dict:
    """Execute timed scenarios; return metrics dict for print / assert."""
    budgets = _budgets()
    report: dict = {"count": count, "budgets": budgets, "samples": {}, "ok": True, "failures": []}

    def record(name: str, samples: list[float], *, use_p95: bool = False) -> None:
        val = _p95(samples) if use_p95 else samples[0]
        report["samples"][name] = {
            "ms": round(val, 1),
            "n": len(samples),
            "avg": round(statistics.mean(samples), 1) if samples else 0.0,
            "max": round(max(samples), 1) if samples else 0.0,
            "budget": budgets.get(name),
        }
        budget = budgets.get(name)
        if budget is not None and val > budget:
            report["ok"] = False
            report["failures"].append(f"{name}={val:.1f}ms > {budget}ms")

    with LargeLibraryFixture(count) as fx:
        report["samples"]["save_catalog"] = {
            "ms": round(fx.save_catalog_ms, 1),
            "n": 1,
            "budget": None,
        }
        report["samples"]["rebuild_indexes"] = {
            "ms": round(fx.rebuild_indexes_ms, 1),
            "n": 1,
            "budget": None,
        }

        lib_q = fx.root_s.replace("\\", "/")
        record("status", fx.time_get("/api/status", repeats=5), use_p95=True)

        web.invalidate_response_caches()
        record("tree_miss", fx.time_get(f"/api/tree?lib={lib_q}"))
        # Warm hit (no separate budget — should be << miss).
        tree_hit = fx.time_get(f"/api/tree?lib={lib_q}", repeats=3)
        report["samples"]["tree_hit"] = {
            "ms": round(statistics.mean(tree_hit), 1),
            "n": len(tree_hit),
            "budget": None,
        }

        web.invalidate_response_caches()
        record(
            "videos_offset0",
            fx.time_get(
                f"/api/videos?lib={lib_q}&category=&sort=mtime_desc&view=flat&offset=0&limit=20"
            ),
        )
        record(
            "videos_deep",
            fx.time_get(
                f"/api/videos?lib={lib_q}&category=&sort=mtime_desc&view=flat"
                f"&offset={max(0, count - 40)}&limit=20"
            ),
        )

        ids20 = [r["id"] for r in fx.rows[:20]]
        ids100 = [r["id"] for r in fx.rows[:100]]
        hints20 = {i: {"root": fx.root_s} for i in ids20}
        hints100 = {i: {"root": fx.root_s} for i in ids100}

        record(
            "by_ids_20",
            fx.time_post_json(
                "/api/videos-by-ids", {"ids": ids20, "hints": hints20}, repeats=3
            ),
            use_p95=True,
        )
        record(
            "by_ids_100",
            fx.time_post_json(
                "/api/videos-by-ids", {"ids": ids100, "hints": hints100}, repeats=3
            ),
            use_p95=True,
        )
        record(
            "by_ids_20_with_ensure",
            fx.time_post_json(
                "/api/videos-by-ids", {"ids": ids20, "hints": hints20}, repeats=3
            ),
            use_p95=True,
        )

        # Thumb path: ready file vs scan-time missing placeholder.
        record(
            "thumb_ready",
            fx.time_get(
                f"/thumb/{fx.sample_id}?v=1&defer=1&root={fx.root_s}",
                repeats=3,
                ok_statuses={200, 503},
            ),
            use_p95=True,
        )
        # Missing id while idle may still walk catalog lookup; budget the
        # deferred scan-time placeholder path instead (what cards hit at boot).
        prev_scan = STATE.get("scanning")
        prev_root = STATE.get("scan_root")
        STATE["scanning"] = True
        STATE["scan_root"] = fx.root_s
        try:
            record(
                "thumb_missing",
                fx.time_get(
                    f"/thumb/{fx.missing_id}?v=1&defer=1&root={fx.root_s}",
                    repeats=3,
                    ok_statuses={200, 503},
                ),
                use_p95=True,
            )
        finally:
            STATE["scanning"] = prev_scan
            STATE["scan_root"] = prev_root

        # Simulate mid-scan churn: lib_gen bumps + scanning flag while UI polls.
        STATE["scanning"] = True
        STATE["scan_root"] = fx.root_s
        churn_videos: list[float] = []
        churn_status: list[float] = []
        churn_byids: list[float] = []

        def bump_gen() -> None:
            STATE["lib_gen"] = int(STATE.get("lib_gen") or 0) + 1
            web.invalidate_response_caches()

        for _ in range(10):
            bump_gen()
            churn_status.extend(fx.time_get("/api/status"))
            churn_videos.extend(
                fx.time_get(
                    f"/api/videos?lib={lib_q}&sort=mtime_desc&view=flat&offset=0&limit=20"
                )
            )
            churn_byids.extend(
                fx.time_post_json(
                    "/api/videos-by-ids", {"ids": ids20, "hints": hints20}
                )
            )

        # Concurrent pressure: 8 parallel status+videos while gen bumps.
        parallel_videos: list[float] = []

        def one_videos() -> float:
            bump_gen()
            return fx.time_get(
                f"/api/videos?lib={lib_q}&sort=mtime_desc&view=flat&offset=0&limit=20"
            )[0]

        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = [pool.submit(one_videos) for _ in range(16)]
            for fut in as_completed(futs):
                parallel_videos.append(fut.result())

        STATE["scanning"] = False
        STATE["scan_root"] = ""
        record("churn_videos_p95", churn_videos + parallel_videos, use_p95=True)
        report["samples"]["churn_status_p95"] = {
            "ms": round(_p95(churn_status), 1),
            "n": len(churn_status),
            "budget": None,
        }
        report["samples"]["churn_byids_p95"] = {
            "ms": round(_p95(churn_byids), 1),
            "n": len(churn_byids),
            "budget": None,
        }

    return report


def print_report(report: dict) -> None:
    count = report["count"]
    mode = "PERF" if _perf_mode() else "CI"
    print(f"\n=== large-library HTTP smoke  count={count}  mode={mode} ===")
    print(f"{'metric':<22} {'ms':>8} {'avg':>8} {'max':>8} {'budget':>8} {'n':>4}")
    for name, row in report["samples"].items():
        budget = row.get("budget")
        btxt = "-" if budget is None else f"{budget:.0f}"
        flag = ""
        if budget is not None and row["ms"] > budget:
            flag = " FAIL"
        print(
            f"{name:<22} {row['ms']:>8.1f} {row.get('avg', row['ms']):>8.1f} "
            f"{row.get('max', row['ms']):>8.1f} {btxt:>8} {row.get('n', 1):>4}{flag}"
        )
    if report["failures"]:
        print("\nFailures:")
        for line in report["failures"]:
            print(" -", line)
    else:
        print("\nAll budgets OK.")
    ensure_row = report["samples"].get("by_ids_20_with_ensure")
    if ensure_row and ensure_row["ms"] > 500:
        print(
            f"\nWatch: by_ids_20_with_ensure={ensure_row['ms']:.0f}ms "
            "(ensure_library still expensive even when disk_libs is mounted)."
        )
    print(
        "\nNote: this harness does NOT walk a real 6000-file disk or run ffmpeg.\n"
        "It catches SQL/API/index/UI-churn regressions. Real scan+thumb bulk still\n"
        "needs a fixture directory or a one-off VG_PERF live pass."
    )


class LargeLibraryHttpSmokeTests(unittest.TestCase):
    def test_six_thousand_http_hot_paths_within_budget(self) -> None:
        # CI keeps a smaller catalog so discover stays under a minute; CLI
        # default remains 6000 via `python tests/test_large_library_http_smoke.py`.
        count = int(os.environ.get("VG_SMOKE_COUNT") or 2000)
        report = run_suite(count)
        if not report["ok"]:
            print_report(report)
        self.assertTrue(
            report["ok"],
            msg="; ".join(report["failures"]) or "budget failure",
        )


class EnsureLibraryMemoryHitTests(unittest.TestCase):
    def test_ensure_skips_archive_when_disk_libs_already_ready(self) -> None:
        from vg.disk_libs import ensure_library

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root_s = str(root.resolve())
            item = {"id": "abc", "_lib_root": root_s, "root": root_s}
            old = {k: STATE.get(k) for k in ("root", "videos", "by_id", "disk_libs")}
            STATE["root"] = root
            STATE["videos"] = [item]
            STATE["by_id"] = {"abc": item}
            STATE["disk_libs"] = {
                root_s: {"by_id": {"abc": item}, "updated": time.time()}
            }
            try:
                with mock.patch("vg.disk_libs.archive_current_library") as arch:
                    self.assertTrue(ensure_library(root_s))
                    arch.assert_not_called()
            finally:
                STATE.update(old)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="6000+ library HTTP smoke / stress")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument(
        "--perf",
        action="store_true",
        help="Use tight VG_PERF budgets (same as VG_PERF=1)",
    )
    args = parser.parse_args(argv)
    if args.perf:
        os.environ["VG_PERF"] = "1"
    report = run_suite(max(100, int(args.count)))
    print_report(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
