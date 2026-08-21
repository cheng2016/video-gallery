# -*- coding: utf-8 -*-
"""ffmpeg discovery, probing, thumbnails, metadata enrichment."""
from __future__ import annotations

import sys


import json
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from vg.cache import (
    thumb_path,
)
from vg.privacy import (
    pack_thumb_bytes,
    probe_audio_enabled,
    probe_duration_enabled,
    unpack_thumb_bytes,
)
from vg.config import (
    BROWSER_FRIENDLY_AUDIO,
    MIN_VIDEO_FILE_BYTES,
    PROBE_META_VER,
)
from vg import state as _state
from vg.state import STATE, _meta_lock
from vg.util import (
    _clear_path_attrs_windows,
    format_duration,
    log,
    meta_worker_count,
    resolve_under_root,
    resolve_video_path,
)

def find_ffmpeg() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    if sys.platform == "darwin":
        # Finder 启动的进程通常没有 Homebrew/MacPorts 的 shell PATH。
        candidates = [
            "/opt/homebrew/bin/ffmpeg",  # Apple Silicon Homebrew
            "/usr/local/bin/ffmpeg",     # Intel Homebrew
            "/opt/local/bin/ffmpeg",     # MacPorts
        ]
    else:
        candidates = [
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
        ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def probe_duration(ffmpeg: str, path: Path) -> float | None:
    info = probe_media_info(ffmpeg, path)
    if not info.get("ok"):
        return None
    dur = info.get("duration")
    return float(dur) if dur else None


def probe_media_info(
    ffmpeg: str,
    path: Path,
    *,
    include_duration: bool = True,
    include_audio: bool = True,
) -> dict:
    """ffprobe detection, limited to the metadata dimensions requested."""
    from vg.diagnostics import emit, error

    started = time.perf_counter()

    def failed(reason: str, **fields) -> dict:
        emit(
            "WARN",
            "media_probe_failed",
            force=True,
            path=path,
            reason=reason,
            include_duration=include_duration,
            include_audio=include_audio,
            elapsed_ms=f"{(time.perf_counter() - started) * 1000.0:.1f}",
            **fields,
        )
        return {"ok": False, "err": reason[:120]}

    if not path or not path.is_file():
        return failed("文件不存在")
    ffprobe = _ffprobe_path(ffmpeg)
    if not ffprobe:
        return failed("未找到 ffprobe")
    try:
        entries = ["stream=index,codec_type"]
        if include_audio:
            entries[0] += ",codec_name"
        if include_duration:
            entries.append("format=duration")
        cmd = [ffprobe, "-v", "error"]
        for entry in entries:
            cmd.extend(["-show_entries", entry])
        cmd.extend(["-of", "json", str(path)])
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if sys.platform == "win32"
            else 0
        )
        # Bulk probes run after scan; keep them below the web UI / player.
        if sys.platform == "win32":
            creationflags |= getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        r = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=25,
            creationflags=creationflags,
        )
        err = (r.stderr or "").strip()
        if r.returncode != 0:
            reason = (err or "ffprobe 失败")[:120]
            return failed(reason, returncode=r.returncode, stderr=err[-500:])
        payload = json.loads(r.stdout or "{}") if r.stdout else {}
        streams = payload.get("streams") or []
        has_video = False
        audio_codec = ""
        for s in streams:
            ctype = (s.get("codec_type") or "").lower()
            cname = (s.get("codec_name") or "").lower().strip()
            if ctype == "video":
                has_video = True
            elif ctype == "audio" and not audio_codec and cname:
                audio_codec = cname
        if not has_video:
            return failed("无视频流", stream_count=len(streams))
        result = {"ok": True}
        fmt = payload.get("format") or {}
        if include_duration and fmt.get("duration"):
            try:
                d = float(fmt["duration"])
                if d > 0:
                    result["duration"] = d
            except (TypeError, ValueError):
                pass
        if include_audio:
            result["audio_codec"] = audio_codec
            result["audio_hard"] = (
                bool(audio_codec) and audio_codec not in BROWSER_FRIENDLY_AUDIO
            )
        from vg.diagnostics import aggregate

        aggregate("media_probe_ok", (time.perf_counter() - started) * 1000.0)
        return result
    except subprocess.TimeoutExpired as exc:
        return failed("探测超时", timeout=exc.timeout)
    except Exception as e:
        error(
            "media_probe_exception",
            e,
            path=path,
            include_duration=include_duration,
            include_audio=include_audio,
        )
        return {"ok": False, "err": str(e)[:120]}


