"""§10 scheduler: schedules on a fake clock, throttled notices, reminders and the nightly jobs."""

from __future__ import annotations

import contextlib
import dataclasses
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import backup, jobs, repo
from app.config import Config
from app.context import AppContext
from app.core import texts
from app.core.clock import FakeClock
from app.core.dates import in_season_window
from app.core.models import Game, GameStatus
from app.handlers import views
from app.max_api import Unauthorized
from app.scheduler import DailyAt, Every, Job, Scheduler, jobs_for
from tests.bot import ADMIN_ID, Bot
from tests.site import full_game_with_waiting
from tools.fake_max import FakeMaxApi, FakeUser

MSK = ZoneInfo("Europe/Moscow")
ORGANIZER = 100


def msk(day: int, hour: int, minute: int = 0, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=MSK)


def travel(clock: FakeClock, moment: datetime) -> None:
    clock.advance((moment - clock.now()).total_seconds())


def notices_to(api: FakeMaxApi, user_id: int, prefix: str) -> list[str]:
    return [text for text in api.texts_to(user_id) if text.startswith(prefix)]


async def organizer_game(bot: Bot, title: str = "Семья Ивановых") -> tuple[FakeUser, Game]:
    olga = bot.person(ORGANIZER, "Ольга")
    await bot.onboard(olga)
    return olga, await bot.create_game(olga, title)


# --- schedules ---------------------------------------------------------------------------------------


def test_every_runs_at_once_then_after_its_interval() -> None:
    every = Every(60)
    assert every.is_due(None, msk(20, 12), MSK)
    assert not every.is_due(msk(20, 12), msk(20, 12) + timedelta(seconds=59), MSK)
    assert every.is_due(msk(20, 12), msk(20, 12, 1), MSK)


def test_daily_job_waits_for_its_slot_and_runs_once_a_day() -> None:
    digest = DailyAt(time(21))
    assert not digest.is_due(None, msk(20, 20, 59), MSK), "a first start before 21:00 waits for 21:00"
    assert digest.is_due(None, msk(20, 21), MSK)
    assert not digest.is_due(msk(20, 21), msk(20, 23, 59), MSK)
    assert not digest.is_due(msk(20, 21), msk(21, 20, 59), MSK)
    assert digest.is_due(msk(20, 21), msk(21, 21), MSK)


def test_missed_daily_run_is_made_up_only_within_grace() -> None:
    digest = DailyAt(time(21), grace=timedelta(hours=2))
    assert digest.is_due(msk(19, 21), msk(20, 22, 30), MSK), "back at 22:30 after being down at 21:00"
    assert not digest.is_due(msk(19, 21), msk(21, 3), MSK), "no digest at 3 a.m."
    backup_job = DailyAt(time(4))
    assert backup_job.is_due(msk(18, 4), msk(20, 15), MSK), "housekeeping is always made up"


def test_jobs_depend_on_the_configuration(config: Config) -> None:
    names = {job.name for job in jobs_for(config)}
    assert names == {"end_games", "retention", "backup", "organizer_notices", "pre_exchange", "certificates",
                     "digest", "webhook_watchdog"}
    polling = {job.name for job in jobs_for(dataclasses.replace(config, mode="polling"))}
    assert polling == names - {"webhook_watchdog"}
    site_only = {job.name for job in jobs_for(dataclasses.replace(config, max_bot_token=""))}
    assert site_only == {"end_games", "retention", "backup"}


