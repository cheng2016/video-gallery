# -*- coding: utf-8 -*-
"""GIF and video-clip endpoints for the player toolbar."""
from __future__ import annotations

import re
from pathlib import Path

from flask import jsonify, request, send_file

from vg.catalog_repository import CatalogRepository, catalog_repository
from vg.convert import enqueue_convert_job
from vg.excerpt import (
    normalize_excerpt_request,
    open_local_file,
    output_under_library,
)
from vg.lan import is_local_client
from vg.roots import get_mounted_roots
from vg.state import STATE, _convert_lock
from vg.util import log


def _library_roots() -> list[str]:
    roots = list(get_mounted_roots() or [])
    if STATE.get("root"):
        roots.append(str(STATE["root"]))
    return roots


def _excerpt_job(job_id: str) -> dict | None:
    if not re.fullmatch(r"[a-f0-9]{8,32}", job_id or ""):
        return None
    with _convert_lock:
        job = (STATE.get("convert_jobs") or {}).get(job_id)
        if not job or (job.get("kind") or "") not in ("gif", "clip"):
            return None
        return {
            "id": job_id,
            "kind": job.get("kind") or "",
            "status": job.get("status") or "",
            "out_path": job.get("out_path") or "",
            "root": job.get("root") or "",
        }


def _finished_output(job_id: str) -> tuple[dict | None, Path | None, str]:
    job = _excerpt_job(job_id)
    if not job:
        return None, None, "未找到任务"
    if job.get("status") != "done":
        return job, None, "文件还没生成好"
    path = Path(job.get("out_path") or "")
    if not path.is_file():
        return job, None, "生成的文件不存在"
    if not output_under_library(path, _library_roots()):
        return job, None, "文件不在片库目录内"
    return job, path, ""


def register(
    app,
    *,
    repository: CatalogRepository = catalog_repository,
) -> None:
    @app.route("/api/excerpt/<vid>", methods=["POST"])
    def api_excerpt_start(vid: str):
        """Queue a GIF or a new video cut from the player range."""
        prefer_root = (request.args.get("root") or "").strip() or None
        body = request.get_json(silent=True) or {}
        mode = (body.get("mode") or "").strip().lower()
        log(
            f"[片段] API 请求 vid={vid} root={prefer_root or ''} mode={mode} "
            f"start={body.get('start')} end={body.get('end')}"
        )
        if not re.fullmatch(r"[a-f0-9]{16}", vid or ""):
            log(f"[片段] 拒绝：无效 id={vid}")
            return jsonify({"ok": False, "msg": "无效 id"}), 400
        if not STATE.get("ffmpeg"):
            log("[片段] 拒绝：未找到 ffmpeg")
            return jsonify({"ok": False, "msg": "未找到 ffmpeg，请先安装后再试"}), 400
        mounts = repository.mounted_roots()
        if not (STATE.get("root") or mounts):
            log("[片段] 拒绝：尚未选择盘符")
            return jsonify({"ok": False, "msg": "尚未选择盘符"}), 400
        if len(mounts) > 1 and not prefer_root:
            log("[片段] 拒绝：多盘未指定 root")
            return jsonify({"ok": False, "msg": "多盘操作必须指定 root"}), 400
        item = repository.find_video(vid, prefer_root=prefer_root)
        if not item:
            log(f"[片段] 拒绝：未找到视频 vid={vid}")
            return jsonify({"ok": False, "msg": "未找到视频"}), 404
        kind = item.get("kind") or ""
        if kind in ("m3u8", "ts_set") or (item.get("ext") or "").lower() == ".m3u8":
            log(f"[片段] 拒绝：流媒体 vid={vid} kind={kind}")
            return jsonify({"ok": False, "msg": "流媒体请先转成文件，再制作 GIF 或截取片段"}), 400
        spec, err = normalize_excerpt_request(
            mode,
            body.get("start"),
            body.get("end"),
            duration=item.get("duration"),
            fps=body.get("fps"),
            width=body.get("width"),
            accurate=bool(body.get("accurate")),
        )
        if not spec:
            log(f"[片段] 拒绝：{err or '起止时间无效'} vid={vid} mode={mode}")
            return jsonify({"ok": False, "msg": err or "起止时间无效"}), 400
        item_root = (
            item.get("_lib_root") or item.get("root") or prefer_root or ""
        ).strip()
        ok, msg, job_id = enqueue_convert_job(
            vid,
            kind=mode,
            name=item.get("name") or "",
            root=item_root or None,
            excerpt=spec,
        )
        log(f"[片段] 入队 ok={ok} job={job_id} vid={vid} mode={mode} msg={msg}")
        return jsonify({
            "ok": ok,
            "job_id": job_id,
            "msg": msg,
            "status": "queued",
            "kind": mode,
            "start": spec["start"],
            "end": spec["end"],
        })

    @app.route("/api/excerpt/job/<job_id>/file")
    def api_excerpt_file(job_id: str):
        """Serve a finished GIF (or clip) so the player can preview it."""
        _job, path, err = _finished_output(job_id)
        if path is None:
            code = 404 if err in ("未找到任务", "生成的文件不存在", "文件还没生成好") else 400
            return jsonify({"ok": False, "msg": err}), code
        mime = "image/gif" if path.suffix.lower() == ".gif" else "video/mp4"
        return send_file(path, mimetype=mime, conditional=True, max_age=0)

    @app.route("/api/excerpt/job/<job_id>/open", methods=["POST"])
    def api_excerpt_open(job_id: str):
        """Open the finished file with the system handler on this computer."""
        if not is_local_client((request.remote_addr or "").strip()):
            log(f"[片段] 拒绝打开：非本机 job={job_id}")
            return jsonify({"ok": False, "msg": "只能在跑服务的电脑上打开生成的文件"}), 400
        _job, path, err = _finished_output(job_id)
        if path is None:
            log(f"[片段] 打开失败 job={job_id} {err}")
            code = 404 if "不存在" in err or err == "未找到任务" or err == "文件还没生成好" else 400
            return jsonify({"ok": False, "msg": err}), code
        try:
            open_local_file(path)
        except Exception as exc:
            log(f"[片段] 打开失败 job={job_id} path={path} err={exc}")
            return jsonify({"ok": False, "msg": f"打开失败: {exc}", "path": str(path)}), 500
        log(f"[片段] 已打开 job={job_id} path={path}")
        return jsonify({"ok": True, "path": str(path), "msg": "已打开生成的文件"})
