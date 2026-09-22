# -*- coding: utf-8 -*-
"""MP4 convert / fix-audio workers and helpers."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from vg.cache import thumb_file_ready, thumb_path, thumb_version
from vg.catalog import build_tree, rebuild_indexes
from vg.catalog_repository import find_video_by_id
from vg.config import (
    BROWSER_HARD_EXTS,
    CONVERT_MAX_PARALLEL,
    PROBE_META_VER,
    RM_EXTS,
    RM_UNAVAILABLE_MSG,
    SEGMENT_FOLDER_GENERIC,
    THUMB_EXT,
    TRANSCODE_OUT_EXTS,
    VGDATA_DIR,
)
from vg.disk_libs import (
    cache_dir_for_item,
    resolve_item_rel,
    root_for_item,
    save_library_item,
    save_root_library,
)
from vg.genres import detect_genres
from vg.media import (
    _apply_probe_to_item,
    fps_can_halve_to_30,
    is_2k_or_4k,
    normalize_target_fps,
    make_thumbnail,
    probe_duration,
    probe_media_info,
)
from vg.state import STATE, _convert_lock
from vg.taxonomy import ensure_video_taxonomy
from vg.thumb_jobs import (
    THUMB_PRIORITY_BATCH,
    submit_thumbnail_job,
    thumbnail_job_key,
)
from vg.util import (
    format_size,
    is_too_small_video,
    log,
    safe_rel,
    video_id,
)
import hashlib

def _sanitize_filename(name: str) -> str:
    name = (name or "video").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.rstrip(" .")
    return name[:120] or "video"


def _unique_mp4_path(out_dir: Path, base_name: str) -> Path:
    stem = _sanitize_filename(base_name)
    candidate = out_dir / f"{stem}.mp4"
    n = 1
    while candidate.exists():
        candidate = out_dir / f"{stem}_{n}.mp4"
        n += 1
    return candidate


def _unique_out_path(out_dir: Path, base_name: str, ext: str = ".mp4") -> Path:
    stem = _sanitize_filename(base_name)
    suffix = ext if ext.startswith(".") else f".{ext}"
    candidate = out_dir / f"{stem}{suffix}"
    n = 1
    while candidate.exists():
        candidate = out_dir / f"{stem}_{n}{suffix}"
        n += 1
    return candidate


def _path_under_root(path: Path, root: Path | None = None) -> bool:
    use_root = root if root is not None else STATE.get("root")
    if not use_root or not path:
        return False
    try:
        path.resolve().relative_to(Path(use_root).resolve())
        return True
    except (ValueError, OSError):
        return False


def convert_parallel_limit() -> int:
    n = STATE.get("convert_parallel")
    if n is None:
        n = CONVERT_MAX_PARALLEL
    try:
        return max(1, min(4, int(n)))
    except (TypeError, ValueError):
        return 1


def _convert_job_public(job: dict) -> dict:
    return {
        "id": job.get("id") or "",
        "vid": job.get("vid") or "",
        "root": job.get("root") or "",
        "kind": job.get("kind") or "mp4",
        "name": job.get("name") or "",
        "status": job.get("status") or "error",
        "msg": job.get("msg") or "",
        "percent": int(job.get("percent") or 0),
        "out_path": job.get("out_path") or "",
        "added_id": job.get("added_id") or "",
        "target_fps": int(job.get("target_fps") or 0) or None,
        "out_ext": job.get("out_ext") or "",
        "scale": int(job.get("scale") or 0) or 0,
        "video_encoder": job.get("video_encoder") or "",
        "audio_encoder": job.get("audio_encoder") or "",
    }


def convert_kind_label(job: dict) -> str:
    kind = (job.get("kind") or "mp4").strip().lower()
    if kind == "fix_audio":
        return "修声音"
    if kind == "fps30":
        target = job.get("target_fps")
        return f"降帧→{target}" if target else "降帧"
    if kind == "transcode":
        parts: list[str] = []
        if job.get("target_fps"):
            parts.append(f"降帧→{job['target_fps']}")
        if job.get("scale"):
            parts.append(f"压缩{job['scale']}p")
        enc = (job.get("video_encoder") or "auto").lower()
        if enc in ("h264", "h265"):
            parts.append(enc.upper())
        aenc = (job.get("audio_encoder") or "auto").lower()
        if aenc in ("aac", "mp3", "opus", "ac3"):
            parts.append({"aac": "AAC", "mp3": "MP3", "opus": "Opus", "ac3": "AC-3"}[aenc])
        ext = (job.get("out_ext") or "").lstrip(".")
        if ext:
            parts.append(f"转{ext}")
        return " · ".join(parts) or "转换"
    return "转封装"


def probe_ffmpeg_realmedia(ffmpeg: str | None) -> bool:
    """True when this ffmpeg binary lists a RealMedia demuxer."""
    if not ffmpeg:
        return False
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-demuxers"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=12,
            creationflags=flags,
        )
    except Exception as exc:
        log(f"[ffmpeg] 探测 RealMedia demuxer 失败: {exc}")
        return False
    blob = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    return "realmedia" in blob or "real_media" in blob


_rm_probe_lock = threading.Lock()


def schedule_ffmpeg_rm_probe() -> bool:
    """Probe RealMedia demuxer in a daemon thread. Returns True if still unknown/in flight."""
    if not STATE.get("ffmpeg"):
        STATE["ffmpeg_rm"] = False
        STATE["ffmpeg_rm_pending"] = False
        return False
    if STATE.get("ffmpeg_rm") is not None:
        return False
    with _rm_probe_lock:
        if STATE.get("ffmpeg_rm") is not None:
            return False
        if STATE.get("ffmpeg_rm_pending"):
            return True
        STATE["ffmpeg_rm_pending"] = True

    def _run() -> None:
        ffmpeg = STATE.get("ffmpeg")
        try:
            ok = probe_ffmpeg_realmedia(ffmpeg)
            STATE["ffmpeg_rm"] = bool(ok)
            log(f"[ffmpeg] 后台探测 RealMedia demuxer={'可用' if ok else '不可用'}")
        except Exception as exc:
            STATE["ffmpeg_rm"] = False
            log(f"[ffmpeg] 后台探测 RealMedia 失败: {exc}")
        finally:
            STATE["ffmpeg_rm_pending"] = False

    threading.Thread(target=_run, daemon=True, name="ffmpeg-rm-probe").start()
    return True


def default_transcode_out_ext(item: dict) -> str:
    kind = (item.get("kind") or "").lower()
    ext = (item.get("ext") or "").lower()
    if kind in ("m3u8", "ts_set") or ext in {".m3u8", ".ts"} or ext in BROWSER_HARD_EXTS:
        return "mp4"
    plain = ext.lstrip(".")
    if plain in TRANSCODE_OUT_EXTS:
        return plain
    return "mp4"


def resolve_transcode_out_ext(requested: str | None, item: dict, encoder: str) -> tuple[str, str]:
    """Return (out_ext, note). WebM + H.264/H.265 is remapped to mkv."""
    allowed = set(TRANSCODE_OUT_EXTS)
    req = (requested or "").lstrip(".").lower()
    if req not in allowed:
        req = default_transcode_out_ext(item)
    note = ""
    enc = (encoder or "auto").strip().lower()
    if req == "webm" and enc in {"h264", "h265"}:
        note = "WebM 不适合 H.264/H.265，已改为 MKV"
        req = "mkv"
    return req, note


def normalize_video_encoder(raw) -> str:
    enc = str(raw or "auto").strip().lower()
    if enc in ("auto", "h264", "h265"):
        return enc
    return "auto"


def normalize_audio_encoder(raw) -> str:
    """auto = keep source stream when possible; else force a common codec."""
    enc = str(raw or "auto").strip().lower()
    if enc in ("auto", "copy", "original", "src", "source"):
        return "auto"
    if enc in ("aac", "mp3", "opus", "ac3"):
        return enc
    # tolerate ffprobe-style aliases
    if enc in ("libmp3lame", "mp3float"):
        return "mp3"
    if enc in ("libopus",):
        return "opus"
    if enc in ("eac3", "ac-3", "dolby"):
        return "ac3"
    return "auto"


def resolve_audio_encoder_for_container(
    audio_encoder: str,
    out_ext: str,
) -> tuple[str, str]:
    """Return (audio_encoder, note). WebM only reliably carries Opus."""
    enc = normalize_audio_encoder(audio_encoder)
    ext = (out_ext or "mp4").lstrip(".").lower()
    if ext == "webm" and enc in {"aac", "mp3", "ac3"}:
        return "opus", "WebM 仅支持 Opus，音频已改为 Opus"
    return enc, ""


def normalize_scale(raw, height=None) -> int:
    try:
        value = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    if value not in (0, 720, 1080):
        return 0
    if value and height is not None:
        try:
            h = int(height)
        except (TypeError, ValueError):
            return value
        if h <= value + 8:
            return 0
    return value


def _ffmpeg_popen_flags() -> int:
    flags = 0
    if sys.platform == "win32":
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        flags |= getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
    return flags


def _humanize_ffmpeg_err(err: str, *, src_ext: str = "") -> str:
    text = (err or "").strip()
    low = text.lower()
    ext = (src_ext or "").lower()
    if ext in RM_EXTS or "realmedia" in low or "rmvb" in low:
        markers = (
            "unknown format",
            "invalid data",
            "demuxer",
            "decoder",
            "not found",
            "could not find codec",
            "probe",
            "failed to open",
        )
        if any(m in low for m in markers) or not text:
            return RM_UNAVAILABLE_MSG
    return text[:400] or "ffmpeg 失败"


def list_convert_jobs(limit: int = 40) -> list[dict]:
    with _convert_lock:
        # Must parenthesize: `x or {}.values()` binds as `x or ({}.values())`,
        # so a non-empty jobs dict would be listed as its string keys.
        raw = list((STATE.get("convert_jobs") or {}).values())
    jobs = [j for j in raw if isinstance(j, dict)]
    jobs.sort(key=lambda j: float(j.get("created") or 0), reverse=True)
    return [_convert_job_public(j) for j in jobs[:limit]]


def enqueue_convert_job(
    vid: str,
    kind: str = "mp4",
    name: str = "",
    root: str | None = None,
    target_fps: int | None = None,
    out_ext: str | None = None,
    scale: int | None = None,
    video_encoder: str | None = None,
    audio_encoder: str | None = None,
) -> tuple[bool, str, str]:
    """Enqueue convert/fix-audio/fps30/transcode job. Returns (ok, msg, job_id)."""
    kind = (kind or "mp4").strip().lower()
    if kind not in ("mp4", "fix_audio", "fps30", "transcode"):
        return False, "未知任务类型", ""
    stored_target = None
    if kind in ("fps30", "transcode") and target_fps is not None and target_fps != "":
        try:
            stored_target = int(round(float(target_fps)))
        except (TypeError, ValueError):
            return False, "目标帧率无效", ""
        if stored_target < 1 or stored_target > 119:
            return False, "目标帧率无效（需 1–119）", ""
    elif kind == "fps30":
        stored_target = 30
    stored_encoder = normalize_video_encoder(video_encoder) if kind == "transcode" else ""
    stored_audio = normalize_audio_encoder(audio_encoder) if kind == "transcode" else ""
    stored_scale = normalize_scale(scale) if kind == "transcode" else 0
    stored_ext = ""
    if kind == "transcode":
        ext = (out_ext or "mp4").lstrip(".").lower()
        stored_ext = ext if ext in TRANSCODE_OUT_EXTS else "mp4"
        if stored_ext == "webm" and stored_encoder in {"h264", "h265"}:
            stored_ext = "mkv"
        stored_audio, _ = resolve_audio_encoder_for_container(stored_audio, stored_ext)
    try:
        root = str(Path(root).expanduser().resolve()) if root else None
    except OSError:
        root = str(root).strip() if root else None
    log(
        f"[转换队列] 入队请求 kind={kind} vid={vid} root={root or ''} "
        f"name={name or ''} target_fps={stored_target or '-'} "
        f"out_ext={stored_ext or '-'} scale={stored_scale or 0} "
        f"encoder={stored_encoder or '-'} audio={stored_audio or '-'}"
    )
    with _convert_lock:
        for jid, job in STATE["convert_jobs"].items():
            job_root = job.get("root") or ""
            try:
                job_root = str(Path(job_root).expanduser().resolve()) if job_root else ""
            except OSError:
                job_root = str(job_root).strip()
            same_target = True
            if kind == "fps30":
                same_target = int(job.get("target_fps") or 30) == stored_target
            elif kind == "transcode":
                same_target = (
                    int(job.get("target_fps") or 0) == int(stored_target or 0)
                    and (job.get("out_ext") or "") == stored_ext
                    and int(job.get("scale") or 0) == int(stored_scale or 0)
                    and (job.get("video_encoder") or "auto") == (stored_encoder or "auto")
                    and (job.get("audio_encoder") or "auto") == (stored_audio or "auto")
                )
            if (
                job.get("vid") == vid
                and job_root.casefold() == (root or "").casefold()
                and job.get("kind", "mp4") == kind
                and same_target
                and job.get("status") in ("queued", "running")
            ):
                return True, "已有同类任务在队列中", jid
        job_id = hashlib.md5(f"{kind}-{vid}-{datetime.now().timestamp()}".encode()).hexdigest()[:12]
        STATE["convert_jobs"][job_id] = {
            "id": job_id,
            "vid": vid,
            "root": root or "",
            "kind": kind,
            "name": name or vid,
            "status": "queued",
            "msg": "排队中…",
            "percent": 0,
            "out_path": "",
            "added_id": "",
            "cancel": False,
            "proc": None,
            "created": time.time(),
            "target_fps": stored_target,
            "out_ext": stored_ext,
            "scale": stored_scale,
            "video_encoder": stored_encoder,
            "audio_encoder": stored_audio,
        }
    pump_convert_queue()
    return True, "已加入转换队列", job_id


def pump_convert_queue() -> None:
    """Start queued jobs up to CONVERT_MAX_PARALLEL."""
    with _convert_lock:
        jobs = STATE.get("convert_jobs") or {}
        running = sum(1 for j in jobs.values() if j.get("status") == "running")
        limit = convert_parallel_limit()
        slots = max(0, limit - running)
        if slots <= 0:
            return
        queued = sorted(
            (j for j in jobs.values() if j.get("status") == "queued" and not j.get("cancel")),
            key=lambda j: j.get("created") or 0,
        )
        to_start = []
        for job in queued[:slots]:
            job["status"] = "running"
            job["msg"] = "准备开始…"
            to_start.append(dict(job))
    for job in to_start:
        jid = job["id"]
        vid = job["vid"]
        root = job.get("root") or None
        kind = job.get("kind") or "mp4"
        if kind == "fix_audio":
            target = _fix_audio_worker
        elif kind == "fps30":
            target = _fps30_worker
        elif kind == "transcode":
            target = _transcode_worker
        else:
            target = _convert_worker
        threading.Thread(
            target=_run_convert_job_wrapper,
            args=(target, jid, vid, root),
            daemon=True,
            name=f"convert-{kind}-{vid[:8]}",
        ).start()


def _run_convert_job_wrapper(worker, job_id: str, vid: str, root: str | None) -> None:
    try:
        worker(job_id, vid, root)
    finally:
        pump_convert_queue()


def _convert_job_update(job_id: str, **kwargs) -> None:
    with _convert_lock:
        job = STATE["convert_jobs"].get(job_id)
        if not job:
            return
        job.update(kwargs)


def _convert_job_cancelled(job_id: str) -> bool:
    with _convert_lock:
        job = STATE["convert_jobs"].get(job_id) or {}
        return bool(job.get("cancel"))


def _parse_ffmpeg_time_seconds(line: str) -> float | None:
    m = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", line)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _probe_input_duration(ffmpeg: str, input_args: list[str]) -> float | None:
    """从 convert 输入参数里取出 -i 路径做 ffprobe。"""
    try:
        i = input_args.index("-i")
        src = input_args[i + 1]
    except (ValueError, IndexError):
        return None
    return probe_duration(ffmpeg, Path(src))


def _kill_convert_proc(proc: subprocess.Popen | None) -> None:
    if not proc or proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _register_converted_mp4(out_path: Path, item_hint: dict | None = None) -> dict | None:
    """把转出的 MP4 登记进所属根目录片库（无需全盘重扫）。"""
    root = root_for_item(item_hint) if item_hint else None
    if root is None:
        root = Path(STATE["root"]) if STATE.get("root") else None
    cache = cache_dir_for_item(item_hint) if item_hint else None
    if cache is None:
        cache = STATE.get("cache_dir")
    if not root or not out_path or not out_path.is_file():
        return None
    try:
        rel = safe_rel(out_path, Path(root))
        st = out_path.stat()
    except (ValueError, OSError):
        return None
    out_ext = (out_path.suffix or ".mp4").lower()
    if is_too_small_video(out_ext, st.st_size):
        return None

    vid = video_id(rel)
    folder = str(Path(rel).parent).replace("\\", "/") if Path(rel).parent != Path(".") else ""
    item = {
        "id": vid,
        "name": out_path.stem,
        "filename": out_path.name,
        "rel": rel,
        "folder": folder,
        "ext": out_ext,
        "size": st.st_size,
        "size_h": format_size(st.st_size),
        "mtime": st.st_mtime,
        "mtime_h": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
        "duration": None,
        "duration_h": "",
        "thumb": f"{vid}{THUMB_EXT}",
        "has_thumb": thumb_file_ready(cache, vid) if cache else False,
        "genres": detect_genres(rel, out_path.stem),
        "_lib_root": str(Path(root).resolve()),
        "_lib_cache": str(cache) if cache else "",
        "_folder_raw": folder,
    }
    ensure_video_taxonomy(item)
    ffmpeg = STATE.get("ffmpeg")
    if ffmpeg:
        info = probe_media_info(ffmpeg, out_path)
        if info.get("ok"):
            _apply_probe_to_item(item, info)
        else:
            item["audio_codec"] = "aac"
            item["audio_hard"] = False
            item["probe_ver"] = PROBE_META_VER
    else:
        item["audio_codec"] = "aac"
        item["audio_hard"] = False
        item["probe_ver"] = PROBE_META_VER

    # 写回所属根的索引，不能把统一片库写入单盘。
    root_s = str(Path(root).resolve())
    if not save_library_item(item, allow_insert=True):
        raise OSError(f"无法保存转换结果索引: {root_s}")

    # 刷新统一片库视图
    try:
        from vg.roots import get_mounted_roots, publish_unified_library

        if len(get_mounted_roots()) > 1:
            publish_unified_library()
        else:
            videos = list(STATE.get("videos") or [])
            replaced = False
            for i, v in enumerate(videos):
                if (v.get("rel") or "") == rel or v.get("id") == vid:
                    videos[i] = item
                    replaced = True
                    break
            if not replaced:
                videos.append(item)
            STATE["videos"] = videos
            STATE["tree"] = build_tree(Path(root), videos)
            rebuild_indexes(videos)
            save_root_library(root_s, videos)
    except Exception as e:
        log(f"[转MP4] 刷新片库失败: {e}")

    if ffmpeg and cache and not item.get("has_thumb"):
        def _thumb_one():
            try:
                out = thumb_path(cache, vid)
                if make_thumbnail(ffmpeg, out_path, out, background=True):
                    item["has_thumb"] = True
                    item["thumb_v"] = thumb_version(cache, vid)
                    rebuild_indexes(STATE.get("videos") or [])
                    return True
            except Exception as e:
                log(f"[转MP4] 预览图失败: {e}")
            return False
        submit_thumbnail_job(
            thumbnail_job_key(cache, vid),
            _thumb_one,
            priority=THUMB_PRIORITY_BATCH,
        )
    log(f"[转MP4] 已入库: {rel}")
    return item


def _run_ffmpeg_attempts(
    job_id: str,
    ffmpeg: str,
    attempts: list[tuple[str, list[str]]],
    out_path: Path,
    duration_hint: float | None = None,
    log_tag: str = "转MP4",
    src_ext: str = "",
) -> tuple[bool, str]:
    """按顺序尝试多组 ffmpeg 参数。返回 (ok, msg)。"""
    last_err = ""
    for label, cmd_tail in attempts:
        if _convert_job_cancelled(job_id):
            return False, "已取消"
        try:
            if out_path.exists():
                out_path.unlink()
        except OSError:
            pass
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "info"] + cmd_tail
        _convert_job_update(job_id, status="running", msg=f"正在{label}…", percent=0)
        log(f"[{log_tag}] {label}: {' '.join(cmd[:8])} … → {out_path.name}")
        if duration_hint:
            log(f"[{log_tag}] 将按时长 {duration_hint:.1f}s 上报进度（约每 10% 打一条日志）")
        else:
            log(f"[{log_tag}] 无总时长，将按已处理时间上报（无法显示百分比）")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="ignore",
                creationflags=_ffmpeg_popen_flags(),
            )
            with _convert_lock:
                job = STATE["convert_jobs"].get(job_id)
                if job is not None:
                    job["proc"] = proc
            err_chunks: list[str] = []
            cancelled = False
            last_logged_pct = -10
            last_log_t = 0.0
            assert proc.stderr is not None
            for line in proc.stderr:
                if _convert_job_cancelled(job_id):
                    cancelled = True
                    _kill_convert_proc(proc)
                    break
                err_chunks.append(line)
                if len(err_chunks) > 40:
                    err_chunks = err_chunks[-40:]
                t = _parse_ffmpeg_time_seconds(line)
                if t is None:
                    continue
                if duration_hint and duration_hint > 0:
                    pct = max(0, min(99, int(t * 100 / duration_hint)))
                    _convert_job_update(job_id, percent=pct, msg=f"正在{label}… {pct}%")
                    if pct >= last_logged_pct + 10:
                        last_logged_pct = pct
                        log(f"[{log_tag}] 进度 {pct}% time={t:.1f}s/{duration_hint:.1f}s job={job_id}")
                else:
                    elapsed = int(t)
                    if elapsed >= last_log_t + 15:
                        last_log_t = elapsed
                        mm, ss = divmod(elapsed, 60)
                        _convert_job_update(
                            job_id,
                            percent=0,
                            msg=f"正在{label}… 已处理 {mm}:{ss:02d}（时长未知）",
                        )
                        log(f"[{log_tag}] 进行中 已处理 {mm}:{ss:02d} job={job_id}（无总时长，无法算百分比）")
            code = proc.wait()
            with _convert_lock:
                job = STATE["convert_jobs"].get(job_id)
                if job is not None:
                    job["proc"] = None
            if cancelled or _convert_job_cancelled(job_id):
                try:
                    out_path.unlink(missing_ok=True)
                except OSError:
                    pass
                return False, "已取消"
            if code == 0 and out_path.is_file() and out_path.stat().st_size > 0:
                return True, f"{label}完成"
            last_err = "".join(err_chunks[-12:]).strip() or f"ffmpeg 退出码 {code}"
            log(f"[{log_tag}] {label}失败: {last_err[:200]}")
        except Exception as e:
            last_err = str(e)
            log(f"[{log_tag}] {label}异常: {e}")
            if _convert_job_cancelled(job_id):
                return False, "已取消"
    return False, _humanize_ffmpeg_err(last_err or "转换失败", src_ext=src_ext)


def _run_ffmpeg_convert(
    job_id: str,
    ffmpeg: str,
    input_args: list[str],
    out_path: Path,
    duration_hint: float | None = None,
) -> tuple[bool, str]:
    """先 copy 封装，失败再重编码。返回 (ok, msg)。"""
    attempts = [
        (
            "封装",
            input_args
            + ["-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(out_path)],
        ),
        (
            "转码",
            input_args
            + [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                str(out_path),
            ],
        ),
    ]
    return _run_ffmpeg_attempts(job_id, ffmpeg, attempts, out_path, duration_hint, log_tag="转MP4")


def _run_ffmpeg_fix_audio(
    job_id: str,
    ffmpeg: str,
    src: Path,
    out_path: Path,
    duration_hint: float | None = None,
) -> tuple[bool, str]:
    """视频直拷 + 音频转 AAC；失败再整片重编码。"""
    input_args = ["-i", str(src)]
    attempts = [
        (
            "修复声音",
            input_args
            + [
                "-map", "0:v:0", "-map", "0:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ac", "2",
                "-movflags", "+faststart",
                str(out_path),
            ],
        ),
        (
            "完整转码",
            input_args
            + [
                "-map", "0:v:0", "-map", "0:a:0",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "192k", "-ac", "2",
                "-movflags", "+faststart",
                str(out_path),
            ],
        ),
    ]
    return _run_ffmpeg_attempts(job_id, ffmpeg, attempts, out_path, duration_hint, log_tag="修声音")


def _prepare_convert_input(item: dict) -> tuple[list[str], Path, Path | None, float | None]:
    """
    返回 (ffmpeg -i 前的参数含 -i, 输出目录, 临时文件或None, 时长提示)。
    """
    kind = item.get("kind") or ""
    root = root_for_item(item) or (Path(STATE["root"]) if STATE.get("root") else None)
    if root is None:
        raise FileNotFoundError("未绑定根目录")
    duration = item.get("duration")
    duration_f = float(duration) if duration else None
    tmp_path: Path | None = None

    if kind == "m3u8" or (item.get("ext") or "").lower() == ".m3u8":
        pl = resolve_item_rel(item, item.get("rel") or "")
        if not pl:
            raise FileNotFoundError("找不到 m3u8 文件")
        out_dir = pl.parent
        # 允许本地 m3u8 引用同目录/子目录 .ts 分片
        return [
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-allowed_extensions", "ALL",
            "-i", str(pl),
        ], out_dir, None, duration_f

    if kind == "ts_set":
        segs = item.get("segments") or []
        if len(segs) < 2:
            raise ValueError("分片不足，无法转换")
        paths: list[Path] = []
        for rel in segs:
            p = resolve_item_rel(item, rel)
            if not p:
                raise FileNotFoundError(f"缺少分片: {rel}")
            paths.append(p)
        folder = (item.get("_folder_raw") or item.get("folder") or "").strip("/").replace("\\", "/")
        # 多根时 folder 带盘符前缀，输出应用 rel 所在真实目录
        out_dir = paths[0].parent
        out_dir.mkdir(parents=True, exist_ok=True)
        cache = cache_dir_for_item(item) or STATE.get("cache_dir") or VGDATA_DIR
        Path(cache).mkdir(parents=True, exist_ok=True)
        tmp_path = Path(cache) / f"convert_{item.get('id') or 'tmp'}.ffconcat"
        lines = []
        for p in paths:
            s = str(p.resolve()).replace("\\", "/")
            s = s.replace("'", r"'\''")
            lines.append(f"file '{s}'")
        tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return ["-f", "concat", "-safe", "0", "-i", str(tmp_path)], out_dir, tmp_path, duration_f

    src = resolve_item_rel(item, item.get("rel") or "")
    if not src or not src.is_file():
        raise FileNotFoundError("源文件不存在")
    return ["-i", str(src)], src.parent, None, duration_f


_GENERIC_MEDIA_STEMS = frozenset({"index", "playlist", "master", "video", "stream"})


def _folder_leaf_name(folder: str) -> str:
    """Last useful folder segment; skip ts/media-style generic dirs."""
    parts = [p for p in (folder or "").strip("/").replace("\\", "/").split("/") if p]
    if not parts:
        return ""
    name = parts[-1]
    if name.lower() in SEGMENT_FOLDER_GENERIC and len(parts) >= 2:
        name = parts[-2]
    if name.lower() in _GENERIC_MEDIA_STEMS:
        return ""
    return name


def _parent_dir_base_name(src: Path | None, item: dict | None = None) -> str:
    """Parent folder of the playlist/file; skip generic segment dirs."""
    if src is not None:
        parent = src.parent
        name = parent.name if parent else ""
        if name.lower() in SEGMENT_FOLDER_GENERIC and parent.parent and parent.parent.name:
            name = parent.parent.name
        if name and name.lower() not in _GENERIC_MEDIA_STEMS:
            return name
    if item:
        for key in ("_folder_raw", "folder"):
            leaf = _folder_leaf_name(str(item.get(key) or ""))
            if leaf:
                return leaf
        rel_parent = Path(str(item.get("rel") or "")).parent.name
        if rel_parent and rel_parent.lower() not in _GENERIC_MEDIA_STEMS | SEGMENT_FOLDER_GENERIC:
            return rel_parent
    return ""


def _convert_mp4_base_name(item: dict, src: Path | None = None) -> str:
    """
    默认保留源文件名。
    仅当文件名是 index/playlist/master 等泛化名时，才用父文件夹名
    （并跳过 ts/media 等泛化目录，取上一级）。
    不要对任意 .ts / m3u8 / ts_set 一律用目录名，否则 Downloads/foo.ts
    会错误变成 Downloads.mp4。
    """
    stem = (
        (src.stem if src is not None else "")
        or Path(item.get("filename") or item.get("rel") or "").stem
        or (item.get("name") or "")
    )
    stem_l = str(stem).strip().lower()
    # Only generic stems (index.m3u8 / index.ts, etc.) take the parent folder.
    if stem_l in _GENERIC_MEDIA_STEMS:
        parent = _parent_dir_base_name(src, item)
        if parent:
            return parent
    display = (item.get("name") or "").strip()
    if display and display.lower() not in _GENERIC_MEDIA_STEMS:
        return display
    if stem and stem_l not in _GENERIC_MEDIA_STEMS:
        return str(stem)
    return _parent_dir_base_name(src, item) or "video"


def _transcode_output_base_name(item: dict, src: Path | None = None) -> str:
    """Name for converted output; HLS index.m3u8 → parent folder, never 'index'."""
    return _convert_mp4_base_name(item, src)


def _transcode_stem_suffix(
    *,
    encoder: str,
    target_fps: int | None,
    scale: int,
    out_ext: str,
    audio_encoder: str = "auto",
) -> str:
    bits: list[str] = []
    enc = normalize_video_encoder(encoder)
    if enc != "auto":
        bits.append(enc)
    aenc = normalize_audio_encoder(audio_encoder)
    if aenc != "auto":
        bits.append(aenc)
    if scale:
        bits.append(f"{int(scale)}p")
    if target_fps:
        bits.append(f"{int(target_fps)}fps")
    # Do not embed out_ext in the stem (avoid name_mkv.mkv). Plain remux
    # keeps an empty suffix so HLS→MP4 becomes FolderName.mp4, not index_conv.
    _ = out_ext
    return "_".join(bits)


def _convert_worker(job_id: str, vid: str, root: str | None = None) -> None:
    tmp_path: Path | None = None
    try:
        item = find_video_by_id(vid, prefer_root=root)
        if not item:
            _convert_job_update(job_id, status="error", msg="未找到视频", percent=0)
            return
        ffmpeg = STATE.get("ffmpeg")
        if not ffmpeg:
            _convert_job_update(job_id, status="error", msg="未找到 ffmpeg", percent=0)
            return
        if _convert_job_cancelled(job_id):
            _convert_job_update(job_id, status="cancelled", msg="已取消", percent=0)
            return
        _convert_job_update(job_id, status="running", msg="正在分析时长…", percent=0)
        input_args, out_dir, tmp_path, duration_hint = _prepare_convert_input(item)
        if not duration_hint:
            duration_hint = _probe_input_duration(ffmpeg, input_args)
        if duration_hint:
            _convert_job_update(job_id, duration=duration_hint)
        item_root = root_for_item(item)
        if not _path_under_root(out_dir, item_root):
            _convert_job_update(job_id, status="error", msg="输出目录不在扫描根下", percent=0)
            return
        base_name = _convert_mp4_base_name(
            item,
            out_dir / Path(item.get("filename") or item.get("rel") or "index.m3u8").name,
        )
        out_path = _unique_mp4_path(out_dir, base_name)
        if not _path_under_root(out_path, item_root):
            _convert_job_update(job_id, status="error", msg="输出路径非法", percent=0)
            return
        if _convert_job_cancelled(job_id):
            _convert_job_update(job_id, status="cancelled", msg="已取消", percent=0)
            return
        _convert_job_update(
            job_id,
            status="running",
            msg="开始转换…",
            percent=0,
            out_path=str(out_path),
        )
        ok, msg = _run_ffmpeg_convert(job_id, ffmpeg, input_args, out_path, duration_hint)
        if ok:
            added = _register_converted_mp4(out_path, item)
            _convert_job_update(
                job_id,
                status="done",
                msg=f"已保存并加入片库：{out_path}",
                percent=100,
                out_path=str(out_path),
                added_id=(added or {}).get("id") or "",
            )
            log(f"[转MP4] 完成 {vid} → {out_path}")
        elif msg == "已取消" or _convert_job_cancelled(job_id):
            try:
                out_path.unlink(missing_ok=True)
            except OSError:
                pass
            _convert_job_update(job_id, status="cancelled", msg="已取消", percent=0)
        else:
            try:
                if out_path.exists() and out_path.stat().st_size == 0:
                    out_path.unlink(missing_ok=True)
            except OSError:
                pass
            _convert_job_update(job_id, status="error", msg=msg[:500] or "转换失败", percent=0)
    except Exception as e:
        if _convert_job_cancelled(job_id):
            _convert_job_update(job_id, status="cancelled", msg="已取消", percent=0)
        else:
            _convert_job_update(job_id, status="error", msg=str(e), percent=0)
        log(f"[转MP4] 任务失败 {vid}: {e}")
    finally:
        if tmp_path:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _fps30_video_encode_args(video_codec: str | None) -> tuple[str, list[str]]:
    """Pick video encoder to match source codec when possible.

    Frame-rate change always requires re-encoding; stream copy cannot alter fps.
    Prefer keeping HEVC→HEVC / H.264→H.264 so the output format does not surprise.
    """
    codec = (video_codec or "").strip().lower()
    if codec in ("hevc", "h265", "hev1", "hvc1"):
        return "H.265", [
            "-c:v", "libx265", "-preset", "veryfast", "-crf", "22",
            "-tag:v", "hvc1",
        ]
    if codec in ("av1", "av01"):
        # libaom-av1 is very slow; fall back to H.264 for practical fps jobs.
        return "H.264", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    return "H.264", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]


def _video_encode_args(encoder: str, src_codec: str = "", out_ext: str = "mp4") -> tuple[str, list[str]]:
    enc = normalize_video_encoder(encoder)
    ext = (out_ext or "mp4").lstrip(".").lower()
    if enc == "h264":
        return "H.264", ["-c:v", "libx264", "-preset", "medium", "-crf", "23"]
    if enc == "h265":
        return "H.265", ["-c:v", "libx265", "-preset", "medium", "-crf", "28", "-tag:v", "hvc1"]
    if ext == "webm":
        return "VP9", ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0"]
    return _fps30_video_encode_args(src_codec)


def _vf_args(target_fps: int | None = None, scale: int = 0) -> list[str]:
    filters: list[str] = []
    if target_fps:
        filters.append(f"fps={int(target_fps)}")
    if scale:
        filters.append(f"scale=-2:{int(scale)}")
    if not filters:
        return []
    return ["-vf", ",".join(filters)]


def _container_tail(out_ext: str) -> list[str]:
    ext = (out_ext or "mp4").lstrip(".").lower()
    if ext in {"mp4", "mov"}:
        return ["-movflags", "+faststart"]
    return []


def _audio_reencode_args(out_ext: str, audio_encoder: str = "auto") -> list[str]:
    """FFmpeg audio args. ``auto`` picks a container-friendly default for re-encode."""
    enc = normalize_audio_encoder(audio_encoder)
    ext = (out_ext or "mp4").lstrip(".").lower()
    if enc == "auto":
        if ext == "webm":
            return ["-c:a", "libopus", "-b:a", "128k"]
        return ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
    if enc == "aac":
        return ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
    if enc == "mp3":
        return ["-c:a", "libmp3lame", "-b:a", "192k", "-ac", "2"]
    if enc == "opus":
        return ["-c:a", "libopus", "-b:a", "128k"]
    if enc == "ac3":
        return ["-c:a", "ac3", "-b:a", "192k", "-ac", "2"]
    return ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]


def _run_ffmpeg_fps30(
    job_id: str,
    ffmpeg: str,
    src: Path,
    out_path: Path,
    duration_hint: float | None = None,
    *,
    video_codec: str | None = None,
    target_fps: int = 30,
) -> tuple[bool, str]:
    """Re-encode video to target_fps; keep audio stream as-is when possible."""
    try:
        target_fps = int(target_fps)
    except (TypeError, ValueError):
        target_fps = 30
    if target_fps < 1:
        target_fps = 30
    vf = f"fps={target_fps}"
    v_label, v_args = _fps30_video_encode_args(video_codec)
    log(
        f"[帧率转码] 编码器选择 job={job_id} src_codec={video_codec or '-'} "
        f"target={target_fps} → {v_label} ({' '.join(v_args)})"
    )
    input_args = ["-i", str(src)]
    attempts = [
        (
            f"帧率转码({v_label}@{target_fps})",
            input_args
            + [
                "-map", "0:v:0", "-map", "0:a?",
                "-vf", vf,
                *v_args,
                "-c:a", "copy",
                "-movflags", "+faststart",
                str(out_path),
            ],
        ),
        (
            f"帧率转码({v_label}@{target_fps}+音频重编码)",
            input_args
            + [
                "-map", "0:v:0", "-map", "0:a?",
                "-vf", vf,
                *v_args,
                "-c:a", "aac", "-b:a", "192k", "-ac", "2",
                "-movflags", "+faststart",
                str(out_path),
            ],
        ),
    ]
    # If HEVC encode fails (missing libx265), fall back to H.264 once.
    if v_label == "H.265":
        _, h264_args = _fps30_video_encode_args("h264")
        attempts.append(
            (
                f"帧率转码(回退H.264@{target_fps})",
                input_args
                + [
                    "-map", "0:v:0", "-map", "0:a?",
                    "-vf", vf,
                    *h264_args,
                    "-c:a", "aac", "-b:a", "192k", "-ac", "2",
                    "-movflags", "+faststart",
                    str(out_path),
                ],
            )
        )
    return _run_ffmpeg_attempts(job_id, ffmpeg, attempts, out_path, duration_hint, log_tag="帧率转码")


def _run_ffmpeg_transcode(
    job_id: str,
    ffmpeg: str,
    input_args: list[str],
    out_path: Path,
    duration_hint: float | None = None,
    *,
    encoder: str = "auto",
    audio_encoder: str = "auto",
    src_codec: str = "",
    out_ext: str = "mp4",
    target_fps: int | None = None,
    scale: int = 0,
    src_ext: str = "",
    force_reencode: bool = False,
) -> tuple[bool, str]:
    vf = _vf_args(target_fps, scale)
    audio_enc = normalize_audio_encoder(audio_encoder)
    force_audio = audio_enc != "auto"
    must_reencode = bool(vf) or force_reencode or normalize_video_encoder(encoder) != "auto"
    if (out_ext or "").lstrip(".").lower() == "webm" and normalize_video_encoder(encoder) == "auto":
        must_reencode = True
    v_label, v_args = _video_encode_args(encoder, src_codec, out_ext)
    container = _container_tail(out_ext)
    audio_re = _audio_reencode_args(out_ext, audio_enc)
    a_label = {
        "auto": "原音频",
        "aac": "AAC",
        "mp3": "MP3",
        "opus": "Opus",
        "ac3": "AC-3",
    }.get(audio_enc, audio_enc)
    attempts: list[tuple[str, list[str]]] = []
    if not must_reencode and not force_audio:
        attempts.append((
            "封装",
            input_args + ["-c", "copy", *container, str(out_path)],
        ))
        attempts.append((
            "封装(音频重编码)",
            input_args + ["-c:v", "copy", *audio_re, *container, str(out_path)],
        ))
    elif not must_reencode and force_audio:
        attempts.append((
            f"封装({a_label})",
            input_args + ["-c:v", "copy", *audio_re, *container, str(out_path)],
        ))
    if force_audio:
        attempts.append((
            f"转码({v_label}+{a_label})",
            input_args + ["-map", "0:v:0", "-map", "0:a?", *vf, *v_args, *audio_re, *container, str(out_path)],
        ))
    else:
        attempts.append((
            f"转码({v_label})",
            input_args + ["-map", "0:v:0", "-map", "0:a?", *vf, *v_args, "-c:a", "copy", *container, str(out_path)],
        ))
        attempts.append((
            f"转码({v_label}+音频)",
            input_args + ["-map", "0:v:0", "-map", "0:a?", *vf, *v_args, *audio_re, *container, str(out_path)],
        ))
    return _run_ffmpeg_attempts(
        job_id, ffmpeg, attempts, out_path, duration_hint, log_tag="转换", src_ext=src_ext,
    )


def _transcode_worker(job_id: str, vid: str, root: str | None = None) -> None:
    tmp_path: Path | None = None
    with _convert_lock:
        job = (STATE.get("convert_jobs") or {}).get(job_id) or {}
        requested_fps = job.get("target_fps")
        requested_scale = int(job.get("scale") or 0)
        requested_ext = job.get("out_ext") or ""
        requested_encoder = job.get("video_encoder") or "auto"
        requested_audio = job.get("audio_encoder") or "auto"
    log(
        f"[转换] 开始 job={job_id} vid={vid} fps={requested_fps or '-'} "
        f"scale={requested_scale or 0} ext={requested_ext or '-'} "
        f"encoder={requested_encoder} audio={requested_audio}"
    )
    try:
        item = find_video_by_id(vid, prefer_root=root)
        if not item:
            _convert_job_update(job_id, status="error", msg="未找到视频", percent=0)
            return
        ffmpeg = STATE.get("ffmpeg")
        if not ffmpeg:
            _convert_job_update(job_id, status="error", msg="未找到 ffmpeg", percent=0)
            return
        src_ext = (item.get("ext") or "").lower()
        if src_ext in RM_EXTS:
            if STATE.get("ffmpeg_rm") is None:
                STATE["ffmpeg_rm"] = probe_ffmpeg_realmedia(ffmpeg)
                STATE["ffmpeg_rm_pending"] = False
                log(f"[转换] 任务内补探测 RealMedia={STATE.get('ffmpeg_rm')} job={job_id}")
            if not STATE.get("ffmpeg_rm"):
                _convert_job_update(job_id, status="error", msg=RM_UNAVAILABLE_MSG, percent=0)
                return
        if _convert_job_cancelled(job_id):
            _convert_job_update(job_id, status="cancelled", msg="已取消", percent=0)
            return
        encoder = normalize_video_encoder(requested_encoder)
        out_ext, remap_note = resolve_transcode_out_ext(requested_ext, item, encoder)
        audio_encoder, audio_note = resolve_audio_encoder_for_container(
            requested_audio, out_ext
        )
        notes = [n for n in (remap_note, audio_note) if n]
        if notes:
            log(f"[转换] {'；'.join(notes)} job={job_id}")
        _convert_job_update(job_id, status="running", msg="正在分析片源…", percent=0)
        input_args, out_dir, tmp_path, duration_hint = _prepare_convert_input(item)
        src = resolve_item_rel(item, item.get("rel") or "")
        info = {}
        if src and src.is_file():
            info = probe_media_info(
                ffmpeg,
                src,
                include_duration=True,
                include_audio=False,
                include_video_meta=True,
            )
            if info.get("ok"):
                _apply_probe_to_item(
                    item,
                    info,
                    include_duration=True,
                    include_audio=False,
                    include_video_meta=True,
                )
        fps = info.get("fps") if info.get("fps") is not None else item.get("fps")
        height = info.get("height") or item.get("height")
        video_codec = info.get("video_codec") or item.get("video_codec") or ""
        target = None
        if requested_fps:
            target = normalize_target_fps(fps, requested_fps) if fps else int(requested_fps)
            if fps and not target:
                _convert_job_update(
                    job_id,
                    status="error",
                    msg=f"目标帧率必须低于源帧率（源 {fps}）",
                    percent=0,
                )
                return
        scale = normalize_scale(requested_scale, height)
        if requested_scale in (720, 1080) and not scale:
            _convert_job_update(
                job_id,
                status="error",
                msg="源画面已小于所选分辨率",
                percent=0,
            )
            return
        if not duration_hint:
            duration_hint = info.get("duration") or _probe_input_duration(ffmpeg, input_args)
        if duration_hint:
            try:
                duration_hint = float(duration_hint)
            except (TypeError, ValueError):
                duration_hint = None
        item_root = root_for_item(item)
        if not _path_under_root(out_dir, item_root):
            _convert_job_update(job_id, status="error", msg="输出目录不在扫描根下", percent=0)
            return
        suffix = _transcode_stem_suffix(
            encoder=encoder,
            target_fps=target,
            scale=scale,
            out_ext=out_ext,
            audio_encoder=audio_encoder,
        )
        src_name = _transcode_output_base_name(item, src)
        out_stem = f"{src_name}_{suffix}" if suffix else src_name
        out_path = _unique_out_path(out_dir, out_stem, f".{out_ext}")
        raw_stem = (src.stem if src else "") or Path(item.get("filename") or "").stem
        log(
            f"[转码命名] job={job_id} kind={item.get('kind') or '-'} "
            f"src_stem={raw_stem or '-'} → base={src_name} out={out_path.name}"
        )
        if not _path_under_root(out_path, item_root):
            _convert_job_update(job_id, status="error", msg="输出路径非法", percent=0)
            return
        force_reencode = src_ext in RM_EXTS
        msg_bits = []
        if target:
            msg_bits.append(f"{int(round(float(fps))) if fps else '?'}→{target}fps")
        if scale:
            msg_bits.append(f"{scale}p")
        msg_bits.append(out_ext)
        if audio_encoder != "auto":
            msg_bits.append({
                "aac": "AAC", "mp3": "MP3", "opus": "Opus", "ac3": "AC-3",
            }.get(audio_encoder, audio_encoder))
        note_text = "；".join(notes)
        _convert_job_update(
            job_id,
            status="running",
            msg=("开始转换（" + " ".join(msg_bits) + "）…")
            + (f" {note_text}" if note_text else ""),
            percent=0,
            out_path=str(out_path),
            out_ext=out_ext,
            audio_encoder=audio_encoder,
        )
        ok, msg = _run_ffmpeg_transcode(
            job_id,
            ffmpeg,
            input_args,
            out_path,
            duration_hint,
            encoder=encoder,
            audio_encoder=audio_encoder,
            src_codec=video_codec,
            out_ext=out_ext,
            target_fps=target,
            scale=scale,
            src_ext=src_ext,
            force_reencode=force_reencode,
        )
        if ok:
            added = _register_converted_mp4(out_path, item)
            extra = f"；{note_text}" if note_text else ""
            _convert_job_update(
                job_id,
                status="done",
                msg=f"已生成并加入片库：{out_path.name}{extra}",
                percent=100,
                out_path=str(out_path),
                added_id=(added or {}).get("id") or "",
            )
            log(f"[转换] 完成 job={job_id} {vid} → {out_path}")
        elif msg == "已取消" or _convert_job_cancelled(job_id):
            try:
                out_path.unlink(missing_ok=True)
            except OSError:
                pass
            _convert_job_update(job_id, status="cancelled", msg="已取消", percent=0)
        else:
            try:
                if out_path.exists() and out_path.stat().st_size == 0:
                    out_path.unlink(missing_ok=True)
            except OSError:
                pass
            fail = _humanize_ffmpeg_err(msg, src_ext=src_ext)
            _convert_job_update(job_id, status="error", msg=fail[:500] or "转换失败", percent=0)
    except Exception as e:
        if _convert_job_cancelled(job_id):
            _convert_job_update(job_id, status="cancelled", msg="已取消", percent=0)
        else:
            _convert_job_update(job_id, status="error", msg=str(e), percent=0)
        log(f"[转换] 异常 job={job_id} vid={vid}: {e}")
    finally:
        if tmp_path:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _fps30_worker(job_id: str, vid: str, root: str | None = None) -> None:
    """High-fps 2K/4K → user-chosen lower fps. Separate from format convert / fix-audio."""
    with _convert_lock:
        requested = ((STATE.get("convert_jobs") or {}).get(job_id) or {}).get("target_fps")
    log(f"[帧率转码] 开始 job={job_id} vid={vid} root={root or ''} target={requested or '-'}")
    try:
        item = find_video_by_id(vid, prefer_root=root)
        if not item:
            log(f"[帧率转码] 失败：未找到视频 job={job_id} vid={vid}")
            _convert_job_update(job_id, status="error", msg="未找到视频", percent=0)
            return
        ffmpeg = STATE.get("ffmpeg")
        if not ffmpeg:
            log(f"[帧率转码] 失败：无 ffmpeg job={job_id}")
            _convert_job_update(job_id, status="error", msg="未找到 ffmpeg", percent=0)
            return
        if _convert_job_cancelled(job_id):
            _convert_job_update(job_id, status="cancelled", msg="已取消", percent=0)
            return
        kind = item.get("kind") or ""
        if kind in ("m3u8", "ts_set") or (item.get("ext") or "").lower() == ".m3u8":
            log(f"[帧率转码] 失败：流媒体 job={job_id} kind={kind}")
            _convert_job_update(job_id, status="error", msg="流媒体请先转成文件后再做帧率转码", percent=0)
            return
        src = resolve_item_rel(item, item.get("rel") or "")
        if not src or not src.is_file():
            log(f"[帧率转码] 失败：源文件不存在 job={job_id} src={src}")
            _convert_job_update(job_id, status="error", msg="源文件不存在", percent=0)
            return
        _convert_job_update(job_id, status="running", msg="正在检测分辨率/帧率…", percent=0)
        info = probe_media_info(
            ffmpeg,
            src,
            include_duration=True,
            include_audio=False,
            include_video_meta=True,
        )
        if not info.get("ok"):
            log(f"[帧率转码] 探测失败 job={job_id} err={info.get('err')}")
            _convert_job_update(job_id, status="error", msg=info.get("err") or "无法读取文件", percent=0)
            return
        _apply_probe_to_item(
            item,
            info,
            include_duration=True,
            include_audio=False,
            include_video_meta=True,
        )
        width = info.get("width") or item.get("width")
        height = info.get("height") or item.get("height")
        fps = info.get("fps") if info.get("fps") is not None else item.get("fps")
        video_codec = info.get("video_codec") or item.get("video_codec") or ""
        log(
            f"[帧率转码] 探测结果 job={job_id} "
            f"{width}x{height}@{fps} vcodec={video_codec or '-'} src={src.name}"
        )
        if not is_2k_or_4k(width, height):
            log(f"[帧率转码] 拒绝：非 2K/4K job={job_id} {width}x{height}")
            _convert_job_update(
                job_id,
                status="error",
                msg=f"仅支持 2K/4K（当前 {width or '?'}x{height or '?'}）",
                percent=0,
            )
            return
        if not fps_can_halve_to_30(fps):
            log(f"[帧率转码] 拒绝：非 60/90/120 档 job={job_id} fps={fps}")
            _convert_job_update(
                job_id,
                status="error",
                msg=f"仅支持 60/90/120fps 源（当前 {fps or '?'} fps）",
                percent=0,
            )
            return
        target = normalize_target_fps(fps, requested, default=30)
        if not target:
            log(f"[帧率转码] 拒绝：目标帧率无效 job={job_id} src={fps} want={requested}")
            _convert_job_update(
                job_id,
                status="error",
                msg=f"目标帧率必须低于源帧率（源 {fps or '?'}）",
                percent=0,
            )
            return
        duration_hint = info.get("duration") or item.get("duration")
        if duration_hint:
            try:
                duration_hint = float(duration_hint)
            except (TypeError, ValueError):
                duration_hint = None
        if not duration_hint:
            duration_hint = probe_duration(ffmpeg, src)
        if duration_hint:
            log(f"[帧率转码] 时长={duration_hint:.1f}s job={job_id}")
        else:
            log(f"[帧率转码] 警告：未探测到时长，进度只能显示已处理时间 job={job_id}")
        out_dir = src.parent
        item_root = root_for_item(item)
        if not _path_under_root(out_dir, item_root):
            log(f"[帧率转码] 拒绝：输出目录越界 job={job_id} out_dir={out_dir}")
            _convert_job_update(job_id, status="error", msg="输出目录不在扫描根下", percent=0)
            return
        fps_label = int(round(float(fps))) if fps else 60
        base_name = f"{src.stem}_{fps_label}to{target}fps"
        out_path = _unique_out_path(out_dir, base_name, ".mp4")
        if not _path_under_root(out_path, item_root):
            log(f"[帧率转码] 拒绝：输出路径非法 job={job_id} out={out_path}")
            _convert_job_update(job_id, status="error", msg="输出路径非法", percent=0)
            return
        if _convert_job_cancelled(job_id):
            _convert_job_update(job_id, status="cancelled", msg="已取消", percent=0)
            return
        log(f"[帧率转码] 开始转码 job={job_id} {fps_label}→{target} out={out_path.name}")
        _convert_job_update(
            job_id,
            status="running",
            msg=f"开始帧率转码（{fps_label}→{target} fps）…",
            percent=0,
            out_path=str(out_path),
        )
        ok, msg = _run_ffmpeg_fps30(
            job_id,
            ffmpeg,
            src,
            out_path,
            duration_hint,
            video_codec=video_codec,
            target_fps=target,
        )
        if ok:
            added = _register_converted_mp4(out_path, item)
            _convert_job_update(
                job_id,
                status="done",
                msg=f"已生成 {target}fps 版本并加入片库：{out_path.name}",
                percent=100,
                out_path=str(out_path),
                added_id=(added or {}).get("id") or "",
            )
            log(f"[帧率转码] 完成 job={job_id} {vid} → {out_path}")
        elif msg == "已取消" or _convert_job_cancelled(job_id):
            try:
                out_path.unlink(missing_ok=True)
            except OSError:
                pass
            log(f"[帧率转码] 已取消 job={job_id}")
            _convert_job_update(job_id, status="cancelled", msg="已取消", percent=0)
        else:
            try:
                if out_path.exists() and out_path.stat().st_size == 0:
                    out_path.unlink(missing_ok=True)
            except OSError:
                pass
            log(f"[帧率转码] 转码失败 job={job_id} msg={msg[:200] if msg else ''}")
            _convert_job_update(job_id, status="error", msg=msg[:500] or "帧率转码失败", percent=0)
    except Exception as e:
        if _convert_job_cancelled(job_id):
            _convert_job_update(job_id, status="cancelled", msg="已取消", percent=0)
        else:
            _convert_job_update(job_id, status="error", msg=str(e), percent=0)
        log(f"[帧率转码] 异常 job={job_id} vid={vid}: {e}")


def _fix_audio_worker(job_id: str, vid: str, root: str | None = None) -> None:
    try:
        item = find_video_by_id(vid, prefer_root=root)
        if not item:
            _convert_job_update(job_id, status="error", msg="未找到视频", percent=0)
            return
        ffmpeg = STATE.get("ffmpeg")
        if not ffmpeg:
            _convert_job_update(job_id, status="error", msg="未找到 ffmpeg", percent=0)
            return
        if _convert_job_cancelled(job_id):
            _convert_job_update(job_id, status="cancelled", msg="已取消", percent=0)
            return
        kind = item.get("kind") or ""
        if kind in ("m3u8", "ts_set") or (item.get("ext") or "").lower() == ".m3u8":
            _convert_job_update(job_id, status="error", msg="流媒体请用「转成 MP4」", percent=0)
            return
        src = resolve_item_rel(item, item.get("rel") or "")
        if not src or not src.is_file():
            _convert_job_update(job_id, status="error", msg="源文件不存在", percent=0)
            return
        _convert_job_update(job_id, status="running", msg="正在检测音频…", percent=0)
        info = probe_media_info(ffmpeg, src)
        if not info.get("ok"):
            _convert_job_update(job_id, status="error", msg=info.get("err") or "无法读取文件", percent=0)
            return
        _apply_probe_to_item(item, info)
        ac = (info.get("audio_codec") or "").strip()
        if not ac:
            _convert_job_update(job_id, status="error", msg="没有音轨，无法修复", percent=0)
            return
        if not info.get("audio_hard"):
            _convert_job_update(
                job_id,
                status="error",
                msg=f"音频已是浏览器友好格式（{ac}），无需修复",
                percent=0,
            )
            return
        duration_hint = info.get("duration") or item.get("duration")
        if duration_hint:
            try:
                duration_hint = float(duration_hint)
            except (TypeError, ValueError):
                duration_hint = None
        out_dir = src.parent
        item_root = root_for_item(item)
        if not _path_under_root(out_dir, item_root):
            _convert_job_update(job_id, status="error", msg="输出目录不在扫描根下", percent=0)
            return
        base_name = f"{src.stem}_browser"
        out_path = _unique_mp4_path(out_dir, base_name)
        if not _path_under_root(out_path, item_root):
            _convert_job_update(job_id, status="error", msg="输出路径非法", percent=0)
            return
        if _convert_job_cancelled(job_id):
            _convert_job_update(job_id, status="cancelled", msg="已取消", percent=0)
            return
        _convert_job_update(
            job_id,
            status="running",
            msg=f"开始修复声音（{ac} → aac）…",
            percent=0,
            out_path=str(out_path),
        )
        ok, msg = _run_ffmpeg_fix_audio(job_id, ffmpeg, src, out_path, duration_hint)
        if ok:
            added = _register_converted_mp4(out_path, item)
            _convert_job_update(
                job_id,
                status="done",
                msg=f"已生成浏览器可播版并加入片库：{out_path.name}",
                percent=100,
                out_path=str(out_path),
                added_id=(added or {}).get("id") or "",
            )
            log(f"[修声音] 完成 {vid} → {out_path}")
        elif msg == "已取消" or _convert_job_cancelled(job_id):
            try:
                out_path.unlink(missing_ok=True)
            except OSError:
                pass
            _convert_job_update(job_id, status="cancelled", msg="已取消", percent=0)
        else:
            try:
                if out_path.exists() and out_path.stat().st_size == 0:
                    out_path.unlink(missing_ok=True)
            except OSError:
                pass
            _convert_job_update(job_id, status="error", msg=msg[:500] or "修复失败", percent=0)
    except Exception as e:
        if _convert_job_cancelled(job_id):
            _convert_job_update(job_id, status="cancelled", msg="已取消", percent=0)
        else:
            _convert_job_update(job_id, status="error", msg=str(e), percent=0)
        log(f"[修声音] 任务失败 {vid}: {e}")
