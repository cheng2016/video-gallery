# -*- coding: utf-8 -*-
"""Mounted-root administration endpoint."""
from __future__ import annotations

import time
from pathlib import Path

from flask import jsonify, request

from vg.roots import (
    add_mount,
    publish_unified_library,
    remove_mount,
    roots_summary,
    set_mounted_roots,
)
from vg.state import STATE


def _unified_snapshot() -> list[dict]:
    """Return the currently published unified video list, or []."""
    vids = STATE.get("videos") or []
    try:
        return list(vids)
    except Exception:
        return []


def register(app) -> None:
    @app.route("/api/roots", methods=["GET", "POST"])
    def api_roots():
        """GET roots; POST add/remove/set/publish."""
        if request.method == "GET":
            started = time.perf_counter()
            snap = _unified_snapshot()
            mounts = roots_summary(snap)
            primary = ""
            try:
                if STATE.get("root"):
                    primary = str(Path(STATE["root"]).resolve())
            except OSError:
                primary = str(STATE.get("root") or "")
            per_root_counts: list[str] = []
            per_root_cat_names: list[str] = []
            for mount in mounts:
                mount["current"] = (
                    bool(primary)
                    and mount.get("path", "").lower() == primary.lower()
                )
                cats = mount.get("categories") or []
                per_root_counts.append(
                    f"{mount.get('path','')}:count={mount.get('count',0)}:cats={len(cats)}"
                )
                # 每个盘保留前 5 个分类名，方便"G:\\:0 但应该有 5 分类"的诊断。
                names = [str(c.get("name") or "?") for c in cats[:5]]
                per_root_cat_names.append(
                    f"{mount.get('path','')}:[{','.join(names)}]"
                )
            try:
                from vg.diagnostics import perf, info as _info

                perf(
                    "api_roots",
                    (time.perf_counter() - started) * 1000.0,
                    force=True,
                    roots=len(mounts),
                    videos_len=len(snap),
                    multi=len(mounts) > 1,
                    primary_defined=bool(primary),
                )
                # 前台「mounts_rendered 仍显示 G:\\:0」但后端是否已经返回了
                # counts，这条 INFO 直接给出返回体的每盘摘要。
                _info(
                    "api_roots_response_summary",
                    force=True,
                    roots=len(mounts),
                    snap_len=len(snap),
                    scanning=bool(STATE.get("scanning")),
                    updating=bool(STATE.get("updating")),
                    meta_running=bool(STATE.get("meta_progress")),
                    thumb_pending=int(STATE.get("thumb_pending") or 0),
                    lib_gen=int(STATE.get("lib_gen") or 0),
                    per_root_counts=" | ".join(per_root_counts),
                    per_root_cat5=" | ".join(per_root_cat_names),
                    primary=primary or "",
                )
            except Exception as _ex:
                try:
                    from vg.diagnostics import error as _diag_err
                    _diag_err("api_roots_response_diag_failed", _ex)
                except Exception:
                    pass
            return jsonify({
                "ok": True,
                "roots": mounts,
                "count": len(mounts),
                "multi": len(mounts) > 1,
                "primary": primary,
            })

        data = request.get_json(silent=True) or {}
        action = (data.get("action") or "add").strip().lower()
        if action == "publish":
            count = publish_unified_library()
            return jsonify({
                "ok": True,
                "msg": f"已刷新统一片库（{count}）",
                "count": count,
            })
        if action == "set":
            paths = data.get("paths") or []
            if not isinstance(paths, list):
                return jsonify({"ok": False, "msg": "paths 无效"}), 400
            cleaned = set_mounted_roots([str(path) for path in paths])
            count = publish_unified_library() if cleaned else 0
            snap = _unified_snapshot()
            return jsonify({
                "ok": True,
                "msg": f"已设置 {len(cleaned)} 个目录",
                "roots": cleaned,
                "count": count,
            })

        path = (data.get("path") or data.get("drive") or "").strip().strip('"')
        if not path:
            return jsonify({"ok": False, "msg": "请提供 path"}), 400
        if len(path) == 2 and path[1] == ":":
            path += "\\"
        snap = _unified_snapshot()
        if action == "remove":
            ok, msg = remove_mount(path)
            return jsonify({"ok": ok, "msg": msg, "roots": roots_summary(snap)})
        ok, msg = add_mount(path)
        return jsonify({"ok": ok, "msg": msg, "roots": roots_summary(snap)})
