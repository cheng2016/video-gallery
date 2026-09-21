# -*- coding: utf-8 -*-
"""Live 6000+ library simulation on this PC (real files + scan + optional thumbs).

Unlike the synthetic SQLite-only smoke test, this builds a fixture tree of
**distinct** video files (unique size + fingerprint), runs the production
scan path, optionally generates thumbnails with the machine's ffmpeg, and
times the same HTTP hot paths the UI uses — including mid-scan churn.

Always run two scenarios by default:
  1. **cold** — wipe catalog/tree/dup/thumb caches, full scan of every drive
  2. **warm** — keep those caches, simulate restart, incremental scan again
Compare timings and fail on catalog shrink / unexpected duplicates.

Examples (from repo root)::

    set PYTHONPATH=D:\\video-gallery
    python tests/bench_large_library_live.py --count 6000 --shape cd --thumbs 20
    python tests/bench_large_library_live.py --seed-from "C:\\Users\\...\\1210_TCL_HDR_60F(HDR10+).mp4" --count 6000

``--shape cd`` builds twin ``C_drive`` / ``D_drive`` trees (Users/WeChat/Movies/…)
plus thousands of empty dirs so the walker sees C:/D:-like fan-out. Fixture +
cache live on ``D:\\vg_bench_fixture``. Bodies are unique per leaf (not
hardlinks of one 454MB file). A large seed is only used to derive one short
template and for optional real-HDR probe/thumb timing.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Allow `python tests/bench_....py` without installing the package.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from vg import web
from vg.media import find_ffmpeg
from vg.scan import scan_videos
from vg.state import STATE


DEFAULT_COUNT = 6000
DEFAULT_TCL_SEED = Path(
    r"C:\Users\ex_zhenjia.cheng\Downloads\1210_TCL_HDR_60F(HDR10+).mp4"
)
# Fixture + cache live on D: — never materialize under AppData / C:.
DEFAULT_FIXTURE_DIR = Path(r"D:\vg_bench_fixture")
_LARGE_SEED_BYTES = 10 * 1024 * 1024
_MIN_UNIQUE_BODY = 100 * 1024
# Per-leaf target size. ~413KB was too small for meaningful I/O/probe load;
# 5MB × 6000 ≈ 30GB on D: (still far below copying 454MB×N).
DEFAULT_LEAF_MB = 5.0


def _ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0


def _p95(samples: list[float]) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    idx = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
    return ordered[idx]


def _print_row(name: str, ms: float, note: str = "") -> None:
    extra = f"  {note}" if note else ""
    print(f"  {name:<28} {ms:>10.1f} ms{extra}")


def resolve_ffmpeg(explicit: str | None = None) -> str | None:
    if explicit and Path(explicit).is_file():
        return str(Path(explicit))
    found = find_ffmpeg()
    if found:
        return str(found)
    # Common local installs (Cursor/Doubao agent workspace, winget, etc.)
    candidates = [
        Path(os.environ.get("FFMPEG", "")),
        Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
    ]
    for c in candidates:
        if c and c.is_file():
            return str(c)
    which = shutil.which("ffmpeg")
    return which


def make_seed_mp4(ffmpeg: str, out: Path) -> float:
    """Create a real H.264/AAC mp4 above MIN_VIDEO_FILE_BYTES (100KB)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.is_file() and out.stat().st_size >= 100 * 1024:
        return 0.0
    t0 = time.perf_counter()
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=1280x720:rate=24:duration=5",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100:duration=5",
        "-c:v",
        "libx264",
        "-b:v",
        "800k",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        "-movflags",
        "+faststart",
        str(out),
    ]
    subprocess.run(cmd, check=True)
    if out.stat().st_size < 100 * 1024:
        # Last resort: append zero padding so scan accepts the file; ffmpeg
        # still decodes the leading valid stream for thumbs.
        need = 100 * 1024 - out.stat().st_size
        with out.open("ab") as fh:
            fh.write(b"\x00" * need)
    return _ms(t0)


def _same_volume(a: Path, b: Path) -> bool:
    try:
        return os.path.splitdrive(str(a.resolve()))[0].casefold() == os.path.splitdrive(
            str(b.resolve())
        )[0].casefold()
    except OSError:
        return False


def _hardlink_or_fail(seed: Path, dest: Path) -> None:
    """Hardlink only — scanner skips symlinks; never copy leaf bodies."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return
    try:
        os.link(seed, dest)
    except OSError as exc:
        size_mb = seed.stat().st_size / (1024 * 1024)
        raise RuntimeError(
            f"hardlink failed ({exc}). Seed is {size_mb:.1f}MB — fixture must be "
            f"on the same drive as the seed pool ({seed}). Refusing to copy leaves."
        ) from exc


def ensure_compact_template(
    master: Path,
    dest: Path,
    ffmpeg: str,
    *,
    min_bytes: int,
) -> Path:
    """Playable mp4 template on the fixture drive (base for unique leaves)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size >= min_bytes:
        return dest
    if dest.exists():
        try:
            dest.unlink()
        except OSError:
            pass
    if master.is_file() and _MIN_UNIQUE_BODY <= master.stat().st_size < _LARGE_SEED_BYTES:
        if master.stat().st_size >= min_bytes:
            shutil.copy2(master, dest)
            return dest
    print(
        f"    … deriving leaf template from {master.name} "
        f"({master.stat().st_size / (1024 * 1024):.0f} MB → ≥{min_bytes / (1024 * 1024):.1f} MB)"
    )
    # Longer / higher-bitrate clip so probe+thumb touch real media weight,
    # not a 400KB stub. Remaining leaf size comes from unique padding.
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        "0",
        "-i",
        str(master),
        "-t",
        "12",
        "-vf",
        "scale=1280:-2",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-b:v",
        "2500k",
        "-an",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    try:
        subprocess.run(cmd, check=True)
    except (OSError, subprocess.CalledProcessError):
        make_seed_mp4(ffmpeg, dest)
    if not dest.is_file() or dest.stat().st_size < _MIN_UNIQUE_BODY:
        make_seed_mp4(ffmpeg, dest)
    # If encode is still under the floor, pad once on the template (leaves
    # will pad further to their own targets).
    if dest.stat().st_size < min_bytes:
        need = min_bytes - dest.stat().st_size
        with dest.open("ab") as fh:
            # Chunked write to avoid a giant byte() alloc.
            chunk = b"\x00" * (1024 * 1024)
            while need > 0:
                n = min(need, len(chunk))
                fh.write(chunk[:n])
                need -= n
    print(
        f"    … leaf template ready  {dest.stat().st_size / (1024 * 1024):.2f} MB"
    )
    return dest