def _ffprobe_path(ffmpeg: str) -> str:
    ffprobe = ffmpeg.replace("ffmpeg", "ffprobe").replace("ffmpeg.exe", "ffprobe.exe")
    if not os.path.isfile(ffprobe):
        ffprobe = shutil.which("ffprobe") or ""
    return ffprobe


def make_thumbnail(
    ffmpeg: str,
    video: Path,
    out: Path,
    seek: float = 3.0,
    force: bool = False,
    *,
    background: bool = False,
    burst: bool = False,
) -> bool:
    """截帧写入预览图 out（.vgt；按隐私设置加密或明文）。有效缓存则跳过；force 或损坏则重建。"""
    from vg.diagnostics import emit, error

    started = time.perf_counter()
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        error("thumbnail_output_dir_failed", exc, video=video, output=out)
        return False
    _clear_path_attrs_windows(out)
    if out.exists() and not force:
        try:
            raw = unpack_thumb_bytes(out.read_bytes())
            if raw and raw[:2] == b"\xff\xd8" and len(raw) > 100:
                return True
            emit(
                "WARN",
                "thumbnail_existing_invalid",
                force=True,
                video=video,
                output=out,
                action="delete_and_regenerate",
            )
            out.unlink(missing_ok=True)
        except OSError as exc:
            error("thumbnail_existing_read_failed", exc, video=video, output=out)
    elif out.exists() and force:
        try:
            out.unlink(missing_ok=True)
        except OSError as exc:
            error("thumbnail_force_delete_failed", exc, video=video, output=out)
            return False

    if not video.is_file():
        emit(
            "WARN",
            "thumbnail_source_missing",
            force=True,
            video=video,
            output=out,
        )
        return False

    tmp = out.with_suffix(".tmp.jpg")
    attempts: list[str] = []
    no_video_stream = False
    try:
        seeks = [seek]
        fallbacks = (1.0, 0.0) if background else (1.0, 0.0, 10.0, 30.0)
        for fallback in fallbacks:
            if abs(fallback - seek) > 0.05:
                seeks.append(fallback)
        for ss in seeks:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            try:
                polite = background and not burst
                thread_args = ["-threads", "1"] if (background or burst) else []
                cmd = [
                    ffmpeg, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                    *thread_args, "-ss", str(ss), "-i", str(video),
                    "-frames:v", "1", "-an", "-sn", "-dn",
                    "-vf", "scale=480:-2", *thread_args,
                    "-q:v", "4", str(tmp),
                ]
                run_cmd = cmd
                if polite and sys.platform != "win32":
                    nice = shutil.which("nice")
                    if nice:
                        run_cmd = [nice, "-n", "10", *cmd]
                creationflags = (
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    if sys.platform == "win32"
                    else 0
                )
                if polite and sys.platform == "win32":
                    creationflags |= getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
                r = subprocess.run(
                    run_cmd,
                    capture_output=True,
                    timeout=25 if background else 60,
                    creationflags=creationflags,
                )
                if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
                    raw = tmp.read_bytes()
                    if not (raw[:2] == b"\xff\xd8"):
                        attempts.append(f"seek={ss}:invalid_jpeg bytes={len(raw)}")
                        continue
                    _clear_path_attrs_windows(out)
                    out.write_bytes(pack_thumb_bytes(raw))
                    from vg.diagnostics import aggregate

                    aggregate(
                        "thumbnail_generated",
                        (time.perf_counter() - started) * 1000.0,
                    )
                    return True
                stderr = (r.stderr or b"").decode("utf-8", errors="replace").strip()
                attempts.append(
                    f"seek={ss}:exit={r.returncode}:"
                    f"{stderr[-500:] if stderr else 'no_output'}"
                )
                # Audio-only containers are still valid media files, but they
                # cannot produce a video frame.  Retrying different seek
                # positions only starts more ffmpeg processes and repeats the
                # same failure (as seen for audio-only .mp4 files in scans).
                stderr_lower = stderr.casefold()
                if (
                    "output file does not contain any stream" in stderr_lower
                    or "matches no streams" in stderr_lower
                    or "no video stream" in stderr_lower
                ):
                    emit(
                        "WARN",
                        "thumbnail_source_no_video_stream",
                        force=True,
                        video=video,
                        output=out,
                        elapsed_ms=f"{(time.perf_counter() - started) * 1000.0:.1f}",
                        attempt=f"seek={ss}",
                        reason="no_video_stream",
                    )
                    no_video_stream = True
                    break
            except subprocess.TimeoutExpired as exc:
                attempts.append(f"seek={ss}:timeout={exc.timeout}s")
            except Exception as exc:
                attempts.append(f"seek={ss}:exception={type(exc).__name__}:{exc}")
                continue
        if not no_video_stream:
            emit(
                "WARN",
                "thumbnail_generation_failed",
                force=True,
                video=video,
                output=out,
                elapsed_ms=f"{(time.perf_counter() - started) * 1000.0:.1f}",
                attempts=" || ".join(attempts),
                background=background,
                burst=burst,
            )
        return False
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError as exc:
            error("thumbnail_temp_cleanup_failed", exc, temp=tmp, video=video)


