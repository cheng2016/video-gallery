# -*- coding: utf-8 -*-
"""Cleanup HTTP endpoint."""
from __future__ import annotations

from flask import jsonify, request

from vg.cleanup import build_cleanup_response


def register(app, *, response_builder=build_cleanup_response) -> None:
    @app.route("/api/cleanup")
    def api_cleanup():
        """Duplicate/bad-file cleanup scoped by disk, channel and folder."""
        return jsonify(response_builder(
            request.args.get("type") or "dup",
            lib=request.args.get("lib") or "",
            category=request.args.get("category") or "",
            folder=request.args.get("folder") or "",
        ))
