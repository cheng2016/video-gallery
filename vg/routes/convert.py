# -*- coding: utf-8 -*-
"""Convert/fix-audio queue HTTP endpoints."""
from __future__ import annotations

import re

from flask import jsonify, request

from vg.config import RM_EXTS, RM_UNAVAILABLE_MSG
from vg.convert import (
    _kill_convert_proc,
    convert_parallel_limit,
    enqueue_convert_job,
    list_convert_jobs,
    normalize_scale,
    normalize_video_encoder,
    pump_convert_queue,
    resolve_transcode_out_ext,
    schedule_ffmpeg_rm_probe,
)
from vg.catalog_repository import CatalogRepository, catalog_repository
from vg.drives import save_prefs
from vg.state import STATE, _convert_lock


def register(
    app,
    *,
    repository: CatalogRepository = catalog_repository,
) -> None:
    @app.route("/api/convert-mp4/<vid>", methods=["POST"])
    def api_convert_mp4_start(vid: str):
        """将 m3u8 / ts_set 转为同目录 MP4（进入限速队列）。"""
        if not re.fullmatch(r"[a-f0-9]{16}", vid or ""):
            return jsonify({"ok": False, "msg": "无效 id"}), 400
        if not STATE.get("ffmpeg"):
            return jsonify({"ok": False, "msg": "未找到 ffmpeg，请先安装后再试"}), 400
        mounts = repository.mounted_roots()
        if not (STATE.get("root") or mounts):
            return jsonify({"ok": False, "msg": "尚未选择盘符"}), 400
        prefer_root = (request.args.get("root") or "").strip() or None
        if len(mounts) > 1 and not prefer_root:
            return jsonify({"ok": False, "msg": "多盘转换必须指定 root"}), 400
        item = repository.find_video(vid, prefer_root=prefer_root)
        if not item:
            return jsonify({"ok": False, "msg": "未找到视频"}), 404
        kind = item.get("kind") or ""
        if (
            kind not in ("m3u8", "ts_set")
            and (item.get("ext") or "").lower() != ".m3u8"
        ):
            return jsonify({"ok": False, "msg": "仅支持 m3u8 / TS 合集"}), 400
        item_root = (
            item.get("_lib_root")
            or item.get("root")
            or prefer_root
            or ""
        ).strip()
        ok, msg, job_id = enqueue_convert_job(
            vid,
            kind="mp4",
            name=item.get("name") or "",
            root=item_root or None,
        )
        return jsonify({
            "ok": ok,
            "job_id": job_id,
            "msg": msg,
            "status": "queued",
        })

    @app.route("/api/convert-mp4/job/<job_id>")
    def api_convert_mp4_status(job_id: str):
        with _convert_lock:
            job = STATE["convert_jobs"].get(job_id)
            if not job:
                return jsonify({"ok": False, "msg": "任务不存在"}), 404
            return jsonify({
                "ok": True,
                "job_id": job_id,
                "vid": job.get("vid") or "",
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
            })

    @app.route("/api/convert-mp4/job/<job_id>/cancel", methods=["POST"])
    def api_convert_mp4_cancel(job_id: str):
        with _convert_lock:
            job = STATE["convert_jobs"].get(job_id)
            if not job:
                return jsonify({"ok": False, "msg": "任务不存在"}), 404
            if job.get("status") in ("done", "error", "cancelled"):
                return jsonify({
                    "ok": True,
                    "msg": "任务已结束",
                    "status": job.get("status"),
                })
            job["cancel"] = True
            job["msg"] = "正在取消…"
            if job.get("status") == "queued":
                job["status"] = "cancelled"
                job["msg"] = "已取消"
                process = None
            else:
                process = job.get("proc")
        if process:
            _kill_convert_proc(process)
        pump_convert_queue()
        return jsonify({
            "ok": True,
            "msg": "已请求取消",
            "status": "cancelling",
        })

    @app.route("/api/fix-audio/<vid>", methods=["POST"])
    def api_fix_audio_start(vid: str):
        """将不兼容浏览器的音轨转为 AAC（进入限速队列）。"""
        if not re.fullmatch(r"[a-f0-9]{16}", vid or ""):
            return jsonify({"ok": False, "msg": "无效 id"}), 400
        if not STATE.get("ffmpeg"):
            return jsonify({"ok": False, "msg": "未找到 ffmpeg，请先安装后再试"}), 400
        mounts = repository.mounted_roots()
        if not (STATE.get("root") or mounts):
            return jsonify({"ok": False, "msg": "尚未选择盘符"}), 400
        prefer_root = (request.args.get("root") or "").strip() or None
        if len(mounts) > 1 and not prefer_root:
            return jsonify({"ok": False, "msg": "多盘修复必须指定 root"}), 400
        item = repository.find_video(vid, prefer_root=prefer_root)
        if not item:
            return jsonify({"ok": False, "msg": "未找到视频"}), 404
        kind = item.get("kind") or ""
        if kind in ("m3u8", "ts_set") or (
            item.get("ext") or ""
        ).lower() == ".m3u8":
            return jsonify({"ok": False, "msg": "流媒体请用「转成 MP4」"}), 400
        item_root = (
            item.get("_lib_root")
            or item.get("root")
            or prefer_root
            or ""
        ).strip()
        ok, msg, job_id = enqueue_convert_job(
            vid,
            kind="fix_audio",
            name=item.get("name") or "",
            root=item_root or None,
        )
        return jsonify({
            "ok": ok,
            "job_id": job_id,
            "msg": msg,
            "status": "queued",
        })

    @app.route("/api/fps30/<vid>", methods=["POST"])
    def api_fps30_start(vid: str):
        """2K/4K 60/90/120fps → 自定义低帧率（独立于转格式 / 修声音）。"""
        from vg.media import normalize_target_fps
        from vg.util import log as _log

        prefer_root = (request.args.get("root") or "").strip() or None
        body = request.get_json(silent=True) or {}
        target_raw = body.get("target_fps")
        if target_raw is None:
            target_raw = request.args.get("target_fps")
        _log(f"[帧率转码] API 请求 vid={vid} root={prefer_root or ''} target_fps={target_raw}")
        if not re.fullmatch(r"[a-f0-9]{16}", vid or ""):
            _log(f"[帧率转码] 拒绝：无效 id={vid}")
            return jsonify({"ok": False, "msg": "无效 id"}), 400
        if not STATE.get("ffmpeg"):
            _log("[帧率转码] 拒绝：未找到 ffmpeg")
            return jsonify({"ok": False, "msg": "未找到 ffmpeg，请先安装后再试"}), 400
        mounts = repository.mounted_roots()
        if not (STATE.get("root") or mounts):
            _log("[帧率转码] 拒绝：尚未选择盘符")
            return jsonify({"ok": False, "msg": "尚未选择盘符"}), 400
        if len(mounts) > 1 and not prefer_root:
            _log("[帧率转码] 拒绝：多盘未指定 root")
            return jsonify({"ok": False, "msg": "多盘帧率转码必须指定 root"}), 400
        item = repository.find_video(vid, prefer_root=prefer_root)
        if not item:
            _log(f"[帧率转码] 拒绝：未找到视频 vid={vid}")
            return jsonify({"ok": False, "msg": "未找到视频"}), 404
        kind = item.get("kind") or ""
        if kind in ("m3u8", "ts_set") or (
            item.get("ext") or ""
        ).lower() == ".m3u8":
            _log(f"[帧率转码] 拒绝：流媒体 vid={vid} kind={kind}")
            return jsonify({"ok": False, "msg": "流媒体请先转成文件后再做帧率转码"}), 400
        src_fps = item.get("fps")
        target_fps = normalize_target_fps(src_fps, target_raw, default=30) if src_fps else None
        if src_fps is not None and not target_fps:
            _log(f"[帧率转码] 拒绝：目标帧率无效 src={src_fps} want={target_raw}")
            return jsonify({"ok": False, "msg": "目标帧率必须低于源帧率"}), 400
        if target_fps is None:
            try:
                target_fps = int(round(float(target_raw))) if target_raw is not None else 30
            except (TypeError, ValueError):
                target_fps = 30
        item_root = (
            item.get("_lib_root")
            or item.get("root")
            or prefer_root
            or ""
        ).strip()
        ok, msg, job_id = enqueue_convert_job(
            vid,
            kind="fps30",
            name=item.get("name") or "",
            root=item_root or None,
            target_fps=target_fps,
        )
        _log(
            f"[帧率转码] 入队结果 ok={ok} job_id={job_id} vid={vid} "
            f"name={item.get('name') or ''} target={target_fps} "
            f"{item.get('width')}x{item.get('height')}@{item.get('fps')} msg={msg}"
        )
        return jsonify({
            "ok": ok,
            "job_id": job_id,
            "msg": msg,
            "status": "queued",
            "target_fps": target_fps,
        })

    @app.route("/api/transcode/<vid>", methods=["POST"])
    def api_transcode_start(vid: str):
        """Unified remux / fps / scale / encoder job from the player convert panel."""
        from vg.media import normalize_target_fps
        from vg.util import log as _log

        prefer_root = (request.args.get("root") or "").strip() or None
        body = request.get_json(silent=True) or {}
        _log(
            f"[转换] API 请求 vid={vid} root={prefer_root or ''} "
            f"body_keys={sorted(body.keys())}"
        )
        if not re.fullmatch(r"[a-f0-9]{16}", vid or ""):
            return jsonify({"ok": False, "msg": "无效 id"}), 400
        if not STATE.get("ffmpeg"):
            return jsonify({"ok": False, "msg": "未找到 ffmpeg，请先安装后再试"}), 400
        mounts = repository.mounted_roots()
        if not (STATE.get("root") or mounts):
            return jsonify({"ok": False, "msg": "尚未选择盘符"}), 400
        if len(mounts) > 1 and not prefer_root:
            return jsonify({"ok": False, "msg": "多盘转换必须指定 root"}), 400
        item = repository.find_video(vid, prefer_root=prefer_root)
        if not item:
            return jsonify({"ok": False, "msg": "未找到视频"}), 404
        item_root = (
            item.get("_lib_root") or item.get("root") or prefer_root or ""
        ).strip()
        if body.get("fix_audio"):
            ok, msg, job_id = enqueue_convert_job(
                vid,
                kind="fix_audio",
                name=item.get("name") or "",
                root=item_root or None,
            )
            return jsonify({"ok": ok, "job_id": job_id, "msg": msg, "status": "queued", "kind": "fix_audio"})

        ext = (item.get("ext") or "").lower()
        if ext in RM_EXTS:
            if STATE.get("ffmpeg_rm") is None:
                schedule_ffmpeg_rm_probe()
                return jsonify({"ok": False, "msg": "正在检测 ffmpeg 是否支持 RMVB，请稍后再试"}), 400
            if not STATE.get("ffmpeg_rm"):
                return jsonify({"ok": False, "msg": RM_UNAVAILABLE_MSG}), 400

        encoder = normalize_video_encoder(body.get("video_encoder"))
        out_ext, remap_note = resolve_transcode_out_ext(body.get("out_ext"), item, encoder)
        src_fps = item.get("fps")
        target_raw = body.get("target_fps")
        target_fps = None
        if target_raw not in (None, "", 0, "0"):
            if src_fps is not None:
                target_fps = normalize_target_fps(src_fps, target_raw)
                if not target_fps:
                    return jsonify({"ok": False, "msg": "目标帧率必须低于源帧率"}), 400
            else:
                try:
                    target_fps = int(round(float(target_raw)))
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "msg": "目标帧率无效"}), 400
        scale_raw = body.get("scale")
        scale = normalize_scale(scale_raw, item.get("height"))
        if scale_raw in (720, 1080, "720", "1080") and not scale:
            return jsonify({"ok": False, "msg": "源画面已小于所选分辨率"}), 400

        ok, msg, job_id = enqueue_convert_job(
            vid,
            kind="transcode",
            name=item.get("name") or "",
            root=item_root or None,
            target_fps=target_fps,
            out_ext=out_ext,
            scale=scale,
            video_encoder=encoder,
        )
        if remap_note and ok:
            msg = f"{msg}（{remap_note}）"
        _log(
            f"[转换] 入队 ok={ok} job={job_id} vid={vid} ext={out_ext} "
            f"fps={target_fps} scale={scale} encoder={encoder} msg={msg}"
        )
        return jsonify({
            "ok": ok,
            "job_id": job_id,
            "msg": msg,
            "status": "queued",
            "kind": "transcode",
            "out_ext": out_ext,
            "target_fps": target_fps,
            "scale": scale,
            "video_encoder": encoder,
        })

    @app.route("/api/convert/queue")
    def api_convert_queue():
        jobs = list_convert_jobs(50)
        active = sum(
            1 for job in jobs if job.get("status") in ("queued", "running")
        )
        return jsonify({
            "ok": True,
            "jobs": jobs,
            "active": active,
            "parallel": convert_parallel_limit(),
        })

    @app.route("/api/convert/batch", methods=["POST"])
    def api_convert_batch():
        """批量加入转换队列，支持 items: [{id, root}] 精确定位磁盘。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("ids") or []
        requested = data.get("items")
        legacy_ids = not isinstance(requested, list)
        if not isinstance(requested, list):
            requested = [{"id": vid} for vid in ids] if isinstance(ids, list) else []
        kind = (data.get("kind") or "mp4").strip().lower()
        if kind not in ("mp4", "fix_audio"):
            return jsonify({"ok": False, "msg": "kind 应为 mp4 或 fix_audio"}), 400
        if data.get("parallel") is not None:
            try:
                STATE["convert_parallel"] = max(
                    1,
                    min(4, int(data["parallel"])),
                )
                save_prefs(convert_parallel=STATE["convert_parallel"])
                pump_convert_queue()
            except (TypeError, ValueError):
                pass
        if not isinstance(requested, list):
            return jsonify({"ok": False, "msg": "items 无效"}), 400
        mounts = repository.mounted_roots()
        if legacy_ids and ids and len(mounts) > 1:
            return jsonify({
                "ok": False,
                "msg": "多盘批量转换必须携带每项 root",
            }), 400
        if not requested:
            return jsonify({
                "ok": True,
                "queued": [],
                "skipped": [],
                "msg": f"并发已设为 {convert_parallel_limit()}",
                "parallel": convert_parallel_limit(),
            })
        if not STATE.get("ffmpeg"):
            return jsonify({"ok": False, "msg": "未找到 ffmpeg"}), 400

        queued = []
        skipped = []
        for raw in requested[:80]:
            entry = raw if isinstance(raw, dict) else {"id": raw}
            vid = str(entry.get("id") or "")
            prefer_root = str(entry.get("root") or "").strip() or None
            if len(mounts) > 1 and not prefer_root:
                skipped.append({"id": vid, "msg": "缺少 root"})
                continue
            if not re.fullmatch(r"[a-f0-9]{16}", vid):
                skipped.append({"id": vid, "msg": "无效 id"})
                continue
            item = repository.find_video(vid, prefer_root=prefer_root)
            if not item:
                skipped.append({"id": vid, "msg": "未找到"})
                continue
            item_kind = item.get("kind") or ""
            ext = (item.get("ext") or "").lower()
            if kind == "mp4":
                if item_kind not in ("m3u8", "ts_set") and ext != ".m3u8":
                    skipped.append({"id": vid, "msg": "不是 m3u8/TS 合集"})
                    continue
            else:
                if item_kind in ("m3u8", "ts_set") or ext == ".m3u8":
                    skipped.append({"id": vid, "msg": "流媒体请用转 MP4"})
                    continue
                if not item.get("audio_hard"):
                    skipped.append({"id": vid, "msg": "无需修声音"})
                    continue
            item_root = (
                item.get("_lib_root")
                or item.get("root")
                or prefer_root
                or ""
            ).strip()
            ok, msg, job_id = enqueue_convert_job(
                vid,
                kind=kind,
                name=item.get("name") or "",
                root=item_root or None,
            )
            if ok:
                queued.append({
                    "id": vid,
                    "job_id": job_id,
                    "name": item.get("name") or "",
                })
            else:
                skipped.append({"id": vid, "msg": msg})
        return jsonify({
            "ok": True,
            "queued": queued,
            "skipped": skipped,
            "msg": (
                f"已排队 {len(queued)} 个"
                + (f"，跳过 {len(skipped)}" if skipped else "")
            ),
            "parallel": convert_parallel_limit(),
        })
