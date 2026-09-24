"""HTTP routes (§8).

Seam: ``app.main.create_app`` calls ``register(app)`` once while building the
application. Handlers reach the ``AppContext`` at request time through
``request.app[app.context.CTX_KEY]``.
"""

from __future__ import annotations

from aiohttp import web


def register(app: web.Application) -> None:
    """Add the site's routes to ``app``. Built by the web stage."""
