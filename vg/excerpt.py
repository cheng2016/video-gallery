# -*- coding: utf-8 -*-
"""Player excerpt jobs: animated GIF and a saved video clip."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from vg.catalog_repository import find_video_by_id
from vg.convert import (
    _convert_job_cancelled,
    _convert_job_update,
    _humanize_ffmpeg_err,
    _path_under_root,
    _register_converted_mp4,
    _run_ffmpeg_attempts,
    _transcode_output_base_name,
    _unique_out_path,
)
from vg.disk_libs import resolve_item_rel, root_for_item
from vg.media import probe_duration
from vg.state import STATE, _convert_lock
from vg.util import log

GIF_MAX_SECONDS = 20.0
CLIP_MAX_SECONDS = 6 * 3600.0
EXCERPT_MIN_SECONDS = 0.2
GIF_FPS_CHOICES = (8, 10, 12, 15)
GIF_WIDTH_CHOICES = (240, 320, 480, 640)


def _as_positive_duration(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0.05:
        return None
    return number


def normalize_excerpt_request(
    mode: str,
    start,
    end,
    duration=None,
    fps=None,
    width=None,
    accurate: bool = False,
) -> tuple[dict | None, str]:
    """Validate a GIF or clip range. Returns (spec, error)."""
    kind = (mode or "").strip().lower()
    if kind not in ("gif", "clip"):
        return None, "未知片段类型"
    try:
        start_f = float(start)
        end_f = float(end)
    except (TypeError, ValueError):
        return None, "起止时间无效"
    if start_f < 0 or end_f < 0:
        return None, "时间不能为负"
    known = _as_positive_duration(duration)
    if known is not None:
        if start_f >= known - 0.05:
            return None, "起点已经到片尾"
        end_f = min(end_f, known)
    if end_f <= start_f + 0.05:
        return None, "终点必须晚于起点"
    span = end_f - start_f
    if span < EXCERPT_MIN_SECONDS:
        return None, "片段太短"
    cap = GIF_MAX_SECONDS if kind == "gif" else CLIP_MAX_SECONDS
    if span > cap + 0.05:
        if kind == "gif":
            return None, f"GIF 最长 {int(GIF_MAX_SECONDS)} 秒，更长请用「截取片段」"
        return None, "单次最多截取 6 小时"
    spec: dict = {
        "start": round(start_f, 3),
        "end": round(end_f, 3),
        "span": round(span, 3),
    }
    if kind == "gif":
        try:
            fps_i = int(fps if fps is not None else 10)
            width_i = int(width if width is not None else 480)
        except (TypeError, ValueError):
            return None, "GIF 参数无效"
        if fps_i not in GIF_FPS_CHOICES:
            return None, "帧率只支持 8 / 10 / 12 / 15"
        if width_i not in GIF_WIDTH_CHOICES:
            return None, "宽度只支持 240 / 320 / 480 / 640"
        spec["fps"] = fps_i
        spec["width"] = width_i
    else:
        spec["accurate"] = bool(accurate)
    return spec, ""


def excerpt_time_tag(seconds: float) -> str:
    return f"{float(seconds):.1f}".replace(".", "p")


def excerpt_output_stem(base: str, mode: str, start: float, end: float) -> str:
    tag = "gif" if mode == "gif" else "clip"
    return f"{base}_{tag}_{excerpt_time_tag(start)}-{excerpt_time_tag(end)}"


def gif_filter(fps: int, width: int) -> str:
    return (
        f"fps={int(fps)},scale={int(width)}:-2:flags=lanczos,"
        "split[s0][s1];[s0]palettegen=stats_mode=diff:max_colors=128[p];"
        "[s1][p]paletteuse=dither=bayer:bayer_scale=5"
    )


def gif_ffmpeg_tail(
    src: Path,
    out: Path,
    start: float,
    duration: float,
    fps: int,
    width: int,
    *,
    palette: bool = True,
) -> list[str]:
    head = [
        "-ss", f"{float(start):.3f}",
        "-t", f"{float(duration):.3f}",
        "-i", str(src),
        "-an",
    ]
    if palette:
        head.extend(["-filter_complex", gif_filter(fps, width)])
    else:
        head.extend(["-vf", f"fps={int(fps)},scale={int(width)}:-2:flags=lanczos"])
    head.extend(["-loop", "0", str(out)])
    return head


def clip_ffmpeg_tail(
    src: Path,
    out: Path,
    start: float,
    duration: float,
    *,
    accurate: bool,
) -> list[str]:
    if accurate:
        # Fast seek to just before the cut, then frame-accurate -ss after -i.
        start_f = max(0.0, float(start))
        preroll = 0.0
        fine = start_f
        if start_f > 2.0:
            preroll = start_f - 2.0
            fine = 2.0
        cmd: list[str] = []
        if preroll > 0:
            cmd.extend(["-ss", f"{preroll:.3f}"])
        cmd.extend(["-i", str(src)])
        if fine > 0:
            cmd.extend(["-ss", f"{fine:.3f}"])
        cmd.extend([
            "-t", f"{float(duration):.3f}",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "20",
            "-c:a", "aac",
            "-b:a", "160k",
            "-movflags", "+faststart",
            str(out),
        ])
        return cmd
    return [
        "-ss", f"{float(start):.3f}",
        "-t", f"{float(duration):.3f}",
        "-i", str(src),
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        str(out),
    ]


def output_under_library(path: Path, roots: list[str] | tuple[str, ...]) -> bool:
    if not path:
        return False
    for root in roots:
        if root and _path_under_root(path, Path(root)):
            return True
    return False


def open_local_file(path: Path) -> None:
    path_str = str(path)
    if sys.platform == "win32":
        os.startfile(path_str)  # type: ignore[attr-defined]
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", path_str])
        return
    subprocess.Popen(["xdg-open", path_str])


def _stream_item(item: dict) -> bool:
    kind = item.get("kind") or ""
    ext = (item.get("ext") or "").lower()
    return kind in ("m3u8", "ts_set") or ext == ".m3u8"


def _load_excerpt_spec(job_id: str) -> dict:
    with _convert_lock:
        job = (STATE.get("convert_jobs") or {}).get(job_id) or {}
        raw = job.get("excerpt") or {}
        return dict(raw) if isinstance(raw, dict) else {}


def run_excerpt_job(job_id: str, vid: str, root: str | None = None) -> None:
    """GIF or clip worker. Mode comes from the queued job kind."""
    out_path: Path | None = None
    try:
        with _convert_lock:
            job = (STATE.get("convert_jobs") or {}).get(job_id) or {}
            mode = (job.get("kind") or "").strip().lower()
        spec = _load_excerpt_spec(job_id)
        log(f"[片段] 开始 job={job_id} vid={vid} mode={mode} root={root or ''} spec={spec}")
        if mode not in ("gif", "clip") or not spec:
            log(f"[片段] 失败：参数无效 job={job_id} mode={mode or '-'}")
            _convert_job_update(job_id, status="error", msg="片段参数无效", percent=0)
            return
        item = find_video_by_id(vid, prefer_root=root)
        if not item:
            log(f"[片段] 失败：未找到视频 job={job_id} vid={vid}")
            _convert_job_update(job_id, status="error", msg="未找到视频", percent=0)
            return
        ffmpeg = STATE.get("ffmpeg")
        if not ffmpeg:
            log(f"[片段] 失败：无 ffmpeg job={job_id}")
            _convert_job_update(job_id, status="error", msg="未找到 ffmpeg", percent=0)
            return
        if _convert_job_cancelled(job_id):
            log(f"[片段] 已取消 job={job_id}（尚未开始）")
            _convert_job_update(job_id, status="cancelled", msg="已取消", percent=0)
            return
        if _stream_item(item):
            log(f"[片段] 失败：流媒体 job={job_id} kind={item.get('kind') or '-'} ext={item.get('ext') or '-'}")
            _convert_job_update(
                job_id,
                status="error",
                msg="流媒体请先转成文件，再制作 GIF 或截取片段",
                percent=0,
            )
            return
        src = resolve_item_rel(item, item.get("rel") or "")
        if not src or not src.is_file():
            log(f"[片段] 失败：源文件不存在 job={job_id} src={src}")
            _convert_job_update(job_id, status="error", msg="源文件不存在", percent=0)
            return
        _convert_job_update(job_id, status="running", msg="正在确认时长…", percent=0)
        probed = probe_duration(ffmpeg, src)
        log(f"[片段] 探测时长 job={job_id} src={src.name} probed={probed if probed else '未知'}")
        spec, err = normalize_excerpt_request(
            mode,
            spec.get("start"),
            spec.get("end"),
            duration=probed,
            fps=spec.get("fps"),
            width=spec.get("width"),
            accurate=bool(spec.get("accurate")),
        )
        if not spec:
            log(f"[片段] 失败：起止时间 job={job_id} {err or '起止时间无效'}")
            _convert_job_update(job_id, status="error", msg=err or "起止时间无效", percent=0)
            return
        item_root = root_for_item(item)
        out_dir = src.parent
        if not _path_under_root(out_dir, item_root):
            log(f"[片段] 失败：输出目录越界 job={job_id} out_dir={out_dir}")
            _convert_job_update(job_id, status="error", msg="输出目录不在扫描根下", percent=0)
            return
        base = _transcode_output_base_name(item, src)
        stem = excerpt_output_stem(base, mode, spec["start"], spec["end"])
        suffix = ".gif" if mode == "gif" else ".mp4"
        out_path = _unique_out_path(out_dir, stem, suffix)
        if not _path_under_root(out_path, item_root):
            log(f"[片段] 失败：输出路径非法 job={job_id} out={out_path}")
            _convert_job_update(job_id, status="error", msg="输出路径非法", percent=0)
            return
        span = float(spec["span"])
        if mode == "gif":
            attempts = [
                (
                    "制作 GIF",
                    gif_ffmpeg_tail(
                        src, out_path, spec["start"], span, spec["fps"], spec["width"], palette=True,
                    ),
                ),
                (
                    "制作 GIF",
                    gif_ffmpeg_tail(
                        src, out_path, spec["start"], span, spec["fps"], spec["width"], palette=False,
                    ),
                ),
            ]
            log_tag = "制作GIF"
        elif spec.get("accurate"):
            attempts = [(
                "精确截取",
                clip_ffmpeg_tail(src, out_path, spec["start"], span, accurate=True),
            )]
            log_tag = "截取片段"
        else:
            attempts = [
                ("快速剪切", clip_ffmpeg_tail(src, out_path, spec["start"], span, accurate=False)),
                ("精确截取", clip_ffmpeg_tail(src, out_path, spec["start"], span, accurate=True)),
            ]
            log_tag = "截取片段"
        log(
            f"[片段] 交给 ffmpeg job={job_id} mode={mode} "
            f"{spec['start']}→{spec['end']} span={span:.3f}s "
            f"accurate={bool(spec.get('accurate'))} "
            f"fps={spec.get('fps') or '-'} width={spec.get('width') or '-'} "
            f"out={out_path.name}"
        )
        _convert_job_update(
            job_id,
            status="running",
            msg="开始处理片段…",
            percent=0,
            out_path=str(out_path),
        )
        ok, msg = _run_ffmpeg_attempts(
            job_id,
            ffmpeg,
            attempts,
            out_path,
            span,
            log_tag=log_tag,
            src_ext=src.suffix.lower(),
        )
        if ok:
            if mode == "clip":
                added = _register_converted_mp4(out_path, item, allow_small=True)
                done = f"已截取并加入片库：{out_path.name}" if added else f"已保存：{out_path.name}"
                added_id = (added or {}).get("id") or ""
            else:
                done = f"已生成 GIF：{out_path.name}"
                added_id = ""
            _convert_job_update(
                job_id,
                status="done",
                msg=done,
                percent=100,
                out_path=str(out_path),
                added_id=added_id,
            )
            log(f"[片段] 完成 job={job_id} {vid} → {out_path} added={added_id or '-'}")
            return
        if msg == "已取消" or _convert_job_cancelled(job_id):
            if out_path is not None:
                try:
                    out_path.unlink(missing_ok=True)
                except OSError:
                    pass
            log(f"[片段] 已取消 job={job_id}")
            _convert_job_update(job_id, status="cancelled", msg="已取消", percent=0)
            return
        fail = _humanize_ffmpeg_err(msg, src_ext=src.suffix.lower())
        _convert_job_update(job_id, status="error", msg=fail[:500] or "片段处理失败", percent=0)
        log(f"[片段] 失败 job={job_id} msg={fail[:200]}")
    except Exception as exc:
        if _convert_job_cancelled(job_id):
            log(f"[片段] 已取消 job={job_id}")
            _convert_job_update(job_id, status="cancelled", msg="已取消", percent=0)
        else:
            _convert_job_update(job_id, status="error", msg=str(exc), percent=0)
            log(f"[片段] 异常 job={job_id} vid={vid}: {exc}")
