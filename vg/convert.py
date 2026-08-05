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

from vg.cache import save_index, thumb_file_ready, thumb_path, thumb_version
from vg.config import (
    CONVERT_MAX_PARALLEL,
    PROBE_META_VER,
    SEGMENT_FOLDER_GENERIC,
    THUMB_EXT,
    VGDATA_DIR,
)
from vg.disk_libs import cache_dir_for_item, resolve_item_rel, root_for_item
from vg.genres import detect_genres
from vg.media import (
    _apply_probe_to_item,
    make_thumbnail,
    probe_duration,
    probe_media_info,
)
from vg.scan import build_tree, find_video_by_id, rebuild_indexes
from vg.state import STATE, _convert_lock
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
        "kind": job.get("kind") or "mp4",
        "name": job.get("name") or "",
        "status": job.get("status") or "error",
        "msg": job.get("msg") or "",
        "percent": int(job.get("percent") or 0),
        "out_path": job.get("out_path") or "",
        "added_id": job.get("added_id") or "",
    }


def list_convert_jobs(limit: int = 40) -> list[dict]:
    with _convert_lock:
        jobs = list(STATE.get("convert_jobs") or {}.values())
    jobs.sort(key=lambda j: j.get("created") or 0, reverse=True)
    return [_convert_job_public(j) for j in jobs[:limit]]


def enqueue_convert_job(vid: str, kind: str = "mp4", name: str = "") -> tuple[bool, str, str]:
    """Enqueue convert/fix-audio job. Returns (ok, msg, job_id)."""
    kind = (kind or "mp4").strip().lower()
    if kind not in ("mp4", "fix_audio"):
        return False, "未知任务类型", ""
    with _convert_lock:
        for jid, job in STATE["convert_jobs"].items():
            if job.get("vid") == vid and job.get("kind", "mp4") == kind and job.get("status") in ("queued", "running"):
                return True, "已有同类任务在队列中", jid
        job_id = hashlib.md5(f"{kind}-{vid}-{datetime.now().timestamp()}".encode()).hexdigest()[:12]
        STATE["convert_jobs"][job_id] = {
            "id": job_id,
            "vid": vid,
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
        kind = job.get("kind") or "mp4"
        target = _fix_audio_worker if kind == "fix_audio" else _convert_worker
        threading.Thread(
            target=_run_convert_job_wrapper,
            args=(target, jid, vid),
            daemon=True,
            name=f"convert-{kind}-{vid[:8]}",
        ).start()


def _run_convert_job_wrapper(worker, job_id: str, vid: str) -> None:
    try:
        worker(job_id, vid)
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
    if is_too_small_video(".mp4", st.st_size):
        return None

    vid = video_id(rel)
    folder = str(Path(rel).parent).replace("\\", "/") if Path(rel).parent != Path(".") else ""
    item = {
        "id": vid,
        "name": out_path.stem,
        "filename": out_path.name,
        "rel": rel,
        "folder": folder,
        "ext": ".mp4",
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

    # 写回该根的索引（disk_libs / 若是当前单根则 STATE）
    root_s = str(Path(root).resolve())
    libs = STATE.setdefault("disk_libs", {})
    lib = libs.get(root_s) or {"root": root_s, "cache_dir": str(cache) if cache else None, "by_id": {}, "updated": time.time()}
    by_id = dict(lib.get("by_id") or {})
    by_id[vid] = item
    lib["by_id"] = by_id
    lib["updated"] = time.time()
    libs[root_s] = lib
    if cache:
        try:
            save_index(Path(cache), Path(root), list(by_id.values()))
        except Exception as e:
            log(f"[转MP4] 保存索引失败: {e}")

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
            if cache:
                save_index(cache, Path(root), videos)
    except Exception as e:
        log(f"[转MP4] 刷新片库失败: {e}")

    if ffmpeg and cache and not item.get("has_thumb"):
        def _thumb_one():
            try:
                out = thumb_path(cache, vid)
                if make_thumbnail(ffmpeg, out_path, out):
                    item["has_thumb"] = True
                    item["thumb_v"] = thumb_version(cache, vid)
                    rebuild_indexes(STATE.get("videos") or [])
            except Exception as e:
                log(f"[转MP4] 预览图失败: {e}")
        threading.Thread(target=_thumb_one, daemon=True, name="convert-thumb").start()
    log(f"[转MP4] 已入库: {rel}")
    return item


def _run_ffmpeg_attempts(
    job_id: str,
    ffmpeg: str,
    attempts: list[tuple[str, list[str]]],
    out_path: Path,
    duration_hint: float | None = None,
    log_tag: str = "转MP4",
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
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="ignore",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
            )
            with _convert_lock:
                job = STATE["convert_jobs"].get(job_id)
                if job is not None:
                    job["proc"] = proc
            err_chunks: list[str] = []
            cancelled = False
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
                if t is not None and duration_hint and duration_hint > 0:
                    pct = max(0, min(99, int(t * 100 / duration_hint)))
                    _convert_job_update(job_id, percent=pct, msg=f"正在{label}… {pct}%")
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
    return False, last_err or "转换失败"


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

    raise ValueError("仅支持 m3u8 / TS 合集")


def _convert_mp4_base_name(item: dict) -> str:
    """
    MP4 文件名优先用 m3u8/合集所在目录名（跳过 ts/media 等泛化目录，取上一级）；
    没有可用目录名时再回退到条目展示名 / 文件名。
    """
    folder = (item.get("folder") or "").strip("/").replace("\\", "/")
    if folder:
        parts = [p for p in folder.split("/") if p]
        name = parts[-1] if parts else ""
        if name.lower() in SEGMENT_FOLDER_GENERIC and len(parts) >= 2:
            name = parts[-2]
        if name and name.lower() not in {"index", "playlist", "master"}:
            return name
    return item.get("name") or Path(item.get("filename") or "video").stem


def _convert_worker(job_id: str, vid: str) -> None:
    tmp_path: Path | None = None
    try:
        item = find_video_by_id(vid)
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
        base_name = _convert_mp4_base_name(item)
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


def _fix_audio_worker(job_id: str, vid: str) -> None:
    try:
        item = find_video_by_id(vid)
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

