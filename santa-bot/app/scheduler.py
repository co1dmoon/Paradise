"""Periodic jobs (§10). Built by the scheduler stage.

Seam: ``app.main`` awaits ``start(app)`` after the outbox has started and
``stop(app)`` on shutdown; the context is ``app[app.context.CTX_KEY]``.
"""

from __future__ import annotations

from aiohttp import web


async def start(app: web.Application) -> None:
    """Start the scheduler loop (no jobs yet)."""


async def stop(app: web.Application) -> None:
    """Stop the scheduler loop."""
