# -*- coding: utf-8 -*-
"""Registration entry point for feature route modules."""
from __future__ import annotations


def register_feature_routes(app) -> None:
    from vg.routes.cleanup import register as register_cleanup
    from vg.routes.convert import register as register_convert
    from vg.routes.export_static import register as register_export_static
    from vg.routes.privacy import register as register_privacy
    from vg.routes.roots import register as register_roots
    from vg.routes.share import register as register_share
    from vg.routes.status import register as register_status

    register_status(app)
    register_share(app)
    register_cleanup(app)
    register_convert(app)
    register_privacy(app)
    register_export_static(app)
    register_roots(app)