async def test_run_due_keeps_a_guard_per_job_and_survives_failures(
    ctx: AppContext, api: FakeMaxApi, clock: FakeClock
) -> None:
    calls: list[str] = []

    async def fine(_: AppContext) -> None:
        calls.append("fine")

    async def broken(_: AppContext) -> None:
        raise RuntimeError("secret detail")

    scheduler = Scheduler(ctx, [Job("broken", Every(60), broken), Job("fine", Every(60), fine),
                                Job("daily", DailyAt(time(12)), fine)])
    assert await scheduler.run_due() == ["broken", "fine", "daily"]
    assert await scheduler.run_due() == []
    clock.advance(60)
    assert await scheduler.run_due() == ["broken", "fine"]
    assert calls == ["fine", "fine", "fine"]
    assert set(await repo.job_runs(ctx.db)) == {"broken", "fine", "daily"}

    restarted = Scheduler(ctx, [Job("daily", DailyAt(time(12)), fine)])
    assert await restarted.run_due() == [], "the guard is stored, a restart does not repeat the job"

    await ctx.outbox.drain()
    alerts = api.texts_to(ADMIN_ID)
    assert len(alerts) == 1 and "RuntimeError в задаче broken" in alerts[0]
    assert "secret detail" not in alerts[0]
    assert "пропущено: 1" not in alerts[0]


# --- §5.4 and §5.6 notices to the organizer -------------------------------------------------------------


async def test_join_notices_are_throttled_to_one_per_ten_minutes(bot: Bot, api: FakeMaxApi, clock: FakeClock) -> None:
    _, game = await organizer_game(bot)
    await bot.join(bot.person(201, "Иван"), game)
    await jobs.organizer_notices(bot.ctx)
    await bot.drain()
    notices = notices_to(api, ORGANIZER, "В игру")
    assert notices == ["В игру «Семья Ивановых» вступили: Иван. Всего: 2 из 10."]
    assert texts.BTN_PANEL in [b.text for b in api.last_to(ORGANIZER).buttons]

    clock.advance(60)
    await bot.join(bot.person(202, "Мария"), game)
    await bot.join(bot.person(203, "Пётр"), game)
    await jobs.organizer_notices(bot.ctx)
    await bot.drain()
    assert len(notices_to(api, ORGANIZER, "В игру")) == 1, "at most one notice per 10 minutes"

    clock.advance(9 * 60)
    await jobs.organizer_notices(bot.ctx)
    await bot.drain()
    assert notices_to(api, ORGANIZER, "В игру")[1:] == ["В игру «Семья Ивановых» вступили: Мария, Пётр. Всего: 4 из 10."]

    clock.advance(3600)
    await jobs.organizer_notices(bot.ctx)
    await bot.drain()
    assert len(notices_to(api, ORGANIZER, "В игру")) == 2, "nobody new, no notice"


async def test_waiting_notices_are_throttled_to_one_per_hour(bot: Bot, api: FakeMaxApi, clock: FakeClock) -> None:
    game = await full_game_with_waiting(bot, clock, bot.person(301, "Дмитрий"))
    await jobs.waiting_notices(bot.ctx)
    await bot.drain()
    first = notices_to(api, ORGANIZER, "В игру «Отдел продаж» хотят")
    assert first == ["В игру «Отдел продаж» хотят вступить ещё 1 чел., но мест нет. Расширить до 30 — 490 ₽."]
    assert [b.text for b in api.last_to(ORGANIZER).buttons] == [texts.BTN_UPGRADE_SHORT]

    clock.advance(30 * 60)
    await bot.join(bot.person(302, "Светлана"), game, wishes=None)
    await jobs.waiting_notices(bot.ctx)
    clock.advance(29 * 60)
    await jobs.waiting_notices(bot.ctx)
    await bot.drain()
    assert len(notices_to(api, ORGANIZER, "В игру «Отдел продаж» хотят")) == 1

    clock.advance(60)
    await jobs.waiting_notices(bot.ctx)
    await bot.drain()
    assert notices_to(api, ORGANIZER, "В игру «Отдел продаж» хотят")[-1].startswith(
        "В игру «Отдел продаж» хотят вступить ещё 2 чел.")


async def test_waiting_notice_without_a_paid_tier(bot: Bot) -> None:
    _, game = await organizer_game(bot, "Офис")
    notice = views.waiting_notice(dataclasses.replace(game, participant_limit=300), 4, None)
    assert notice.text == "В игру «Офис» хотят вступить ещё 4 чел., но мест нет."
    assert notice.keyboard is not None and [b.text for b in notice.keyboard.buttons()] == [texts.BTN_PANEL]


