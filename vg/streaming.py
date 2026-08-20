# -*- coding: utf-8 -*-
"""Playlist rewrite and file streaming helpers."""
from __future__ import annotations

import mimetypes
import os
import re
from pathlib import Path
from urllib.parse import quote

from flask import Response, abort, request, send_file

from vg.segments import _normalize_playlist_rel

def rewrite_m3u8_for_proxy(
    text: str,
    playlist_rel: str,
    vid: str,
    root: str | None = None,
    operation_id: str | None = None,
) -> str:
    """把 m3u8 里的相对分片改写到本服务 /hls/<vid>/file?rel=..."""
    from urllib.parse import quote

    base_dir = str(Path(playlist_rel.replace("\\", "/")).parent).replace("\\", "/")
    if base_dir == ".":
        base_dir = ""
    lines: list[str] = []
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            lines.append(line)
            continue
        if re.match(r"https?://", raw, re.I):
            lines.append(line)
            continue
        seg_rel = _normalize_playlist_rel(base_dir, raw)
        root_q = f"&root={quote(root, safe='')}" if root else ""
        op_q = f"&op={quote(operation_id, safe='')}" if operation_id else ""
        lines.append(f"/hls/{vid}/file?rel={quote(seg_rel, safe='')}{root_q}{op_q}")
    return "\n".join(lines) + "\n"

def _stream_file(path: Path, mime: str | None = None):
    from vg.diagnostics import emit, error

    operation_id = (request.args.get("op") or "").strip()[:64]
    mime = mime or mimetypes.guess_type(str(path))[0] or "video/mp2t"
    try:
        file_size = path.stat().st_size
    except OSError as exc:
        error(
            "stream_file_stat_failed",
            exc,
            path=path,
            operation_id=operation_id,
        )
        abort(404)
    range_header = request.headers.get("Range")

    if not range_header:
        return send_file(path, mimetype=mime, conditional=True)

    m = re.match(r"bytes=(\d+)-(\d*)", range_header)
    if not m:
        emit(
            "WARN",
            "stream_range_invalid",
            force=True,
            path=path,
            range=range_header,
            file_size=file_size,
            operation_id=operation_id,
        )
        abort(400)
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else file_size - 1
    end = min(end, file_size - 1)
    if start > end or start >= file_size:
        emit(
            "WARN",
            "stream_range_unsatisfiable",
            force=True,
            path=path,
            start=start,
            end=end,
            file_size=file_size,
            operation_id=operation_id,
        )
        abort(416)
    length = end - start + 1

    def generate():
        remaining = length
        try:
            with open(path, "rb") as f:
                f.seek(start)
                chunk = 1024 * 256
                while remaining > 0:
                    data = f.read(min(chunk, remaining))
                    if not data:
                        emit(
                            "WARN",
                            "stream_file_ended_early",
                            force=True,
                            path=path,
                            remaining=remaining,
                            operation_id=operation_id,
                        )
                        break
                    remaining -= len(data)
                    yield data
        except (OSError, GeneratorExit) as exc:
            if isinstance(exc, OSError):
                error(
                    "stream_file_read_failed",
                    exc,
                    path=path,
                    sent=length - remaining,
                    remaining=remaining,
                    operation_id=operation_id,
                )
            raise

    resp = Response(generate(), status=206, mimetype=mime, direct_passthrough=True)
    resp.headers.add("Content-Range", f"bytes {start}-{end}/{file_size}")
    resp.headers.add("Accept-Ranges", "bytes")
    resp.headers.add("Content-Length", str(length))
    return resp