def save_thumbnail_jpeg(out: Path, jpeg_bytes: bytes) -> bool:
    """把 JPEG 写入预览图文件（按隐私设置加密或明文）。"""
    if not jpeg_bytes or jpeg_bytes[:2] != b"\xff\xd8":
        from vg.diagnostics import emit

        emit(
            "WARN",
            "thumbnail_jpeg_rejected",
            force=True,
            output=out,
            reason="invalid_jpeg_header",
            bytes=len(jpeg_bytes or b""),
        )
        return False
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        _clear_path_attrs_windows(out)
        out.write_bytes(pack_thumb_bytes(jpeg_bytes))
        return True
    except OSError as exc:
        from vg.diagnostics import error

        error("thumbnail_jpeg_write_failed", exc, output=out, bytes=len(jpeg_bytes))
        return False


def _first_media_from_m3u8(playlist: Path) -> Path | None:
    try:
        text = playlist.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if re.match(r"https?://", s, re.I):
            continue
        cand = (playlist.parent / s.split("?")[0]).resolve()
        try:
            if cand.is_file():
                if cand.suffix.lower() == ".m3u8":
                    nested = _first_media_from_m3u8(cand)
                    if nested:
                        return nested
                    continue
                return cand
        except OSError:
            continue
    return None


def _video_file_for_thumb(item: dict) -> Path | None:
    """取可用于截帧的实体文件（TS 合集用第一段；m3u8 解析首个媒体）。"""
    from vg.disk_libs import resolve_item_rel, root_for_item

    if not root_for_item(item) and not STATE.get("root"):
        return None
    if item.get("kind") == "ts_set" and item.get("segments"):
        return resolve_item_rel(item, item["segments"][0])
    if item.get("kind") == "m3u8" or (item.get("ext") or "").lower() == ".m3u8":
        pl = resolve_item_rel(item, item.get("rel") or "")
        if pl:
            hit = _first_media_from_m3u8(pl)
            if hit:
                return hit
        return None
    return resolve_item_rel(item, item.get("rel") or "")