# --- §5.9 b, c ------------------------------------------------------------------------------------------------


async def test_organizer_nudge_is_sent_once_by_day(bot: Bot, api: FakeMaxApi, clock: FakeClock) -> None:
    _, game = await organizer_game(bot)
    far = await bot.create_game(bot.person(ORGANIZER, "Ольга"), "Дальняя")
    await repo.update_game(bot.ctx.db, game.id, exchange_date=date(2026, 11, 23))
    await repo.update_game(bot.ctx.db, far.id, exchange_date=date(2026, 11, 24))

    travel(clock, msk(20, 22, 30))
    await jobs.organizer_nudges(bot.ctx)
    await bot.drain()
    assert notices_to(api, ORGANIZER, "До обмена") == [], "not at night"

    travel(clock, msk(21, 10))
    await jobs.organizer_nudges(bot.ctx)
    await jobs.organizer_nudges(bot.ctx)
    await bot.drain()
    nudges = notices_to(api, ORGANIZER, "До обмена")
    assert nudges == ["До обмена в «Семья Ивановых» 2 дн., а жеребьёвки ещё не было. Участников: 1.",
                      "До обмена в «Дальняя» 3 дн., а жеребьёвки ещё не было. Участников: 1."]
    assert texts.BTN_DRAW in [b.text for b in api.last_to(ORGANIZER).buttons]


async def test_pre_exchange_reminder_the_day_before(bot: Bot, api: FakeMaxApi, clock: FakeClock) -> None:
    olga, game = await organizer_game(bot)
    people = [bot.person(501 + i, name) for i, name in enumerate(["Иван", "Мария", "Пётр"])]
    for someone in people:
        await bot.join(someone, game)
    await repo.update_game(bot.ctx.db, game.id, exchange_date=date(2026, 11, 25))
    await bot(olga.press(texts.BTN_PANEL))
    await bot(olga.press(texts.BTN_DRAW))
    await bot(olga.press(texts.BTN_CONFIRM_DRAW))
    await bot.drain()

    travel(clock, msk(23, 12))
    await jobs.pre_exchange_reminders(bot.ctx)
    await bot.drain()
    assert all(not notices_to(api, uid, "Завтра") for uid in (ORGANIZER, 501, 502, 503)), "two days before: nothing"

    travel(clock, msk(24, 12))
    await jobs.pre_exchange_reminders(bot.ctx)
    await jobs.pre_exchange_reminders(bot.ctx)
    await bot.drain()
    names = {ORGANIZER: "Ольга", 501: "Иван", 502: "Мария", 503: "Пётр"}
    pairs = await repo.assignments(bot.ctx.db, game.id)
    for giver, receiver in pairs.items():
        assert notices_to(api, giver, "Завтра") == [
            f"Завтра обмен подарками в игре «Семья Ивановых». Вы дарите: {names[receiver]}."]
        assert texts.BTN_WHOM_DO_I_GIFT in [b.text for b in api.last_to(giver).buttons]
    assert (await bot.game(game.id)).pre_exchange_sent


async def test_no_reminder_when_the_organizer_turned_it_off(bot: Bot, api: FakeMaxApi, clock: FakeClock) -> None:
    _, game = await organizer_game(bot)
    await repo.update_game(bot.ctx.db, game.id, exchange_date=date(2026, 11, 21), status=GameStatus.DRAWN,
                           reminder_on=False)
    await jobs.pre_exchange_reminders(bot.ctx)
    await bot.drain()
    assert notices_to(api, ORGANIZER, "Завтра") == []


# --- nightly jobs --------------------------------------------------------------------------------------------


