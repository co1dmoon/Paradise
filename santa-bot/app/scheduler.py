"""Periodic jobs (§10): one asyncio loop with a 30 s tick; each job keeps its own last-run guard.

Schedule (Moscow time, config TZ); the job bodies live in ``app.jobs``:

- every minute: join and waiting notices, organizer nudges;
- 12:00 pre-exchange reminders; 00:10 ending games; 03:30 data retention;
  04:00 backup; 09:00 certificate expiry check; 21:00 the admins' digest;
- every 10 minutes (webhook mode): the webhook watchdog.

The outbox worker runs on its own (``Outbox.start``, started by ``app.main``).

Last runs are stored in the ``job_runs`` table, so a restart neither repeats a
daily job nor skips it: a daily job missed while the bot was down runs as soon
as it is back, unless the job has a ``grace`` period and it is over (nobody
wants the evening digest at 3 a.m.). A job that fails is reported to the admins
(at most once per 5 minutes) and waits for its next slot.

The tick sleeps in real time; whether a job is due is decided on the injected
clock, so tests call ``Scheduler.run_due`` on a fake clock.

Seam: ``app.main`` awaits ``start(app)`` after the outbox has started and
``stop(app)`` on shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from aiohttp import web

from app import jobs, repo
from app.config import Config
from app.context import CTX_KEY, AppContext

log = logging.getLogger(__name__)

TICK_SECONDS = 30.0

JobBody = Callable[[AppContext], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Every:
    """Run when ``seconds`` have passed since the last run (at once on the first tick)."""

    seconds: float

    def is_due(self, last: datetime | None, now: datetime, tz: ZoneInfo) -> bool:
        return last is None or (now - last).total_seconds() >= self.seconds


@dataclass(frozen=True, slots=True)
class DailyAt:
    """Run once a day at local time ``at``; a missed run is made up within ``grace`` (None: any time)."""

    at: time
    grace: timedelta | None = None

    def is_due(self, last: datetime | None, now: datetime, tz: ZoneInfo) -> bool:
        local = now.astimezone(tz)
        slot = datetime.combine(local.date(), self.at, tzinfo=tz)
        if local < slot:
            if last is None:
                return False  # first start before today's slot: wait for it
            slot -= timedelta(days=1)
        if last is not None and last >= slot:
            return False
        return self.grace is None or local - slot <= self.grace


Schedule = Every | DailyAt


@dataclass(frozen=True, slots=True)
class Job:
    name: str
    schedule: Schedule
    run: JobBody


def jobs_for(config: Config) -> list[Job]:
    """The jobs this configuration needs: without a bot token only housekeeping runs."""
    schedule = [
        Job("end_games", DailyAt(time(0, 10)), jobs.end_games),
        Job("retention", DailyAt(time(3, 30)), jobs.purge_old_data),
        Job("backup", DailyAt(time(4, 0)), jobs.backup_database),
    ]
    if not config.bot_enabled:
        return schedule
    schedule += [
        Job("organizer_notices", Every(60), jobs.organizer_notices),
        Job("pre_exchange", DailyAt(time(12, 0), grace=timedelta(hours=9)), jobs.pre_exchange_reminders),
        Job("certificates", DailyAt(time(9, 0)), jobs.certificate_check),
        Job("digest", DailyAt(time(21, 0), grace=timedelta(hours=2)), jobs.digest),
    ]
    if config.mode == "webhook":
        schedule.append(Job("webhook_watchdog", Every(600), jobs.webhook_watchdog))
    return schedule


class Scheduler:
    def __init__(self, ctx: AppContext, schedule: Sequence[Job]) -> None:
        self._ctx = ctx
        self._jobs = tuple(schedule)

    async def run_due(self) -> list[str]:
        """Run every job that is due now, one after another; returns their names."""
        ctx = self._ctx
        last_runs = await repo.job_runs(ctx.db)
        ran = []
        for job in self._jobs:
            now = ctx.clock.now()
            if job.schedule.is_due(last_runs.get(job.name), now, ctx.config.tz):
                await self._run(job)
                await repo.record_job_run(ctx.db, job.name, now)
                ran.append(job.name)
        return ran

    async def _run(self, job: Job) -> None:
        try:
            await job.run(self._ctx)
        except Exception as error:
            log.exception("job failed", extra={"job": job.name})
            await self._ctx.alerts.error(f"{type(error).__name__} в задаче {job.name}")

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_due()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(TICK_SECONDS)


_TASK_KEY = web.AppKey("scheduler_task", asyncio.Task)


async def start(app: web.Application) -> None:
    ctx = app[CTX_KEY]
    scheduler = Scheduler(ctx, jobs_for(ctx.config))
    app[_TASK_KEY] = asyncio.create_task(scheduler.run_forever(), name="scheduler")


async def stop(app: web.Application) -> None:
    task = app.get(_TASK_KEY)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