def _write_unique_mp4(
    template: bytes,
    dest: Path,
    index: int,
    *,
    target_bytes: int,
) -> None:
    """Write a distinct mp4: unique fingerprint + size around ``target_bytes``.

    Duplicate detection groups by size then file_sig (head+middle+tail samples).
    Hardlinking one body 6000× makes every row a duplicate — not a real library.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    body = bytearray(template)
    tag = index.to_bytes(4, "big") + hashlib.sha1(f"vg-bench-{index}".encode()).digest()[:12]
    # Touch head / middle / near-tail so fingerprint samples diverge.
    for pos in (64, max(64, len(body) // 2), max(64, len(body) - 96)):
        end = min(len(body), pos + len(tag))
        body[pos:end] = tag[: end - pos]
    # Size band: target ± up to ~1MB so the library is not one flat size.
    want = max(len(body) + 32, int(target_bytes) + (index % 1024) * 1024)
    pad_len = max(32, want - len(body))
    # Unique trailer (changes size + tail fingerprint sample).
    trailer = index.to_bytes(8, "big") + bytes([index % 256]) * min(64, pad_len)
    pad_len -= len(trailer)
    with dest.open("wb") as fh:
        fh.write(bytes(body))
        fh.write(trailer)
        chunk = bytes([ (index + 17) % 256 ]) * (1024 * 1024)
        while pad_len > 0:
            n = min(pad_len, len(chunk))
            fh.write(chunk[:n])
            pad_len -= n


def _fixture_leaves_are_unique(
    leaves: list[Path],
    count: int,
    *,
    min_avg_bytes: int,
) -> bool:
    """True when leaves look like distinct, adequately sized files."""
    if len(leaves) < int(count * 0.95):
        return False
    sample = leaves[: min(300, len(leaves))]
    try:
        sizes = [p.stat().st_size for p in sample]
        size_set = set(sizes)
        # Hardlink pool → 1 size; unique trailer scheme → many sizes.
        if len(size_set) < max(20, len(sample) // 10):
            return False
        avg = sum(sizes) / len(sizes)
        if avg < min_avg_bytes * 0.85:
            return False
        # Reject leftover hardlink fixtures (nlink >> 1).
        nlinks = [p.stat().st_nlink for p in sample[:20]]
        if nlinks and (sum(nlinks) / len(nlinks)) > 2.5:
            return False
    except OSError:
        return False
    return True


def _purge_legacy_seed_pool(pool_dir: Path) -> None:
    """Drop old 433MB×N pool copies — unique leaves no longer need them."""
    if not pool_dir.is_dir():
        return
    freed = 0
    for p in pool_dir.glob("seed_*.mp4"):
        try:
            sz = p.stat().st_size
            p.unlink()
            freed += sz
        except OSError:
            pass
    if freed:
        print(f"    … removed legacy seed pool  {freed / (1024 * 1024):.0f} MB")


def prepare_unique_template(
    base: Path,
    master: Path,
    ffmpeg: str,
    *,
    leaf_bytes: int,
) -> bytes:
    """Build/read the template used to stamp unique leaves on D:."""
    pool_dir = base / "_seeds"
    pool_dir.mkdir(parents=True, exist_ok=True)
    _purge_legacy_seed_pool(pool_dir)
    # Real encoded body should be a meaningful fraction of the leaf target.
    min_template = max(_MIN_UNIQUE_BODY, min(leaf_bytes, int(leaf_bytes * 0.45)))
    template_path = ensure_compact_template(
        master, pool_dir / "template.mp4", ffmpeg, min_bytes=min_template
    )
    data = template_path.read_bytes()
    if len(data) < _MIN_UNIQUE_BODY:
        raise RuntimeError(f"compact template too small: {template_path}")
    est_total_gb = (leaf_bytes * 6000) / (1024 ** 3)
    print(
        f"    … unique-leaf template  {len(data) / (1024 * 1024):.2f} MB  "
        f"(target leaf ≈{leaf_bytes / (1024 * 1024):.1f} MB, "
        f"~{est_total_gb:.1f} GB for 6000 files)"
    )
    return data


def materialize_cd_fixture(
    base: Path,
    seed: Path,
    count: int,
    ffmpeg: str,
    *,
    empty_dirs: int = 4000,
    leaf_mb: float = DEFAULT_LEAF_MB,
) -> tuple[Path, Path, float]:
    """Build twin roots with ``count`` distinct mp4 files (unique size/sig)."""
    t0 = time.perf_counter()
    if not seed.is_file():
        raise FileNotFoundError(seed)
    leaf_bytes = max(_MIN_UNIQUE_BODY, int(float(leaf_mb) * 1024 * 1024))
    c_root = base / "C_drive"
    d_root = base / "D_drive"

    build_cd_shaped_empty_trees(c_root, d_root, empty_dirs)
    template = prepare_unique_template(base, seed, ffmpeg, leaf_bytes=leaf_bytes)

    n_c = count // 2
    n_d = count - n_c
    existing = list(c_root.rglob("*.mp4")) + list(d_root.rglob("*.mp4"))
    if len(existing) >= count and _fixture_leaves_are_unique(
        existing, count, min_avg_bytes=leaf_bytes
    ):
        return c_root, d_root, _ms(t0)
    for p in existing:
        try:
            p.unlink()
        except OSError:
            pass

    for i in range(n_c):
        if i % 4 == 0:
            dest_dir = c_root / "Users/ex_zhenjia.cheng/Videos" / f"lib_{i // 100:02d}"
        elif i % 4 == 1:
            dest_dir = c_root / "Users/ex_zhenjia.cheng/Downloads" / "clips" / f"y{i // 200:02d}"
        elif i % 4 == 2:
            dest_dir = (
                c_root
                / "Users/ex_zhenjia.cheng/Documents/xwechat_files/wxid_bench/msg/video/2026-09"
                / f"m{i // 150:02d}"
            )
        else:
            dest_dir = c_root / "Users/Public/Videos" / f"pub_{i // 120:02d}"
        dest = dest_dir / f"clip-c-{i:05d}.mp4"
        _write_unique_mp4(template, dest, i, target_bytes=leaf_bytes)
        if (i + 1) % 1000 == 0:
            print(f"    … C_drive unique files {i + 1}/{n_c}")

    for i in range(n_d):
        if i % 5 == 0:
            dest_dir = d_root / "Movies/华语" / f"batch_{i // 100:02d}"
        elif i % 5 == 1:
            dest_dir = d_root / "Movies/欧美" / f"batch_{i // 100:02d}"
        elif i % 5 == 2:
            dest_dir = d_root / "电视剧/国产" / f"s{i // 80:02d}"
        elif i % 5 == 3:
            dest_dir = d_root / "video-gallery" / "inbox" / f"g{i // 90:02d}"
        else:
            dest_dir = d_root / "备份/2025" / f"dump_{i // 200:02d}"
        dest = dest_dir / f"clip-d-{i:05d}.mp4"
        _write_unique_mp4(template, dest, n_c + i, target_bytes=leaf_bytes)
        if (i + 1) % 1000 == 0:
            print(f"    … D_drive unique files {i + 1}/{n_d}")

    return c_root, d_root, _ms(t0)


def materialize_fixture(
    fixture_root: Path,
    seed: Path,
    count: int,
    ffmpeg: str,
    *,
    folders: tuple[str, ...] = ("电影", "综艺", "纪录片"),
    leaf_mb: float = DEFAULT_LEAF_MB,
) -> float:
    """Place ``count`` distinct video files under fixture_root."""
    t0 = time.perf_counter()
    fixture_root.mkdir(parents=True, exist_ok=True)
    leaf_bytes = max(_MIN_UNIQUE_BODY, int(float(leaf_mb) * 1024 * 1024))
    template = prepare_unique_template(
        fixture_root.parent, seed, ffmpeg, leaf_bytes=leaf_bytes
    )
    existing = [p for p in fixture_root.rglob("*.mp4") if p.name != "_seed.mp4"]
    if len(existing) >= count and _fixture_leaves_are_unique(
        existing, count, min_avg_bytes=leaf_bytes
    ):
        return _ms(t0)
    for p in existing:
        try:
            p.unlink()
        except OSError:
            pass

    for i in range(count):
        folder = folders[i % len(folders)]
        dest_dir = fixture_root / folder / f"batch_{i // 200:03d}"
        dest = dest_dir / f"clip-{i:05d}.mp4"
        _write_unique_mp4(template, dest, i, target_bytes=leaf_bytes)
        if (i + 1) % 1000 == 0:
            print(f"    … materialized unique {i + 1}/{count}")
    return _ms(t0)


def build_cd_shaped_empty_trees(c_root: Path, d_root: Path, empty_dirs: int) -> int:
    """Create C:\\ / D:\\-like skeletons so the walker has real fan-out."""
    c_skeleton = [
        "Users/ex_zhenjia.cheng/Documents",
        "Users/ex_zhenjia.cheng/Downloads",
        "Users/ex_zhenjia.cheng/Videos",
        "Users/ex_zhenjia.cheng/Desktop",
        "Users/ex_zhenjia.cheng/Documents/xwechat_files/wxid_bench/msg/video/2026-09",
        "Users/Public/Videos",
        "Program Files/Common Files",
        "Program Files/VideoGallery Bench",
        "Program Files (x86)/Common Files",
        "Windows/System32/drivers",
        "Windows/SysWOW64",
        "Windows/Fonts",
        "Drivers/Display",
        "Drivers/Audio",
        "PerfLogs",
        "inetpub/wwwroot",
    ]
    d_skeleton = [
        "video-gallery",
        "Movies/华语",
        "Movies/欧美",
        "Movies/纪录片",
        "电视剧/国产",
        "电视剧/日韩",
        "备份/2024",
        "备份/2025",
        "Tools",
        "ISO",
        "SteamLibrary/steamapps/common",
        "Projects/repo_a",
        "Projects/repo_b",
    ]
    made = 0
    for rel in c_skeleton:
        (c_root / rel).mkdir(parents=True, exist_ok=True)
        made += 1
    for rel in d_skeleton:
        (d_root / rel).mkdir(parents=True, exist_ok=True)
        made += 1
    # Extra empty leaves: walk cost without counting as videos.
    for i in range(empty_dirs):
        if i % 2 == 0:
            p = c_root / "Users" / "ex_zhenjia.cheng" / "AppData" / "Local" / "junk" / f"n{i:04d}" / "sub"
        else:
            p = d_root / "empty_tree" / f"bucket_{i // 50:03d}" / f"leaf_{i:04d}"
        p.mkdir(parents=True, exist_ok=True)
        made += 1
    return made


def _state_snapshot() -> dict:
    keys = (
        "root",
        "mounted_roots",
        "lan_share",
        "lib_gen",
        "videos",
        "by_id",
        "by_thumb_id",
        "facets",
        "tree",
        "disk_libs",
        "scanning",
        "updating",
        "meta_progress",
        "scan_root",
        "cache_dir",
        "ffmpeg",
        "scan_live",
        "scan_progress",
        "thumb_progress",
    )
    return {k: STATE.get(k) for k in keys}


def mount_fixture(roots: list[Path], cache: Path, ffmpeg: str | None) -> None:
    from vg.roots import set_mounted_roots

    resolved = [str(r.resolve()) for r in roots]
    primary = roots[0]
    STATE.update(
        {
            "root": primary,
            "lan_share": False,
            "lib_gen": int(STATE.get("lib_gen") or 0),
            "videos": [],
            "by_id": {},
            "by_thumb_id": {},
            "facets": {},
            "tree": {},
            "disk_libs": {},
            "scanning": False,
            "updating": False,
            "meta_progress": "",
            "scan_root": "",
            "cache_dir": cache,
            "ffmpeg": ffmpeg,
            "scan_live": [],
            "scan_progress": "",
            "thumb_progress": "",
        }
    )
    set_mounted_roots(resolved, primary=resolved[0])
    web.invalidate_response_caches()


def time_get(client, path: str, *, ok: set[int] | None = None) -> float:
    allowed = ok or {200}
    t0 = time.perf_counter()
    resp = client.get(path)
    elapsed = _ms(t0)
    if resp.status_code not in allowed:
        raise RuntimeError(f"GET {path} -> {resp.status_code}")
    return elapsed


def time_post(client, path: str, payload: dict) -> float:
    t0 = time.perf_counter()
    resp = client.post(path, json=payload)
    elapsed = _ms(t0)
    if resp.status_code >= 500:
        raise RuntimeError(f"POST {path} -> {resp.status_code}")
    return elapsed


def http_idle_suite(client, root_s: str, count: int) -> dict[str, float]:
    # Empty lib = all disks (multi-root view).
    lib_q = ""
    ids = [v["id"] for v in (STATE.get("videos") or [])[:100]]
    hints = {
        i: {"root": (v.get("_lib_root") or v.get("root") or root_s)}
        for i, v in zip(ids, (STATE.get("videos") or [])[:100])
    }
    out: dict[str, float] = {}
    web.invalidate_response_caches()
    out["status"] = statistics.mean([time_get(client, "/api/status") for _ in range(5)])
    out["tree_miss"] = time_get(client, "/api/tree")
    out["tree_hit"] = statistics.mean([time_get(client, "/api/tree") for _ in range(3)])
    out["videos_offset0"] = time_get(
        client,
        "/api/videos?sort=mtime_desc&view=flat&offset=0&limit=20",
    )
    deep = max(0, count - 40)
    out["videos_deep"] = time_get(
        client,
        f"/api/videos?sort=mtime_desc&view=flat&offset={deep}&limit=20",
    )
    if ids:
        out["by_ids_20"] = statistics.mean(
            [
                time_post(
                    client,
                    "/api/videos-by-ids",
                    {"ids": ids[:20], "hints": {k: hints[k] for k in ids[:20]}},
                )
                for _ in range(3)
            ]
        )
        out["by_ids_100"] = statistics.mean(
            [
                time_post(
                    client,
                    "/api/videos-by-ids",
                    {"ids": ids[:100], "hints": hints},
                )
                for _ in range(3)
            ]
        )
        vid0 = ids[0]
        root0 = hints[vid0]["root"]
        out["thumb"] = statistics.mean(
            [
                time_get(
                    client,
                    f"/thumb/{vid0}?v=1&defer=1&root={root0}",
                    ok={200, 503},
                )
                for _ in range(3)
            ]
        )
    return out


def mid_scan_churn(client, root_s: str, duration_s: float = 8.0) -> dict[str, float]:
    """Hammer status/videos/by-ids while a scan thread is (or was) running."""
    videos = list(STATE.get("videos") or [])
    ids = [v["id"] for v in videos[:20]]
    hints = {
        i: {"root": (v.get("_lib_root") or v.get("root") or root_s)}
        for i, v in zip(ids, videos[:20])
    }
    status_s: list[float] = []
    videos_s: list[float] = []
    byids_s: list[float] = []
    deadline = time.time() + duration_s

    def one_round() -> None:
        status_s.append(time_get(client, "/api/status"))
        videos_s.append(
            time_get(
                client,
                "/api/videos?sort=mtime_desc&view=flat&offset=0&limit=20",
            )
        )
        if ids:
            byids_s.append(
                time_post(
                    client,
                    "/api/videos-by-ids",
                    {"ids": ids, "hints": hints},
                )
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = []
        while time.time() < deadline:
            futs.append(pool.submit(one_round))
            time.sleep(0.15)
        for fut in as_completed(futs):
            fut.result()
    return {
        "churn_status_p95": _p95(status_s),
        "churn_videos_p95": _p95(videos_s),
        "churn_byids_p95": _p95(byids_s),
        "churn_rounds": float(len(status_s)),
    }


def wipe_scan_caches(roots: list[Path]) -> None:
    """Drop catalog / tree / dup-sig / thumbs so the next scan is a true first run."""
    from vg.cache import ensure_cache_dir
    from vg.config import VGDATA_DIR

    n = 0
    for root in roots:
        cache = ensure_cache_dir(root)
        if cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)
            n += 1
        cache.mkdir(parents=True, exist_ok=True)
    unified = VGDATA_DIR / "unified"
    if unified.is_dir():
        shutil.rmtree(unified, ignore_errors=True)
        n += 1
    dup = VGDATA_DIR / "duplicate_signatures_cache.json"
    if dup.is_file():
        dup.unlink()
        n += 1
    print(f"    … wiped {n} cache locations  ({VGDATA_DIR})")


def library_health() -> dict[str, int]:
    videos = list(STATE.get("videos") or [])
    return {
        "videos": len(videos),
        "dup_rows": sum(1 for v in videos if v.get("dup")),
        "bad_rows": sum(1 for v in videos if v.get("bad")),
        "lib_gen": int(STATE.get("lib_gen") or 0),
    }


def load_from_disk_caches(roots: list[Path]) -> float:
    """Second-launch path: catalogs already on disk, memory is empty."""
    from vg.disk_libs import load_library_from_index
    from vg.roots import publish_unified_library

    t0 = time.perf_counter()
    per_root: list[str] = []
    for root in roots:
        one = time.perf_counter()
        load_library_from_index(root)
        per_root.append(f"{root.name}={_ms(one):.1f}")
    loaded = time.perf_counter()
    publish_unified_library(heavy=True, reason="bench_warm_startup", refresh_tree=True)
    published = time.perf_counter()
    print(
        f"[PERF] startup_load_split load_ms={(loaded - t0) * 1000.0:.1f} "
        f"publish_ms={(published - loaded) * 1000.0:.1f} "
        f"per_root={','.join(per_root)}",
        flush=True,
    )
    return _ms(t0)


def run_thumb_sample(
    *,
    cache: Path,
    ffmpeg: str,
    thumbs: str,
    seed_path: Path,
    primary_s: str,
    client,
    force: bool,
) -> dict[str, float]:
    from vg.cache import thumb_path
    from vg.http_helpers import resolve_local_path
    from vg.media import make_thumbnail, probe_media_info

    metrics: dict[str, float] = {}
    n_videos = len(STATE.get("videos") or [])
    if thumbs == "0":
        return metrics
    thumb_n = n_videos if thumbs == "all" else max(0, int(thumbs))
    if not thumb_n:
        return metrics
    print(f"  -- probe+thumb sample={thumb_n} force={force} --")
    videos = list(STATE.get("videos") or [])
    sample = videos[:thumb_n]
    probe_ms: list[float] = []
    thumb_ms_list: list[float] = []
    ok_probe = ok_thumb = 0
    sizes: set[int] = set()
    for v in sample:
        path = resolve_local_path(v)
        if path is None or not Path(path).is_file():
            print(f"    skip {v.get('id')}: unresolved rel={v.get('rel')}")
            continue
        path = Path(path)
        size_b = path.stat().st_size
        sizes.add(size_b)
        size_mb = size_b / (1024 * 1024)
        t0 = time.perf_counter()
        info = probe_media_info(
            ffmpeg,
            path,
            include_duration=True,
            include_audio=True,
            include_video_meta=True,
        )
        pms = _ms(t0)
        probe_ms.append(pms)
        if info.get("ok"):
            ok_probe += 1
        out = thumb_path(cache, str(v["id"]))
        t1 = time.perf_counter()
        made = make_thumbnail(ffmpeg, path, out, force=force, burst=True)
        tms = _ms(t1)
        thumb_ms_list.append(tms)
        if made:
            ok_thumb += 1
            v["has_thumb"] = True
        print(
            f"    sample {v['id'][:8]}  {size_mb:.2f}MB  "
            f"probe={pms:.0f}ms  thumb={tms:.0f}ms  ok={made}"
        )
    print(f"    unique sizes in sample: {len(sizes)}/{len(sample)}")
    if probe_ms:
        metrics["lib_probe_mean"] = statistics.mean(probe_ms)
        _print_row(
            "lib_probe_mean",
            metrics["lib_probe_mean"],
            f"ok={ok_probe}/{len(probe_ms)} p95={_p95(probe_ms):.0f}ms",
        )
    if thumb_ms_list:
        metrics["lib_thumb_mean"] = statistics.mean(thumb_ms_list)
        _print_row(
            "lib_thumb_mean",
            metrics["lib_thumb_mean"],
            f"ok={ok_thumb}/{len(thumb_ms_list)} p95={_p95(thumb_ms_list):.0f}ms",
        )
    if force and seed_path.is_file() and seed_path.stat().st_size >= _LARGE_SEED_BYTES:
        print(
            f"  -- real HDR probe+thumb "
            f"({seed_path.stat().st_size / (1024 * 1024):.0f} MB, single file) --"
        )
        t0 = time.perf_counter()
        hdr_info = probe_media_info(
            ffmpeg,
            seed_path,
            include_duration=True,
            include_audio=True,
            include_video_meta=True,
        )
        hdr_pms = _ms(t0)
        hdr_out = cache / "_bench_hdr_sample.vgt"
        t1 = time.perf_counter()
        hdr_ok = make_thumbnail(ffmpeg, seed_path, hdr_out, force=True, burst=True)
        hdr_tms = _ms(t1)
        metrics["hdr_probe"] = hdr_pms
        metrics["hdr_thumb"] = hdr_tms
        _print_row("hdr_probe", hdr_pms, f"ok={bool(hdr_info.get('ok'))}")
        _print_row("hdr_thumb", hdr_tms, f"ok={hdr_ok}")
    if sample:
        vid0 = sample[0]["id"]
        root0 = sample[0].get("_lib_root") or primary_s
        thumb_http = statistics.mean(
            [
                time_get(
                    client,
                    f"/thumb/{vid0}?v=1&defer=1&root={root0}",
                    ok={200, 503},
                )
                for _ in range(3)
            ]
        )
        metrics["thumb_http_after"] = thumb_http
        _print_row("thumb_http_after", thumb_http)
    return metrics


def run_phase(
    *,
    name: str,
    roots: list[Path],
    cache: Path,
    ffmpeg: str,
    count: int,
    thumbs: str,
    seed_path: Path,
    incremental: bool,
    load_disk_first: bool,
) -> dict[str, float]:
    print(
        f"\n========== phase {name}  "
        f"incremental={incremental}  load_disk={load_disk_first} =========="
    )
    mount_fixture(roots, cache, ffmpeg)
    client = web.app.test_client()
    primary_s = str(roots[0].resolve())
    metrics: dict[str, float] = {}

    if load_disk_first:
        load_ms = load_from_disk_caches(roots)
        h = library_health()
        metrics["startup_load_catalogs"] = load_ms
        metrics["startup_videos"] = float(h["videos"])
        _print_row(
            "startup_load_catalogs",
            load_ms,
            f"videos={h['videos']} dup={h['dup_rows']}",
        )

    total_scan = 0.0
    kind = "inc" if incremental else "full"
    for root in roots:
        t0 = time.perf_counter()
        scan_videos(root, do_thumbs=False, incremental=incremental, quiet=False)
        one = _ms(t0)
        total_scan += one
        h = library_health()
        label = f"scan_{kind}[{root.name}]"
        metrics[label] = one
        _print_row(
            label,
            one,
            f"videos={h['videos']} dup={h['dup_rows']}",
        )
    h = library_health()
    metrics["scan_total"] = total_scan
    metrics["videos"] = float(h["videos"])
    metrics["dup_rows"] = float(h["dup_rows"])
    metrics["bad_rows"] = float(h["bad_rows"])
    _print_row(
        "scan_total",
        total_scan,
        f"videos={h['videos']} dup={h['dup_rows']} bad={h['bad_rows']}",
    )
    if h["videos"] < count * 0.95:
        print(f"ERROR: {name} scan found {h['videos']}, expected ~{count}")

    idle = http_idle_suite(client, primary_s, int(h["videos"] or count))
    print(f"  -- idle HTTP ({name}) --")
    for k, v in idle.items():
        _print_row(k, v)
        metrics[k] = v

    target = roots[-1]
    print(f"  -- mid-scan HTTP churn ({name}, rescan {target.name}) --")
    scan_err: list[BaseException] = []

    def _rescan() -> None:
        try:
            scan_videos(target, do_thumbs=False, incremental=True, quiet=False)
        except BaseException as exc:  # noqa: BLE001
            scan_err.append(exc)

    th = threading.Thread(target=_rescan, name=f"bench-rescan-{name}", daemon=True)
    th.start()
    time.sleep(0.3)
    churn = mid_scan_churn(client, primary_s, duration_s=8.0)
    th.join(timeout=900)
    if scan_err:
        raise scan_err[0]
    for k, v in churn.items():
        metrics[k] = v
        if k == "churn_rounds":
            print(f"  {k:<28} {int(v):>10}")
        else:
            _print_row(k, v)

    after = library_health()
    metrics["videos_after_churn"] = float(after["videos"])
    metrics["dup_after_churn"] = float(after["dup_rows"])
    if after["videos"] < h["videos"] * 0.95:
        print(
            f"ERROR: {name} catalog shrink after churn "
            f"{h['videos']} -> {after['videos']}"
        )

    metrics.update(
        run_thumb_sample(
            cache=cache,
            ffmpeg=ffmpeg,
            thumbs=thumbs,
            seed_path=seed_path,
            primary_s=primary_s,
            client=client,
            force=not incremental,
        )
    )
    return metrics


def compare_phases(cold: dict[str, float], warm: dict[str, float], count: int) -> list[str]:
    print("\n========== cold vs warm ==========")
    flags: list[str] = []
    timing_keys = [
        "startup_load_catalogs",
        "scan_total",
        "status",
        "tree_miss",
        "tree_hit",
        "videos_offset0",
        "videos_deep",
        "by_ids_20",
        "by_ids_100",
        "thumb",
        "churn_status_p95",
        "churn_videos_p95",
        "churn_byids_p95",
        "lib_probe_mean",
        "lib_thumb_mean",
        "thumb_http_after",
    ]
    count_keys = [
        "videos",
        "dup_rows",
        "bad_rows",
        "startup_videos",
        "videos_after_churn",
        "dup_after_churn",
        "churn_rounds",
    ]
    for k in count_keys:
        if k not in cold and k not in warm:
            continue
        c = cold.get(k)
        w = warm.get(k)
        cleft = f"{c:.0f}" if c is not None else "-"
        wright = f"{w:.0f}" if w is not None else "-"
        print(f"  {k:<28} {cleft:>10}  ->  {wright:>10}")
    for k in timing_keys:
        if k not in cold and k not in warm:
            continue
        c = cold.get(k)
        w = warm.get(k)
        if c is not None and w is not None and w > 0.05:
            ratio = c / w
            print(f"  {k:<28} {c:>10.1f}  ->  {w:>10.1f} ms  ({ratio:.1f}x)")
        else:
            cleft = f"{c:.1f}" if c is not None else "-"
            wright = f"{w:.1f}" if w is not None else "-"
            print(f"  {k:<28} {cleft:>10}  ->  {wright:>10} ms")

    def _err(msg: str) -> None:
        flags.append("ERROR " + msg)

    def _warn(msg: str) -> None:
        flags.append("WARN  " + msg)

    if cold.get("videos", 0) < count * 0.95:
        _err(f"cold scan found {cold.get('videos')} expected ~{count}")
    if warm.get("videos", 0) < count * 0.95:
        _err(f"warm scan found {warm.get('videos')} expected ~{count}")
    if warm.get("videos", 0) and cold.get("videos", 0):
        if warm["videos"] < cold["videos"] * 0.95:
            _err(f"catalog shrink cold={cold['videos']:.0f} warm={warm['videos']:.0f}")
    if warm.get("videos_after_churn") and warm.get("videos"):
        if warm["videos_after_churn"] < warm["videos"] * 0.95:
            _err(
                f"warm shrink after churn "
                f"{warm['videos']:.0f} -> {warm['videos_after_churn']:.0f}"
            )
    if cold.get("dup_rows") or warm.get("dup_rows"):
        _err(
            f"dup_rows cold={cold.get('dup_rows', 0):.0f} "
            f"warm={warm.get('dup_rows', 0):.0f} (leaves are unique files)"
        )
    if "startup_videos" in warm and warm["startup_videos"] < count * 0.95:
        _err(
            f"warm startup loaded {warm['startup_videos']:.0f} videos, "
            f"expected ~{count} from catalog cache"
        )
    if cold.get("scan_total") and warm.get("scan_total"):
        if warm["scan_total"] > cold["scan_total"] * 1.1:
            _warn(
                f"warm scan slower than cold "
                f"{warm['scan_total']:.0f} > {cold['scan_total']:.0f} ms"
            )
        elif warm["scan_total"] > cold["scan_total"] * 0.7:
            _warn(
                f"warm scan only {cold['scan_total'] / max(warm['scan_total'], 1):.1f}x "
                f"faster (cache should skip fingerprint I/O)"
            )
    if warm.get("tree_miss") and cold.get("tree_miss"):
        if warm["tree_miss"] > max(400.0, cold["tree_miss"] * 1.5):
            _warn(f"warm tree_miss {warm['tree_miss']:.0f}ms slower than cold")
    if warm.get("churn_videos_p95", 0) > 1500:
        _warn(f"warm churn_videos_p95 {warm['churn_videos_p95']:.0f}ms")
    if cold.get("churn_videos_p95", 0) > 1500:
        _warn(f"cold churn_videos_p95 {cold['churn_videos_p95']:.0f}ms")
    return flags


def run_live(
    *,
    count: int,
    thumbs: str,
    seed_from: str | None,
    fixture_dir: str | None,
    ffmpeg_bin: str | None,
    keep: bool,
    shape: str,
    empty_dirs: int,
    leaf_mb: float,
    phases: str,
) -> int:
    ffmpeg = resolve_ffmpeg(ffmpeg_bin)
    if not ffmpeg:
        print("ERROR: ffmpeg not found. Pass --ffmpeg PATH")
        return 2
    print(f"ffmpeg: {ffmpeg}")

    seed_path = Path(seed_from) if seed_from else DEFAULT_TCL_SEED
    if shape == "cd" and not seed_from:
        seed_path = DEFAULT_TCL_SEED

    if fixture_dir:
        base = Path(fixture_dir)
    elif shape == "cd":
        base = DEFAULT_FIXTURE_DIR
    else:
        base = Path(tempfile.mkdtemp(prefix="vg_live_bench_"))
    base.mkdir(parents=True, exist_ok=True)
    cleanup_tmp = fixture_dir is None and shape != "cd" and not keep

    cache = base / "cache"
    cache.mkdir(parents=True, exist_ok=True)

    leaf_mb = max(0.2, float(leaf_mb))
    est_gb = (leaf_mb * count) / 1024.0
    print(f"\n=== live fixture  count={count}  shape={shape}  base={base} ===")
    print(f"  leaf_target                 {leaf_mb:.1f} MB/file  (~{est_gb:.1f} GB total)")
    if not seed_path.is_file():
        print(f"ERROR: seed not found: {seed_path}")
        return 2
    print(
        f"  seed                        {seed_path.name}  "
        f"{seed_path.stat().st_size / (1024 * 1024):.1f} MB"
    )

    roots: list[Path]
    if shape == "cd":
        c_root, d_root, mat_ms = materialize_cd_fixture(
            base,
            seed_path,
            count,
            ffmpeg,
            empty_dirs=empty_dirs,
            leaf_mb=leaf_mb,
        )
        roots = [c_root, d_root]
        n_files = sum(1 for r in roots for _ in r.rglob("*.mp4"))
        _print_row("materialize_cd_fixture", mat_ms, f"files={n_files} empty_dirs~{empty_dirs}")
    else:
        root = base / "library"
        mat_ms = materialize_fixture(root, seed_path, count, ffmpeg, leaf_mb=leaf_mb)
        roots = [root]
        n_files = sum(1 for _ in root.rglob("*.mp4"))
        _print_row("materialize_fixture", mat_ms, f"files={n_files}")

    if n_files < count * 0.95:
        print(f"ERROR: expected ~{count} files, got {n_files}")
        return 1

    old = _state_snapshot()
    try:
        results: dict[str, dict[str, float]] = {}
        want = str(phases or "both").lower()
        if want not in ("both", "cold", "warm"):
            print(f"ERROR: unknown --phases {phases}")
            return 2
        print(f"  phases                      {want}  (cold=no cache, warm=catalog cache)")
        if want in ("both", "cold"):
            wipe_scan_caches(roots)
            results["cold"] = run_phase(
                name="cold",
                roots=roots,
                cache=cache,
                ffmpeg=ffmpeg,
                count=count,
                thumbs=thumbs,
                seed_path=seed_path,
                incremental=False,
                load_disk_first=False,
            )
        if want in ("both", "warm"):
            results["warm"] = run_phase(
                name="warm",
                roots=roots,
                cache=cache,
                ffmpeg=ffmpeg,
                count=count,
                thumbs=thumbs,
                seed_path=seed_path,
                incremental=True,
                load_disk_first=True,
            )
        flags: list[str] = []
        if "cold" in results and "warm" in results:
            flags = compare_phases(results["cold"], results["warm"], count)
        elif "cold" in results and results["cold"].get("videos", 0) < count * 0.95:
            flags.append("ERROR cold scan found too few videos")
        elif "warm" in results and results["warm"].get("videos", 0) < count * 0.95:
            flags.append("ERROR warm scan found too few videos")
        print("\n========== flags ==========")
        if flags:
            for line in flags:
                print(f"  {line}")
        else:
            print("  none")
        print("\nAll live scenarios finished.")
        print(f"Fixture at: {base}")
        if any(line.startswith("ERROR") for line in flags):
            return 1
        return 0
    finally:
        STATE.update(old)
        web.invalidate_response_caches()
        if cleanup_tmp:
            try:
                shutil.rmtree(base, ignore_errors=True)
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Live 6000+ library scan/HTTP bench")
    p.add_argument("--count", type=int, default=DEFAULT_COUNT)
    p.add_argument(
        "--thumbs",
        default="0",
        help="0 | all | N — how many thumbs to generate after scan (default 0)",
    )
    p.add_argument(
        "--seed-from",
        default=None,
        help=f"Seed video (default for --shape cd: {DEFAULT_TCL_SEED})",
    )
    p.add_argument(
        "--fixture-dir",
        default=None,
        help=f"Persistent fixture dir on D: (default: {DEFAULT_FIXTURE_DIR})",
    )
    p.add_argument(
        "--shape",
        choices=("cd", "flat"),
        default="cd",
        help="cd = twin C_drive/D_drive trees; flat = single library root",
    )
    p.add_argument(
        "--empty-dirs",
        type=int,
        default=4000,
        help="Extra empty dirs for walk fan-out (cd shape only)",
    )
    p.add_argument(
        "--leaf-mb",
        type=float,
        default=DEFAULT_LEAF_MB,
        help=(
            f"Target size per unique leaf in MB (default {DEFAULT_LEAF_MB}; "
            f"6000×{DEFAULT_LEAF_MB:.0f}MB ≈ {6000 * DEFAULT_LEAF_MB / 1024:.0f}GB on D:)"
        ),
    )
    p.add_argument("--ffmpeg", default=None, help="ffmpeg.exe path")
    p.add_argument(
        "--phases",
        choices=("both", "cold", "warm"),
        default="both",
        help="both = wipe caches + full scan, then restart from catalogs (default)",
    )
    p.add_argument("--keep", action="store_true", help="Keep temp fixture dir")
    p.add_argument("--perf", action="store_true", help="Set VG_PERF=1 for tighter notes")
    args = p.parse_args(argv)
    if args.perf:
        os.environ["VG_PERF"] = "1"
    return run_live(
        count=max(10, int(args.count)),
        thumbs=str(args.thumbs),
        seed_from=args.seed_from,
        fixture_dir=args.fixture_dir,
        ffmpeg_bin=args.ffmpeg,
        keep=bool(args.keep),
        shape=str(args.shape),
        empty_dirs=max(0, int(args.empty_dirs)),
        leaf_mb=float(args.leaf_mb),
        phases=str(args.phases),
    )


if __name__ == "__main__":
    raise SystemExit(main())