def _apply_probe_to_item(
    item: dict,
    info: dict,
    *,
    include_duration: bool = True,
    include_audio: bool = True,
) -> None:
    item["probe_ver"] = PROBE_META_VER
    if include_duration:
        item["probe_duration_done"] = True
    if include_audio:
        item["probe_audio_done"] = True
    if info.get("ok"):
        item.pop("bad", None)
        item.pop("bad_reason", None)
        if include_duration:
            dur = info.get("duration")
            if dur:
                item["duration"] = dur
                item["duration_h"] = format_duration(dur)
        if include_audio and "audio_codec" in info:
            ac = (info.get("audio_codec") or "").lower().strip()
            item["audio_codec"] = ac
            item["audio_hard"] = bool(info.get("audio_hard")) if "audio_hard" in info else (
                bool(ac) and ac not in BROWSER_FRIENDLY_AUDIO
            )
    else:
        item["bad"] = True
        item["bad_reason"] = info.get("err") or "无法读取"


def _item_probe_path(item: dict) -> Path | None:
    from vg.disk_libs import resolve_item_rel

    thumb_src = _video_file_for_thumb(item)
    if thumb_src and thumb_src.is_file():
        return thumb_src
    rel = item.get("rel") or ""
    if rel:
        return resolve_item_rel(item, rel)
    return None


def _duration_already_known(item: dict) -> bool:
    """True when duration was probed before, or a real duration is already cached."""
    if item.get("probe_duration_done") or item.get("bad"):
        return True
    dur = item.get("duration")
    try:
        return dur is not None and float(dur) > 0
    except (TypeError, ValueError):
        return bool(item.get("duration_h"))


def _audio_already_known(item: dict) -> bool:
    """True when audio was probed before; empty codec still counts as done."""
    if item.get("probe_audio_done") or item.get("bad"):
        return True
    return "audio_codec" in item


def _probe_scope_label(*, want_duration: bool, want_audio: bool) -> str:
    """Human-readable probe target for logs / UI progress."""
    if want_duration and want_audio:
        return "时长+声音"
    if want_duration:
        return "时长"
    if want_audio:
        return "声音"
    return "无"


def _probe_cpu_label(workers: int) -> str:
    cpus = max(1, os.cpu_count() or 1)
    return f"{workers} 线程 / {cpus} 逻辑核"


def _persist_probed_items(items: list[dict]) -> None:
    """UPSERT probed rows into SQLite so a mid-run exit keeps progress.

    Batches by disk (one transaction each). Does not bump lib_gen — callers bump
    once via rebuild_indexes when the whole enrichment finishes.
    """
    if not items:
        return
    from vg.disk_libs import save_library_items

    try:
        save_library_items(items, bump_gen=False)
    except Exception as e:
        log(f"[元数据] 中途保存失败: {e}")


def _needs_metadata_probe(
    item: dict,
    *,
    want_duration: bool | None = None,
    want_audio: bool | None = None,
) -> bool:
    if want_duration is None:
        want_duration = probe_duration_enabled()
    if want_audio is None:
        want_audio = probe_audio_enabled()
    if not want_duration and not want_audio:
        return False
    duration_done = _duration_already_known(item)
    audio_done = _audio_already_known(item)
    if (not want_duration or duration_done) and (not want_audio or audio_done):
        return False
    kind = item.get("kind") or ""
    if kind == "ts_set" and not item.get("segments"):
        return False
    size = int(item.get("size") or 0)
    if size and size < MIN_VIDEO_FILE_BYTES and kind not in ("m3u8", "ts_set"):
        return False
    return True


def _metadata_reuse_snapshot(
    video: dict,
    *,
    want_duration: bool,
    want_audio: bool,
) -> dict | None:
    """Copyable probe fields from a known good catalog row. Never copies bad marks."""
    if video.get("bad"):
        return None
    out: dict = {}
    if want_duration and _duration_already_known(video):
        dur = video.get("duration")
        try:
            if dur is not None and float(dur) > 0:
                out["duration"] = float(dur)
                if video.get("duration_h"):
                    out["duration_h"] = video["duration_h"]
        except (TypeError, ValueError):
            if video.get("duration_h"):
                out["duration_h"] = video["duration_h"]
        out["probe_duration_done"] = True
    if want_audio and _audio_already_known(video):
        out["audio_codec"] = (video.get("audio_codec") or "").lower().strip()
        if "audio_hard" in video:
            out["audio_hard"] = bool(video.get("audio_hard"))
        else:
            out["audio_hard"] = bool(out["audio_codec"]) and out["audio_codec"] not in BROWSER_FRIENDLY_AUDIO
        out["probe_audio_done"] = True
    if not out:
        return None
    out["probe_ver"] = video.get("probe_ver") if video.get("probe_ver") is not None else PROBE_META_VER
    return out


