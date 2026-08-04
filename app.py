#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local video gallery entry. Logic lives in vg/."""
from __future__ import annotations
from vg.web import app  # noqa: F401 — re-export
from vg.main import main

if __name__ == "__main__":
    main()
