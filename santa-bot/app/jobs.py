"""The scheduler's jobs (§10). Each takes the ``AppContext`` and reads the time from its clock.

People get only the messages §5.9 allows: join and waiting notices to the organizer
(throttled per game), one nudge before an undrawn exchange, and one reminder the
day before a drawn one. Everything else goes to the admins or the logs.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import time, timedelta
from pathlib import Path

from app import backup, repo
from app.context import AppContext
from app.core import analytics, retention, texts
from app.core.dates import in_season_window
from app.core.games import ACTIVE, WAITING
from app.core.models import GameStatus
from app.handlers import flows, notices
from app.max_api import MaxApiError, Unauthorized, ensure_subscription
from app.tls import CERTS_DIR, build_ssl_context, bundled_certificates

log = logging.getLogger(__name__)

JOIN_NOTICE_INTERVAL = timedelta(minutes=10)
WAITING_NOTICE_INTERVAL = timedelta(hours=1)
NUDGE_DAYS_BEFORE = 3
NUDGE_FROM, NUDGE_UNTIL = time(10), time(21)  # the nudge starts when a day starts; not at midnight
OUTBOX_RETENTION = timedelta(days=7)
PROCESSED_UPDATES_RETENTION = timedelta(days=3)
CERTIFICATE_WARNING = timedelta(days=30)


# --- every minute: organizer notices (§5.4, §5.6, §5.9 a–b) ---------------------------------------


async def organizer_notices(ctx: AppContext) -> None:
    await join_notices(ctx)
    await waiting_notices(ctx)
    await organizer_nudges(ctx)


async def join_notices(ctx: AppContext) -> None:
    """'В игру … вступили: …' at most once per 10 minutes per game.

    Joins hold the game lock, so under it every join so far is older than the
    timestamp recorded here and none is announced twice.
    """
    for game in await repo.games_with_newcomers(ctx.db, ACTIVE, ctx.clock.now() - JOIN_NOTICE_INTERVAL):
        async with ctx.locks.game(game.id), ctx.db.transaction() as tx:
            fresh = await repo.get_game(tx, game.id)
            if fresh is None or fresh.status != GameStatus.COLLECTING:
                continue
            newcomers = await repo.joined_since(tx, game.id, fresh.last_join_notice_at, fresh.organizer_id)
            if newcomers:
                active = await repo.count_participants(tx, game.id, ACTIVE)
                await notices.join_notice(ctx, fresh, newcomers, active, db=tx)
                await repo.update_game(tx, game.id, last_join_notice_at=ctx.clock.now())


async def waiting_notices(ctx: AppContext) -> None:
    """'В игру … хотят вступить ещё w чел.' at most once per hour per game."""
    for game in await repo.games_with_newcomers(ctx.db, WAITING, ctx.clock.now() - WAITING_NOTICE_INTERVAL):
        async with ctx.locks.game(game.id):
            fresh = await repo.get_game(ctx.db, game.id)
            if fresh is None or fresh.status != GameStatus.COLLECTING:
                continue
            upgrade = await flows.offer_upgrade(ctx, fresh)
            async with ctx.db.transaction() as tx:
                waiting = await repo.count_participants(tx, game.id, WAITING)
                await notices.waiting_notice(ctx, fresh, waiting, upgrade, db=tx)
                await repo.update_game(tx, game.id, last_waiting_notice_at=ctx.clock.now())


async def organizer_nudges(ctx: AppContext) -> None:
    """One nudge when the exchange is 3 days away or less and there was no draw (daytime only)."""
    local = ctx.clock.now().astimezone(ctx.config.tz)
    if not NUDGE_FROM <= local.time() < NUDGE_UNTIL:
        return
    today = local.date()
    due = await repo.games_to_nudge(ctx.db, today + timedelta(days=1), today + timedelta(days=NUDGE_DAYS_BEFORE))
    for game in due:
        async with ctx.locks.game(game.id), ctx.db.transaction() as tx:
            fresh = await repo.get_game(tx, game.id)
            if fresh is None or fresh.status != GameStatus.COLLECTING or fresh.exchange_date is None:
                continue
            active = await repo.count_participants(tx, game.id, ACTIVE)
            await notices.organizer_nudge(ctx, fresh, (fresh.exchange_date - today).days, active, db=tx)
            await repo.update_game(tx, game.id, org_nudge_sent=True)


# --- 12:00 MSK: the reminder the day before the exchange (§5.9 c) ---------------------------------


async def pre_exchange_reminders(ctx: AppContext) -> None:
    for game in await repo.games_to_remind(ctx.db, ctx.today() + timedelta(days=1)):
        async with ctx.locks.game(game.id), ctx.db.transaction() as tx:
            people = {p.user_id: p for p in await repo.participants(tx, game.id, ACTIVE)}
            pairs = [
                (giver, people[receiver])
                for giver, receiver in (await repo.assignments(tx, game.id)).items()
                if giver in people and receiver in people
            ]
            await notices.pre_exchange_reminders(ctx, game, pairs, db=tx)
            await repo.update_game(tx, game.id, pre_exchange_sent=True)


# --- nightly housekeeping ----------------------------------------------------------------------------


async def end_games(ctx: AppContext) -> None:
    """00:10 MSK: finish games 3 days after the exchange; end abandoned ones."""
    ended = await retention.end_games(ctx.db, ctx.clock.now(), ctx.today())
    log.info("games ended", extra=asdict(ended))


async def purge_old_data(ctx: AppContext) -> None:
    """03:30 MSK: data retention, plus old outbox rows (they quote wishes) and update keys."""
    now = ctx.clock.now()
    report = await retention.purge(ctx.db, now, ctx.today())
    outbox_rows = await ctx.outbox.prune(now - OUTBOX_RETENTION)
    update_keys = await repo.prune_processed_updates(ctx.db, now - PROCESSED_UPDATES_RETENTION)
    log.info("retention done", extra={**asdict(report), "outbox_rows": outbox_rows, "update_keys": update_keys})


async def backup_database(ctx: AppContext) -> None:
    """04:00 MSK: online backup to /data/backups/santa-YYYYMMDD.db, keeping 14 files;
    also to the S3 bucket when S3_* is set (P1)."""
    config = ctx.config
    path = await backup.make_backup(ctx.db, config.backups_dir, ctx.today())
    removed = backup.rotate(config.backups_dir)
    log.info("backup written", extra={"file": path.name, "removed": len(removed)})
    if not config.s3_enabled:
        return
    bucket = backup.Bucket(config.s3_endpoint, config.s3_bucket, config.s3_key, config.s3_secret, config.s3_region)
    try:
        await backup.upload(bucket, path, ctx.clock.now(), build_ssl_context())
    except backup.UploadFailed as error:
        log.error("backup upload failed", extra={"error": str(error)})
        await ctx.alerts.notify_admins(texts.backup_upload_failed(str(error)))
        return
    log.info("backup uploaded", extra={"file": path.name})


# --- the bot's own health -----------------------------------------------------------------------------


async def webhook_watchdog(ctx: AppContext) -> None:
    """Every 10 minutes: MAX deletes a subscription after 8 hours of failures; put it back."""
    try:
        created = await ensure_subscription(ctx.api, ctx.config.webhook_url, ctx.config.max_webhook_secret)
    except Unauthorized:
        await ctx.alerts.token_rejected()
        return
    except MaxApiError as error:
        log.warning("webhook check failed", extra={"error": str(error)})
        return
    if not created:
        return
    if ctx.runtime.webhook_registered:
        await ctx.alerts.notify_admins(texts.WEBHOOK_RESTORED)
    log.warning("webhook subscription (re)created")
    ctx.runtime.webhook_registered = True


async def certificate_check(ctx: AppContext, certs_dir: Path = CERTS_DIR) -> None:
    """Daily: warn the admins 30 days before a bundled certificate in certs/ expires."""
    now, today = ctx.clock.now(), ctx.today()
    for certificate in bundled_certificates(certs_dir):
        if certificate.not_after - now <= CERTIFICATE_WARNING:
            expires = certificate.not_after.astimezone(ctx.config.tz).date()
            await ctx.alerts.notify_admins(texts.certificate_expiring(
                name=certificate.common_name, expires=expires, days=(expires - today).days))


async def digest(ctx: AppContext) -> None:
    """21:00 MSK between DIGEST_FROM and DIGEST_TO: yesterday's and today's key numbers."""
    config, today = ctx.config, ctx.today()
    if not in_season_window(today, config.digest_from, config.digest_to):
        return
    yesterday_stats = await analytics.collect_day(ctx.db, today - timedelta(days=1), config.tz)
    today_stats = await analytics.collect_day(ctx.db, today, config.tz)
    await ctx.alerts.notify_admins(texts.digest(yesterday=yesterday_stats, today=today_stats))
