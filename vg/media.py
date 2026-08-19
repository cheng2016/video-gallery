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
    resolve_under_root,
    resolve_video_path,
    thumb_worker_count,
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
    if not path or not path.is_file():
        return {"ok": False, "err": "文件不存在"}
    ffprobe = _ffprobe_path(ffmpeg)
    if not ffprobe:
        return {"ok": False, "err": "未找到 ffprobe"}
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
        r = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
        )
        err = (r.stderr or "").strip()
        if r.returncode != 0:
            return {"ok": False, "err": (err or "ffprobe 失败")[:120]}
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
            return {"ok": False, "err": "无视频流"}
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
        return result
    except subprocess.TimeoutExpired:
        return {"ok": False, "err": "探测超时"}
    except Exception as e:
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
    out.parent.mkdir(parents=True, exist_ok=True)
    _clear_path_attrs_windows(out)
    if out.exists() and not force:
        try:
            raw = unpack_thumb_bytes(out.read_bytes())
            if raw and raw[:2] == b"\xff\xd8" and len(raw) > 100:
                return True
            out.unlink(missing_ok=True)
        except OSError:
            pass
    elif out.exists() and force:
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass

    if not video.is_file():
        return False

    tmp = out.with_suffix(".tmp.jpg")
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
                        continue
                    _clear_path_attrs_windows(out)
                    out.write_bytes(pack_thumb_bytes(raw))
                    return True
            except Exception:
                continue
        return False
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def save_thumbnail_jpeg(out: Path, jpeg_bytes: bytes) -> bool:
    """把 JPEG 写入预览图文件（按隐私设置加密或明文）。"""
    if not jpeg_bytes or jpeg_bytes[:2] != b"\xff\xd8":
        return False
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        _clear_path_attrs_windows(out)
        out.write_bytes(pack_thumb_bytes(jpeg_bytes))
        return True
    except OSError:
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


def _persist_probed_items(items: list[dict]) -> None:
    """Write probed rows to index.json immediately so a mid-run exit keeps progress."""
    if not items:
        return
    from vg.disk_libs import save_library_item

    for item in items:
        try:
            save_library_item(item)
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
    workers = max(1, min(4, thumb_worker_count(total)))
    STATE["meta_progress"] = f"{label}探测 0/{total}（{workers} 线程）…"
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
    flush_every = 5
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
                    STATE["meta_progress"] = f"{label}探测 {done}/{total}（可读 {ok_n}，异常 {fail_n}）…"
                    if done % 40 == 0 or done == total:
                        log(f"[元数据] ({done}/{total}) {'OK' if ok else '异常'} {name}")
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
        if not need:
            STATE["meta_progress"] = ""
            return
        log(f"[元数据] 后台探测 {len(need)} 个（已有时长/音频的会跳过）…")
        ok_n, fail_n = enrich_metadata_parallel(need, label="后台")
        current = list(STATE.get("videos") or [])
        rebuild_indexes(current)
        # Incremental saves already happened; one last pass covers a short tail.
        _persist_probed_items(need)
        STATE["meta_progress"] = f"元数据完成：可读 {ok_n}，异常 {fail_n}"
        log(f"[元数据] 完成：可读 {ok_n}，异常 {fail_n}")
    except Exception as e:
        STATE["meta_progress"] = f"元数据探测失败: {e}"
        log(f"[元数据] 失败: {e}")
    finally:
        _state._meta_running = False
        threading.Timer(4.0, lambda: STATE.update(meta_progress="")).start()