def build_metadata_source_index(
    *,
    want_duration: bool,
    want_audio: bool,
) -> dict[str, dict]:
    """Build one reusable probe index from memory and persisted catalogs."""
    from vg.thumbs import _iter_memory_videos, thumb_content_keys

    index: dict[str, dict] = {}

    def richer(a: dict, b: dict) -> dict:
        merged = dict(a)
        for key, value in b.items():
            if key not in merged or (key == "duration" and value and not merged.get(key)):
                merged[key] = value
        return merged

    def register(video: dict) -> None:
        snap = _metadata_reuse_snapshot(
            video,
            want_duration=want_duration,
            want_audio=want_audio,
        )
        if not snap:
            return
        for key in thumb_content_keys(video):
            prev = index.get(key)
            index[key] = snap if prev is None else richer(prev, snap)

    for video in _iter_memory_videos():
        register(video)
    persisted = 0
    started = time.perf_counter()
    try:
        from vg.catalog_db import iter_catalog_cache_dirs, load_catalog_videos

        for cache in iter_catalog_cache_dirs():
            for video in load_catalog_videos(cache):
                if not isinstance(video, dict):
                    continue
                snap = _metadata_reuse_snapshot(
                    video,
                    want_duration=want_duration,
                    want_audio=want_audio,
                )
                if snap:
                    persisted += 1
                    register(video)
    except Exception as exc:
        log(f"[元数据] 读取 SQLite 持久化复用索引失败: {exc}")
    log(
        f"[元数据] 持久化复用索引完成：条目 {persisted}，键 {len(index)}，"
        f"耗时 {(time.perf_counter() - started) * 1000.0:.1f}ms"
    )
    return index