async def test_backup_writes_a_readable_copy_and_keeps_14(ctx: AppContext, config: Config) -> None:
    await ctx.db.execute("INSERT INTO settings (key, value) VALUES ('marker', 'x')")
    await jobs.backup_database(ctx)
    path = config.backups_dir / "santa-20261120.db"
    assert path.exists() and oct(path.stat().st_mode & 0o777) == "0o600"
    with contextlib.closing(sqlite3.connect(path)) as copy:
        assert copy.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert copy.execute("SELECT value FROM settings WHERE key = 'marker'").fetchone() == ("x",)

    for day in range(1, 17):
        (config.backups_dir / backup.backup_name(date(2026, 10, day))).write_bytes(b"")
    (config.backups_dir / "notes.txt").write_text("не трогать")
    removed = backup.rotate(config.backups_dir)
    kept = sorted(p.name for p in config.backups_dir.iterdir())
    assert [p.name for p in removed] == ["santa-20261001.db", "santa-20261002.db", "santa-20261003.db"]
    assert len([name for name in kept if name.startswith("santa-")]) == 14
    assert "notes.txt" in kept and "santa-20261120.db" in kept


async def test_webhook_watchdog_restores_a_lost_subscription(
    ctx: AppContext, api: FakeMaxApi, config: Config
) -> None:
    await jobs.webhook_watchdog(ctx)
    assert [s.url for s in api.subscriptions] == [config.webhook_url] and ctx.runtime.webhook_registered
    await jobs.webhook_watchdog(ctx)
    api.subscriptions.clear()
    await jobs.webhook_watchdog(ctx)
    await ctx.outbox.drain()
    assert api.texts_to(ADMIN_ID) == [texts.WEBHOOK_RESTORED]
    assert [s.url for s in api.subscriptions] == [config.webhook_url]


async def test_rejected_token_alerts_the_admins(
    ctx: AppContext, api: FakeMaxApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unauthorized() -> list[object]:
        raise Unauthorized(401, "invalid token")

    monkeypatch.setattr(api, "list_subscriptions", unauthorized)
    await jobs.webhook_watchdog(ctx)
    await jobs.webhook_watchdog(ctx)
    await ctx.outbox.drain()
    assert api.texts_to(ADMIN_ID) == [texts.TOKEN_REJECTED]


async def test_certificate_expiry_is_announced_30_days_ahead(
    ctx: AppContext, api: FakeMaxApi, clock: FakeClock
) -> None:
    await jobs.certificate_check(ctx)
    travel(clock, datetime(2027, 2, 10, 12, tzinfo=timezone.utc))
    await jobs.certificate_check(ctx)
    await ctx.outbox.drain()
    assert api.texts_to(ADMIN_ID) == [
        "Сертификат Russian Trusted Sub CA истекает 6 марта 2027 (через 24 дн.). "
        "Скачайте новый с gu-st.ru, положите в certs/ и пересоберите бота."
    ]


async def test_digest_only_in_its_season(bot: Bot, api: FakeMaxApi, clock: FakeClock) -> None:
    await organizer_game(bot)
    travel(clock, msk(20, 21))
    await jobs.digest(bot.ctx)
    await bot.drain()
    assert api.texts_to(ADMIN_ID) == [
        "Сводка\n"
        "Вчера: игр 0, жеребьёвок 0, оплат 0 на 0 ₽, новых пользователей 0\n"
        "Сегодня: игр 1, жеребьёвок 0, оплат 0 на 0 ₽, новых пользователей 1"
    ]
    travel(clock, datetime(2027, 1, 11, 21, tzinfo=MSK))
    await jobs.digest(bot.ctx)
    await bot.drain()
    assert len(api.texts_to(ADMIN_ID)) == 1


def test_season_window_wraps_the_new_year() -> None:
    assert in_season_window(date(2026, 11, 1), "11-01", "01-10")
    assert in_season_window(date(2027, 1, 10), "11-01", "01-10")
    assert not in_season_window(date(2026, 10, 31), "11-01", "01-10")
    assert not in_season_window(date(2027, 1, 11), "11-01", "01-10")
    assert in_season_window(date(2026, 6, 15), "06-01", "06-30")
    assert not in_season_window(date(2026, 7, 1), "06-01", "06-30")

