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


def parse_frame_rate(raw) -> float | None:
    """Parse ffprobe r_frame_rate / avg_frame_rate strings like '60/1'."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text in ("0/0", "N/A", "nan"):
        return None
    try:
        if "/" in text:
            num_s, den_s = text.split("/", 1)
            num, den = float(num_s), float(den_s)
            if den == 0:
                return None
            value = num / den
        else:
            value = float(text)
        if value <= 0 or value > 240:
            return None
        return value
    except (TypeError, ValueError):
        return None


def is_2k_or_4k(width, height) -> bool:
    """True for 1440p / 2K / 4K class resolutions (by width or height)."""
    try:
        w = int(width or 0)
        h = int(height or 0)
    except (TypeError, ValueError):
        return False
    return w >= 2560 or h >= 1440


HIGH_FPS_CLASSES = (60, 90, 120)
FPS_TARGET_PRESETS = (15, 20, 24, 25, 30, 48, 50, 60, 72, 90, 100, 120)
_HIGH_FPS_TOLERANCE = 3.0


def classify_high_fps(fps) -> int | None:
    """Map 59.94/60, 90, 119.88/120 to a transcode class; else None."""
    try:
        value = float(fps)
    except (TypeError, ValueError):
        return None
    for cls in HIGH_FPS_CLASSES:
        if abs(value - cls) <= _HIGH_FPS_TOLERANCE:
            return cls
    return None


def fps_can_halve_to_30(fps) -> bool:
    """True when source is 60/90/120-class high fps (button eligible)."""
    return classify_high_fps(fps) is not None


def fps_target_choices(fps) -> list[int]:
    """Lower-rate presets strictly below the source."""
    try:
        src = float(fps)
    except (TypeError, ValueError):
        return []
    return [t for t in FPS_TARGET_PRESETS if t < src - 0.5]


SCALE_PRESETS = (1080, 720)


def scale_choices(width=None, height=None) -> list[int]:
    """Heights the source can downscale to (already-smaller sizes omitted)."""
    try:
        h = int(height or 0)
    except (TypeError, ValueError):
        return []
    return [s for s in SCALE_PRESETS if h > s + 8]


def normalize_target_fps(src_fps, target_fps, default: int = 30) -> int | None:
    """Return a valid integer target strictly below source, or None."""
    choices = fps_target_choices(src_fps)
    try:
        src = float(src_fps)
    except (TypeError, ValueError):
        return None
    if target_fps is None or target_fps == "":
        if default in choices:
            return default
        return choices[0] if choices else None
    try:
        target = int(round(float(target_fps)))
    except (TypeError, ValueError):
        return None
    if target in choices:
        return target
    if 1 <= target < src - 0.5:
        return target
    return None


def fps_gate_reason(
    *,
    width=None,
    height=None,
    fps=None,
    has_ffmpeg: bool = True,
    stream_like: bool = False,
) -> str:
    """Explain why fps30 is/isn't offered. Used by API payload + logs.

    Media-specific reasons (resolution/fps) are checked before ``no_ffmpeg`` so
    a 4K@30 file reports ``fps_too_low`` even when ffmpeg is missing — that is
    the user-facing reason the button stays gray.
    """
    if stream_like:
        return "stream_like"
    if not (width or height):
        return "no_video_meta"
    if not is_2k_or_4k(width, height):
        return "not_2k_or_4k"
    if fps is None:
        return "no_fps"
    if not fps_can_halve_to_30(fps):
        return "fps_too_low"
    if not has_ffmpeg:
        return "no_ffmpeg"
    return "ok"


def probe_media_info(
    ffmpeg: str,
    path: Path,
    *,
    include_duration: bool = True,
    include_audio: bool = True,
    include_video_meta: bool = False,
) -> dict:
    """ffprobe detection, limited to the metadata dimensions requested.

    ``include_video_meta`` (width/height/fps) is for on-demand playback use only;
    bulk scan/probe paths must leave it False.
    """
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
            include_video_meta=include_video_meta,
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
        stream_fields = ["index", "codec_type"]
        if include_audio or include_video_meta:
            stream_fields.append("codec_name")
        if include_video_meta:
            stream_fields.extend(["width", "height", "r_frame_rate", "avg_frame_rate"])
        entries = [f"stream={','.join(stream_fields)}"]
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
        video_codec = ""
        width = None
        height = None
        fps = None
        for s in streams:
            ctype = (s.get("codec_type") or "").lower()
            cname = (s.get("codec_name") or "").lower().strip()
            if ctype == "video":
                has_video = True
                if include_video_meta and not video_codec and cname:
                    video_codec = cname
                if include_video_meta and width is None:
                    try:
                        w = int(s.get("width") or 0)
                        h = int(s.get("height") or 0)
                    except (TypeError, ValueError):
                        w, h = 0, 0
                    if w > 0 and h > 0:
                        width, height = w, h
                    fps = parse_frame_rate(s.get("avg_frame_rate")) or parse_frame_rate(
                        s.get("r_frame_rate")
                    )
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
        if include_video_meta:
            if width is not None:
                result["width"] = width
            if height is not None:
                result["height"] = height
            if fps is not None:
                result["fps"] = round(fps, 3)
            result["video_codec"] = video_codec
            result["probe_video_meta_done"] = True
        if include_video_meta or include_audio:
            log(
                f"[帧率探测] ffprobe ok path={path.name} "
                f"{width or 0}x{height or 0}@"
                f"{result.get('fps') if include_video_meta else '-'} "
                f"vcodec={(video_codec or '-') if include_video_meta else '(skip)'} "
                f"acodec={(audio_codec or '-') if include_audio else '(skip)'}"
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
            include_video_meta=include_video_meta,
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
    corrupted_input = False
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
                # Corrupted / truncated containers: retrying another seek point
                # only starts more ffmpeg processes and repeats the same failure.
                if (
                    "invalid data found when processing input" in stderr_lower
                    or "moov atom not found" in stderr_lower
                    or "error opening input" in stderr_lower
                ):
                    emit(
                        "WARN",
                        "thumbnail_source_corrupted",
                        force=True,
                        video=video,
                        output=out,
                        elapsed_ms=f"{(time.perf_counter() - started) * 1000.0:.1f}",
                        attempt=f"seek={ss}",
                        reason="invalid_input",
                    )
                    no_video_stream = True
                    corrupted_input = True
                    break
            except subprocess.TimeoutExpired as exc:
                attempts.append(f"seek={ss}:timeout={exc.timeout}s")
            except Exception as exc:
                attempts.append(f"seek={ss}:exception={type(exc).__name__}:{exc}")
                continue
        if corrupted_input:
            # Keep the specific source-corruption event above, and also emit
            # the generic failure event expected by aggregate log analysis.
            # Audio-only files intentionally remain source_no_video_stream
            # only, because that is a valid non-video container diagnosis.
            emit(
                "WARN",
                "thumbnail_generation_failed",
                force=True,
                video=video,
                output=out,
                elapsed_ms=f"{(time.perf_counter() - started) * 1000.0:.1f}",
                attempts=" || ".join(attempts),
                reason="invalid_input",
                background=background,
                burst=burst,
            )
        elif not no_video_stream:
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
    include_video_meta: bool = False,
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
        if include_video_meta:
            if info.get("width"):
                item["width"] = int(info["width"])
            if info.get("height"):
                item["height"] = int(info["height"])
            if info.get("fps") is not None:
                try:
                    item["fps"] = float(info["fps"])
                except (TypeError, ValueError):
                    pass
            if "video_codec" in info:
                item["video_codec"] = str(info.get("video_codec") or "").strip().lower()
            # Only mark done when we actually got resolution; otherwise allow retry.
            if item.get("width") or item.get("height"):
                item["probe_video_meta_done"] = True
            else:
                item.pop("probe_video_meta_done", None)
                log(
                    f"[帧率探测] 探测成功但无分辨率 "
                    f"path={info.get('path') or item.get('rel')} "
                    f"err=missing_width_height"
                )
    else:
        # Video-meta-only probes must not mark the whole item bad: playback can
        # still work, and this probe is optional / on-demand.
        if include_duration or include_audio:
            item["bad"] = True
            item["bad_reason"] = info.get("err") or "无法读取"
        if include_video_meta:
            # Failed on-demand fps probe: do NOT stick "done", so next open retries.
            item.pop("probe_video_meta_done", None)
            log(
                f"[帧率探测] 失败 vid={item.get('id') or ''} "
                f"rel={item.get('rel') or ''} err={info.get('err') or 'unknown'}"
            )


def _item_probe_path(item: dict) -> Path | None:
    from vg.disk_libs import resolve_item_rel

    thumb_src = _video_file_for_thumb(item)
    if thumb_src and thumb_src.is_file():
        return thumb_src
    rel = item.get("rel") or ""
    if rel:
        return resolve_item_rel(item, rel)
    return None


_player_probe_lock = threading.Lock()
_player_probe_inflight: set[str] = set()


def _player_probe_key(vid: str, root: str | None) -> str:
    return f"{(root or '').strip().casefold()}::{vid}"


def _player_video_meta_done(item: dict) -> bool:
    has_dims = bool(item.get("width") or item.get("height"))
    has_codec = "video_codec" in item
    return bool(item.get("probe_video_meta_done")) and has_dims and has_codec


def wants_player_video_meta(item: dict, *, stream_like: bool) -> bool:
    """True when player-open should (re)probe resolution/fps/video/audio codecs."""
    if stream_like or not isinstance(item, dict):
        return False
    return not (_player_video_meta_done(item) and _audio_already_known(item))


def player_video_meta_pending(vid: str, root: str | None) -> bool:
    with _player_probe_lock:
        return _player_probe_key(vid, root) in _player_probe_inflight


def schedule_player_video_meta_probe(vid: str, root: str | None, ffmpeg: str) -> bool:
    """Start a daemon ffprobe for width/height/fps/video+audio codecs. Never blocks."""
    if not vid or not ffmpeg:
        log(f"[帧率探测] 无法启动后台线程 vid={vid or '-'} ffmpeg={bool(ffmpeg)}")
        return False
    key = _player_probe_key(vid, root)
    with _player_probe_lock:
        if key in _player_probe_inflight:
            log(f"[帧率探测] 后台已在跑，跳过重复入队 vid={vid} key={key}")
            return False
        _player_probe_inflight.add(key)
    log(f"[帧率探测] 启动后台线程 vid={vid} root={root or ''} key={key}")
    threading.Thread(
        target=_player_video_meta_worker,
        args=(vid, root, ffmpeg, key),
        daemon=True,
        name=f"probe-vmeta-{vid[:8]}",
    ).start()
    return True


def _player_video_meta_worker(vid: str, root: str | None, ffmpeg: str, key: str) -> None:
    from vg.catalog_repository import find_video_by_id
    from vg.disk_libs import save_library_item

    try:
        item = find_video_by_id(vid, prefer_root=root)
        if not item:
            log(f"[帧率探测] 后台跳过：未找到视频 vid={vid}")
            return
        path = _item_probe_path(item)
        want_video = not _player_video_meta_done(item)
        want_audio = not _audio_already_known(item)
        log(
            f"[帧率探测] 后台开始 vid={vid} path={path} "
            f"exists={bool(path and path.is_file())} "
            f"want_video={int(want_video)} want_audio={int(want_audio)}"
        )
        if not want_video and not want_audio:
            log(f"[帧率探测] 后台跳过：画面/音频均已缓存 vid={vid}")
            return
        if not path or not path.is_file() or path.suffix.lower() == ".m3u8":
            log(f"[帧率探测] 后台跳过：无实体文件 vid={vid} path={path}")
            return
        info = probe_media_info(
            ffmpeg,
            path,
            include_duration=False,
            include_audio=want_audio,
            include_video_meta=want_video,
        )
        _apply_probe_to_item(
            item,
            info,
            include_duration=False,
            include_audio=want_audio,
            include_video_meta=want_video,
        )
        saved = False
        try:
            saved = bool(save_library_item(item))
        except Exception as exc:
            log(f"[帧率探测] 后台写入片库失败 vid={vid}: {exc}")
        log(
            f"[帧率探测] 后台完成 vid={vid} ok={info.get('ok')} "
            f"{item.get('width') or 0}x{item.get('height') or 0}@"
            f"{item.get('fps')} vcodec={item.get('video_codec') or '-'} "
            f"acodec={item.get('audio_codec') or '-'} "
            f"audio_hard={int(bool(item.get('audio_hard')))} "
            f"done={bool(item.get('probe_video_meta_done'))} "
            f"audio_done={bool(item.get('probe_audio_done'))} "
            f"err={info.get('err') or ''} saved={int(saved)}"
        )
        from vg.diagnostics import emit

        emit(
            "INFO",
            "fps_video_meta_probed",
            force=True,
            video_id=vid,
            ok=bool(info.get("ok")),
            width=item.get("width") or 0,
            height=item.get("height") or 0,
            fps=item.get("fps"),
            video_codec=item.get("video_codec") or "",
            audio_codec=item.get("audio_codec") or "",
            audio_hard=bool(item.get("audio_hard")),
            err=info.get("err") or "",
            path=str(path),
            background=True,
            saved=saved,
        )
    except Exception as exc:
        from vg.diagnostics import error as diag_error

        diag_error("fps_video_meta_worker_failed", exc, video_id=vid, root=root or "")
        log(f"[帧率探测] 后台异常 vid={vid}: {exc}")
    finally:
        with _player_probe_lock:
            _player_probe_inflight.discard(key)


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
            cache_rows = load_catalog_videos(cache)
            cache_snapshots = 0
            for video in cache_rows:
                if not isinstance(video, dict):
                    continue
                snap = _metadata_reuse_snapshot(
                    video,
                    want_duration=want_duration,
                    want_audio=want_audio,
                )
                if snap:
                    persisted += 1
                    cache_snapshots += 1
                    register(video)
            log(
                f"[元数据] 复用源诊断 cache={cache} rows={len(cache_rows)} "
                f"完整探测条目={cache_snapshots}"
            )
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
    """Look up a probe snapshot in the already-built batch index.

    Batch enrichment builds the complete SQLite index once.  Falling back to
    ``find_probe_donor`` here would issue one SQLite query for every miss and
    turn a large scan into an accidental N+1 query loop.
    """
    from vg.thumbs import thumb_content_keys

    for key in thumb_content_keys(item):
        found = sources.get(key)
        if found is not None:
            return found

    return None


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
    _state._meta_root = str(STATE.get("root") or "")
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
        from collections import Counter

        root_counts = Counter(
            str(v.get("_lib_root") or v.get("root") or "?")
            for v in videos
            if isinstance(v, dict)
        )
        need_root_counts = Counter(
            str(v.get("_lib_root") or v.get("root") or "?")
            for v in need
            if isinstance(v, dict)
        )
        known_duration = sum(1 for v in videos if _duration_already_known(v))
        known_audio = sum(1 for v in videos if _audio_already_known(v))
        log(
            f"[元数据] 入队诊断 root={_state._meta_root or STATE.get('root') or '?'} "
            f"videos={len(videos)} need={len(need)} 已知时长={known_duration} "
            f"已知声音={known_audio} roots={dict(root_counts)} need_roots={dict(need_root_counts)}"
        )
        need, reused_n = adopt_metadata_from_catalog(
            need,
            want_duration=want_duration,
            want_audio=want_audio,
        )

        def _safe_rebuild() -> None:
            # Probe fields are already on the live video dicts / SQLite. A heavy
            # mid-scan rebuild raced ``_publish_live`` (bench: 4000←4500). Use a
            # light rebuild while scanning so duration facets still advance.
            scanning = bool(STATE.get("scanning"))
            if scanning:
                try:
                    from vg.diagnostics import emit

                    emit(
                        "INFO",
                        "metadata_enrichment_light_rebuild_scanning",
                        force=True,
                        videos=len(STATE.get("videos") or []),
                        thread=threading.current_thread().name,
                    )
                except Exception:
                    pass
            rebuild_indexes(
                list(STATE.get("videos") or []),
                heavy=not scanning,
            )

        if not need:
            if reused_n:
                _safe_rebuild()
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
        # Incremental batches already UPSERTed; one catalog rebuild
        # advances lib_gen. Do NOT re-save every probed row here — that used to
        # rewrite the whole catalog thousands of times after "完成".
        _safe_rebuild()
        reuse_tip = f"，复用 {reused_n}" if reused_n else ""
        STATE["meta_progress"] = (
            f"元数据完成（{scope}）：可读 {ok_n}，异常 {fail_n}{reuse_tip}"
        )
        log(f"[元数据] 完成（{scope}）：可读 {ok_n}，异常 {fail_n}{reuse_tip}")
        try:
            from vg.diagnostics import catalog_plane_snapshot

            catalog_plane_snapshot(
                "metadata_enrichment_done",
                readable=ok_n,
                failed=fail_n,
                reused=reused_n,
            )
        except Exception:
            pass
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
        _state._meta_root = ""
        threading.Timer(4.0, lambda: STATE.update(meta_progress="")).start()