def _lookup_probe_snapshot(
    item: dict,
    sources: dict[str, dict],
    *,
    want_duration: bool,
    want_audio: bool,
) -> dict | None:
    """Memory hit first, then indexed SQLite cross-cache donor lookup."""
    from vg.thumbs import thumb_content_keys

    for key in thumb_content_keys(item):
        found = sources.get(key)
        if found is not None:
            return found

    from vg.catalog_db import find_probe_donor
    from vg.duplicates import duplicate_name_key

    try:
        size = int(item.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    donor = find_probe_donor(
        file_sig=str(item.get("file_sig") or "").strip(),
        name_key=duplicate_name_key(item),
        size=size,
        skip_bad=True,
    )
    if not donor:
        return None
    return _metadata_reuse_snapshot(
        donor,
        want_duration=want_duration,
        want_audio=want_audio,
    )


def reuse_existing_metadata(
    item: dict,
    sources: dict[str, dict],
    *,
    want_duration: bool,
    want_audio: bool,
) -> bool:
    """Adopt duration/audio from another disk's catalog row. No ffprobe."""
    hit = _lookup_probe_snapshot(
        item,
        sources,
        want_duration=want_duration,
        want_audio=want_audio,
    )
    if hit is None:
        return False

    applied = False
    if want_duration and not _duration_already_known(item):
        if "duration" in hit:
            item["duration"] = hit["duration"]
        if hit.get("duration_h"):
            item["duration_h"] = hit["duration_h"]
        if hit.get("probe_duration_done") or hit.get("duration"):
            item["probe_duration_done"] = True
            applied = True
    if want_audio and not _audio_already_known(item):
        if "audio_codec" in hit or hit.get("probe_audio_done"):
            item["audio_codec"] = hit.get("audio_codec") or ""
            if "audio_hard" in hit:
                item["audio_hard"] = bool(hit["audio_hard"])
            item["probe_audio_done"] = True
            applied = True
    if not applied:
        return False
    item["probe_ver"] = hit.get("probe_ver") if hit.get("probe_ver") is not None else PROBE_META_VER
    return not _needs_metadata_probe(
        item,
        want_duration=want_duration,
        want_audio=want_audio,
    )


def adopt_metadata_from_catalog(
    need: list[dict],
    *,
    want_duration: bool,
    want_audio: bool,
) -> tuple[list[dict], int]:
    """Reuse cross-disk probe results. Returns (still_need_ffprobe, reused_count)."""
    if not need:
        return [], 0
    sources = build_metadata_source_index(
        want_duration=want_duration,
        want_audio=want_audio,
    )
    started = time.perf_counter()
    total = len(need)
    leftover: list[dict] = []
    reused: list[dict] = []
    for index, item in enumerate(need, 1):
        if reuse_existing_metadata(
            item,
            sources,
            want_duration=want_duration,
            want_audio=want_audio,
        ):
            reused.append(item)
        else:
            leftover.append(item)
        if index == total or index % 40 == 0:
            log(
                f"[元数据] 跨盘复用检查 {index}/{total}"
                f"（已复用 {len(reused)}，待探测 {len(leftover)}，"
                f"耗时 {(time.perf_counter() - started) * 1000.0:.0f}ms）"
            )
    if reused:
        _persist_probed_items(reused)
        scope = _probe_scope_label(want_duration=want_duration, want_audio=want_audio)
        log(f"[元数据] 跨盘复用 {len(reused)} 个（{scope}），无需重新 ffprobe")
    return leftover, len(reused)


def enrich_metadata_parallel(items: list[dict], label: str = "元数据") -> tuple[int, int]:
    """并行 ffprobe：补时长 + 损坏标记。返回 (成功, 失败)。"""
    ffmpeg = STATE.get("ffmpeg")
    if not items or not ffmpeg:
        return 0, 0
    include_duration = probe_duration_enabled()
    include_audio = probe_audio_enabled()
    if not include_duration and not include_audio:
        return 0, 0
    total = len(items)
    workers = meta_worker_count(total)
    scope = _probe_scope_label(want_duration=include_duration, want_audio=include_audio)
    cpu = _probe_cpu_label(workers)
    STATE["meta_progress"] = f"{label}探测{scope} 0/{total}（{cpu}）…"
    log(f"[元数据] {label}探测{scope}：共 {total} 个，占用 {cpu}")
    from vg.diagnostics import call as diagnostic_call

    diagnostic_call(
        "enrich_metadata_parallel",
        total=total,
        workers=workers,
        scope=scope,
        label=label,
    )
    ok_n = fail_n = done = 0
    lock = threading.Lock()

    def one(item: dict) -> tuple[dict, bool, str]:
        name = item.get("name") or item.get("rel") or item.get("id") or "?"
        kind = item.get("kind") or ""
        is_stream = kind in ("m3u8", "ts_set") or (item.get("ext") or "").lower() == ".m3u8"
        path = _item_probe_path(item)
        if not path or not path.is_file():
            item["probe_ver"] = PROBE_META_VER
            if include_duration:
                item["probe_duration_done"] = True
            if include_audio:
                item["probe_audio_done"] = True
            if not is_stream:
                item["bad"] = True
                item["bad_reason"] = "文件不存在"
            return item, False, f"{name} (无实体文件)"
        # 播放列表：只对真实媒体分片探测；若落到 .m3u8 本身则只记 probe，不标坏
        if is_stream and path.suffix.lower() == ".m3u8":
            item["probe_ver"] = PROBE_META_VER
            if include_duration:
                item["probe_duration_done"] = True
            if include_audio:
                item["probe_audio_done"] = True
                item["audio_codec"] = item.get("audio_codec") or ""
                item["audio_hard"] = False
            return item, True, name
        info = probe_media_info(
            ffmpeg,
            path,
            include_duration=include_duration,
            include_audio=include_audio,
        )
        _apply_probe_to_item(
            item,
            info,
            include_duration=include_duration,
            include_audio=include_audio,
        )
        return item, bool(info.get("ok")), name

    dirty: list[dict] = []
    flush_every = 40
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(one, it) for it in items]
            for fut in as_completed(futures):
                item, ok, name = fut.result()
                flush_now: list[dict] = []
                with lock:
                    done += 1
                    if ok:
                        ok_n += 1
                    else:
                        fail_n += 1
                    dirty.append(item)
                    if len(dirty) >= flush_every or done == total:
                        flush_now = dirty
                        dirty = []
                    STATE["meta_progress"] = (
                        f"{label}探测{scope} {done}/{total}"
                        f"（可读 {ok_n}，异常 {fail_n}，{cpu}）…"
                    )
                    if done % 40 == 0 or done == total:
                        log(
                            f"[元数据] ({done}/{total}) {'OK' if ok else '异常'}"
                            f" [{scope}] {name}"
                        )
                if flush_now:
                    _persist_probed_items(flush_now)
    finally:
        if dirty:
            _persist_probed_items(dirty)
    return ok_n, fail_n


