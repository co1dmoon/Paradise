"""Application factory, startup and shutdown (§11). Run with ``python -m app.main``.

Startup: config is validated before the app is built (fail fast), then
1. open the database and apply migrations, seed settings from env (DB wins later);
2. GET /me: log the bot name and warn the admins if it differs from MAX_BOT_USERNAME;
   register the '/' command menu (PATCH /me/commands);
3. webhook mode: post our subscription (url, update types, secret); polling mode: start the poller;
4. start the outbox worker and the scheduler.
Without MAX_BOT_TOKEN steps 2–3 and the outbox are skipped: only the website runs.

Seams for later stages: ``handlers.router.dispatch(ctx, update)`` (via
``app.updates.process_update``), ``web.routes.register(app)`` and
``scheduler.start(app)`` / ``scheduler.stop(app)``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import sys
from collections.abc import AsyncIterator
from aiohttp import web

from app import jobs, repo, scheduler
from app.alerts import Alerter
from app.config import Config, ConfigError, load_config
from app.context import CTX_KEY, AppContext
from app.core import texts
from app.core.clock import Clock, SystemClock
from app.db import Database
from app.delivery import DeliveryTracker
from app.log import setup_logging
from app.max_api import HttpMaxApi, MaxApi, MaxApiError, Unauthorized
from app.outbox import Outbox
from app.tls import build_ssl_context
from app.updates import process_update
from app.web import routes

log = logging.getLogger("app.main")

POLL_TIMEOUT = 30
POLL_ERROR_PAUSE = 5.0


async def build_context(
    config: Config,
    *,
    api: MaxApi | None = None,
    clock: Clock | None = None,
    rng: random.Random | None = None,
) -> AppContext:
    """Open the database (migrated and seeded) and wire the outbox, alerts and delivery hooks."""
    clock = clock or SystemClock()
    db = await Database.open(config.db_path)
    await db.migrate()
    await repo.seed_settings(db, config.default_settings)
    if api is None:
        api = HttpMaxApi(config.max_bot_token, config.max_api_base, build_ssl_context())
    outbox = Outbox(db, api, clock)
    alerts = Alerter(outbox, config.admin_user_ids, clock)
    outbox.hooks = DeliveryTracker(db, outbox, alerts, clock)
    return AppContext(config, db, api, outbox, alerts, clock, rng or random.SystemRandom())


def create_app(
    config: Config,
    *,
    api: MaxApi | None = None,
    clock: Clock | None = None,
    rng: random.Random | None = None,
) -> web.Application:
    app = web.Application(client_max_size=1024 * 1024)

    async def lifecycle(app: web.Application) -> AsyncIterator[None]:
        ctx = await build_context(config, api=api, clock=clock, rng=rng)
        app[CTX_KEY] = ctx
        poller = await _start_bot(ctx)
        await scheduler.start(app)
        yield
        await scheduler.stop(app)
        if poller is not None:
            poller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poller
        await ctx.wait_background()
        await ctx.outbox.stop()
        await ctx.api.close()
        await ctx.db.close()
        log.info("stopped")

    app.cleanup_ctx.append(lifecycle)
    routes.register(app)
    return app


async def _start_bot(ctx: AppContext) -> asyncio.Task[None] | None:
    config = ctx.config
    if not config.bot_enabled:
        log.warning("bot disabled: MAX_BOT_TOKEN is empty; serving the website only")
        return None
    await _check_identity(ctx)
    await _register_commands(ctx)
    poller = None
    if config.mode == "webhook":
        await jobs.sync_webhook(ctx, startup=True)
    else:
        await _warn_about_webhook(ctx)
        poller = asyncio.create_task(_poll(ctx), name="poller")
    ctx.outbox.start()
    return poller


async def _check_identity(ctx: AppContext) -> None:
    try:
        me = await ctx.api.get_me()
    except Unauthorized:
        await ctx.alerts.token_rejected()
        return
    except MaxApiError as error:
        log.warning("GET /me failed", extra={"error": str(error)})
        return
    ctx.runtime.bot_username = me.username
    await ctx.outbox.retry_unauthorized_now()
    log.info("bot identity", extra={"bot_username": me.username, "bot_id": me.user_id})
    configured = ctx.config.max_bot_username
    if me.username and me.username.lower() != configured.lower():
        await ctx.alerts.notify_admins(texts.username_mismatch(actual=me.username, configured=configured))


async def _register_commands(ctx: AppContext) -> None:
    """The '/' menu (§9). Optional: everything is reachable through buttons anyway."""
    try:
        await ctx.api.set_commands(texts.COMMAND_MENU)
    except MaxApiError as error:
        log.warning("command menu not registered", extra={"error": str(error)})


async def _warn_about_webhook(ctx: AppContext) -> None:
    """MAX does not deliver updates by polling while a webhook subscription exists (dev.max.ru,
    POST /subscriptions, checked 2026-09-25). The subscription is not removed here: it may be
    the production bot's, and a local test must never switch production off."""
    try:
        subscriptions = await ctx.api.list_subscriptions()
    except MaxApiError as error:
        log.warning("could not list webhook subscriptions", extra={"error": str(error)})
        return
    if subscriptions:
        log.error("MODE=polling, but the bot has a webhook subscription: MAX delivers nothing by polling "
                  "while it exists. Use a separate test bot, or MODE=webhook")


async def _poll(ctx: AppContext) -> None:
    """MODE=polling, for local manual tests only (the docs say not for production).

    Updates of one page are processed in order, like a webhook batch.
    """
    marker: int | None = None
    while True:
        try:
            page = await ctx.api.get_updates(marker, POLL_TIMEOUT)
        except Unauthorized:
            await ctx.alerts.token_rejected()
            await asyncio.sleep(POLL_ERROR_PAUSE)
            continue
        except MaxApiError as error:
            log.warning("polling failed", extra={"error": str(error)})
            await asyncio.sleep(POLL_ERROR_PAUSE)
            continue
        if page.marker is not None:
            marker = page.marker
        for raw in page.updates:
            await process_update(ctx, raw)


def main() -> None:
    setup_logging()
    try:
        config = load_config()
    except ConfigError as error:
        print("Ошибка в настройках (.env):\n- " + "\n- ".join(error.problems), file=sys.stderr)
        sys.exit(2)
    for warning in config.warnings:
        log.warning(warning)
    web.run_app(create_app(config), host="0.0.0.0", port=config.port, access_log=None, print=None)


if __name__ == "__main__":
    main()