def start_metadata_enrichment() -> None:
    """后台补时长 / 损坏检测（不阻塞浏览）。"""
    if not probe_duration_enabled() and not probe_audio_enabled():
        STATE["meta_progress"] = ""
        return
    if not STATE.get("ffmpeg") or not STATE.get("videos"):
        return
    if not _meta_lock.acquire(blocking=False):
        return
    if _state._meta_running:
        _meta_lock.release()
        return
    _state._meta_running = True
    _meta_lock.release()
    threading.Thread(target=_bg_enrich_metadata, daemon=True, name="meta-enrich").start()


def _bg_enrich_metadata() -> None:
    from vg.catalog import rebuild_indexes

    started = time.perf_counter()
    try:
        videos = STATE.get("videos") or []
        want_duration = probe_duration_enabled()
        want_audio = probe_audio_enabled()
        need = [
            v
            for v in videos
            if _needs_metadata_probe(
                v,
                want_duration=want_duration,
                want_audio=want_audio,
            )
        ]
        need, reused_n = adopt_metadata_from_catalog(
            need,
            want_duration=want_duration,
            want_audio=want_audio,
        )
        if not need:
            if reused_n:
                rebuild_indexes(list(STATE.get("videos") or []))
                scope = _probe_scope_label(
                    want_duration=want_duration,
                    want_audio=want_audio,
                )
                STATE["meta_progress"] = f"元数据完成：跨盘复用 {reused_n}（{scope}）"
                log(f"[元数据] 完成：全部跨盘复用 {reused_n}（{scope}）")
            else:
                STATE["meta_progress"] = ""
            return
        tip = f"，跨盘已复用 {reused_n}" if reused_n else ""
        scope = _probe_scope_label(want_duration=want_duration, want_audio=want_audio)
        log(
            f"[元数据] 后台探测{scope}：待 ffprobe {len(need)} 个"
            f"（本盘已缓存的会跳过{tip}）…"
        )
        ok_n, fail_n = enrich_metadata_parallel(need, label="后台")
        current = list(STATE.get("videos") or [])
        # Incremental batches already UPSERTed; one catalog rebuild
        # advances lib_gen. Do NOT re-save every probed row here — that used to
        # rewrite the whole catalog thousands of times after "完成".
        rebuild_indexes(current)
        reuse_tip = f"，复用 {reused_n}" if reused_n else ""
        STATE["meta_progress"] = (
            f"元数据完成（{scope}）：可读 {ok_n}，异常 {fail_n}{reuse_tip}"
        )
        log(f"[元数据] 完成（{scope}）：可读 {ok_n}，异常 {fail_n}{reuse_tip}")
        from vg.diagnostics import perf as diagnostic_perf

        diagnostic_perf(
            "metadata_enrichment",
            (time.perf_counter() - started) * 1000.0,
            force=True,
            scope=scope,
            readable=ok_n,
            failed=fail_n,
            reused=reused_n,
        )
    except Exception as e:
        STATE["meta_progress"] = f"元数据探测失败: {e}"
        from vg.util import log_error

        log_error("metadata_enrichment_failed", e)
    finally:
        _state._meta_running = False
        threading.Timer(4.0, lambda: STATE.update(meta_progress="")).start()
