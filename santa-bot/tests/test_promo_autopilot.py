"""PROMO_SPEC §6: the daily run and the guard, test vs autopilot mode, approvals, alerts and failures."""

from __future__ import annotations

import dataclasses
from datetime import date, time, timedelta

import pytest

from app.config import Config
from app.context import AppContext
from app.core import texts
from app.core.clock import FakeClock
from app.promo import autopilot, links, store
from app.promo.platforms import AuthError, Budget, BudgetKind, CampaignState, Platform, Transient
from app.promo.rules import PausedBy
from app.promo.store import ActionStatus, PostStatus
from app.scheduler import DailyAt, Every, jobs_for
from tests.bot import ADMIN_ID, Bot
from tests.promo_helpers import games_from, msk, person, travel
from tools.fake_ads import FakeAdPlatform
from tools.fake_max import FakeMaxApi

WEEK, DAY = BudgetKind.WEEK, BudgetKind.DAY


@pytest.fixture
def env(env: dict[str, str]) -> dict[str, str]:
    return {**env, "PROMO_ENABLED": "1", "PROMO_START": "11-15"}


@pytest.fixture
def direct(ctx: AppContext) -> FakeAdPlatform:
    platform = FakeAdPlatform(Platform.DIRECT)
    ctx.ad_platforms[Platform.DIRECT] = platform
    return platform


@pytest.fixture
def vk(ctx: AppContext) -> FakeAdPlatform:
    platform = FakeAdPlatform(Platform.VK)
    ctx.ad_platforms[Platform.VK] = platform
    return platform


async def expensive_and_cheap(ctx: AppContext, direct: FakeAdPlatform, vk: FakeAdPlatform) -> None:
    """«Поиск» spent 900 ₽ and brought no game; «Сайт» brought 3 games for 300 ₽."""
    direct.add("701", "Поиск", budget=Budget(3000_00, WEEK))
    vk.add("55", "Сайт", budget=Budget(500_00, DAY))
    await autopilot.register(ctx, Platform.DIRECT, "701", "")
    await autopilot.register(ctx, Platform.VK, "55", "")
    direct.spend_on("701", date(2026, 11, 21), 900_00)
    vk.spend_on("55", date(2026, 11, 21), 300_00)
    await games_from(ctx.db, "vk55", 3, seen=msk(21, 15))


async def report_and_messages(ctx: AppContext, api: FakeMaxApi) -> list[str]:
    await ctx.outbox.drain()
    return api.texts_to(ADMIN_ID)


async def test_test_mode_says_what_it_would_do_and_changes_nothing(ctx: AppContext, api: FakeMaxApi,
                                                                   clock: FakeClock, direct: FakeAdPlatform,
                                                                   vk: FakeAdPlatform) -> None:
    await expensive_and_cheap(ctx, direct, vk)
    travel(clock, msk(25))

    await autopilot.daily_job(ctx)

    assert direct.mutations() == [] and vk.mutations() == []
    (report,) = await report_and_messages(ctx, api)
    assert report == (
        "Реклама — 25 ноября. Режим: тест — ничего не меняю.\n"
        "Потрачено всего 1 200 ₽ из 15 000 ₽.\n"
        "Яндекс «Поиск»: вчера 0 ₽, всего 900 ₽ · игр на 3+: 0 · — ₽ за игру · оплат 0 ₽ · идёт\n"
        "VK «Сайт»: вчера 0 ₽, всего 300 ₽ · игр на 3+: 3 · 100 ₽ за игру · оплат 0 ₽ · идёт\n"
        "Сделал бы: «Поиск» — пауза: по 22 ноября потрачено 900 ₽, а игр на 3+ участника нет.\n"
        "Предложил бы: поднять бюджет «Сайт» с 500 до 650 ₽ в день: по 22 ноября игр на 3+ участника: 3, "
        "по 100 ₽ за игру — дешевле порога 150 ₽."
    )
    rows = await ctx.db.fetchall("SELECT src, action, mode, status FROM promo_actions ORDER BY id")
    assert [tuple(row) for row in rows] == [("yd701", "pause", "dry", "proposed"),
                                            ("vk55", "set_budget", "dry", "proposed")]


async def test_autopilot_pauses_the_expensive_and_asks_before_raising(bot: Bot, api: FakeMaxApi, clock: FakeClock,
                                                                      direct: FakeAdPlatform,
                                                                      vk: FakeAdPlatform) -> None:
    ctx = bot.ctx
    await expensive_and_cheap(ctx, direct, vk)
    await store.set_settings(ctx.db, auto=True)
    travel(clock, msk(25))

    await autopilot.daily_job(ctx)

    assert direct.mutations() == [("suspend", "701")] and vk.mutations() == []
    report, proposal = await report_and_messages(ctx, api)
    assert "Режим: автопилот." in report
    assert "Яндекс «Поиск»: вчера 0 ₽, всего 900 ₽ · игр на 3+: 0 · — ₽ за игру · оплат 0 ₽ · на паузе " \
           "(остановил автопилот)" in report
    assert "Сделал: «Поиск» — пауза: по 22 ноября потрачено 900 ₽, а игр на 3+ участника нет." in report
    assert "Предлагаю: поднять бюджет «Сайт» с 500 до 650 ₽ в день:" in report
    assert proposal.startswith("Предлагаю поднять бюджет «Сайт» с 500 до 650 ₽ в день:")
    assert proposal.endswith("В кабинете это будет 532,78 ₽ без НДС. Предложение действует сутки.")
    assert api.button_texts(ADMIN_ID) == ["Поднять до 650 ₽", "Не надо"]
    paused = await store.get_campaign(ctx.db, "yd701")
    assert paused is not None and (paused.state, paused.paused_by) == (CampaignState.PAUSED, PausedBy.AUTOPILOT)

    admin = bot.person(ADMIN_ID, "Админ")
    await bot(admin.press("Поднять до 650 ₽"))

    assert vk.mutations() == [("set_budget", "55", 650_00, DAY)]
    assert admin.screen_text == "Готово: бюджет «Сайт» теперь 650 ₽ в день (в кабинете 532,78 ₽ без НДС)."
    raised = await store.get_campaign(ctx.db, "vk55")
    assert raised is not None and raised.budget == Budget(650_00, DAY)
    assert raised.last_budget_change_day == date(2026, 11, 25)
    action = await ctx.db.fetchone("SELECT status, mode, decided_by FROM promo_actions WHERE src = 'vk55'")
    assert action is not None and tuple(action) == ("applied", "auto", ADMIN_ID)

    await bot.forge(admin, f"pa:{await proposal_id(ctx)}")  # a second tap, or the other admin's copy
    assert admin.last_text == "Этот бюджет уже подняли." and len(vk.mutations()) == 1


async def proposal_id(ctx: AppContext) -> int:
    return int(await ctx.db.fetchval("SELECT id FROM promo_actions WHERE action = 'set_budget'"))


async def test_declining_keeps_the_budget(bot: Bot, clock: FakeClock, direct: FakeAdPlatform,
                                          vk: FakeAdPlatform) -> None:
    await expensive_and_cheap(bot.ctx, direct, vk)
    await store.set_settings(bot.ctx.db, auto=True)
    travel(clock, msk(25))
    await autopilot.daily_job(bot.ctx)
    await bot.drain()
    admin = bot.person(ADMIN_ID, "Админ")

    await bot(admin.press("Не надо"))

    assert admin.screen_text == "Хорошо, бюджет «Сайт» оставляю 500 ₽."
    assert vk.mutations() == []
    assert await bot.ctx.db.fetchval("SELECT status FROM promo_actions WHERE src = 'vk55'") == "declined"
    await bot.forge(admin, f"pa:{await proposal_id(bot.ctx)}")
    assert admin.last_text == "От этого предложения уже отказались." and vk.mutations() == []


@pytest.mark.parametrize(("change", "reason"), [
    ("age", texts.PROMO_EXPIRED_OLD),
    ("cap", texts.promo_expired_cap(1000_00)),
    ("paused", texts.PROMO_EXPIRED_NOT_RUNNING),
    ("budget", texts.PROMO_EXPIRED_CHANGED),
])
async def test_a_stale_proposal_expires_and_says_why(bot: Bot, clock: FakeClock, direct: FakeAdPlatform,
                                                     vk: FakeAdPlatform, change: str, reason: str) -> None:
    ctx = bot.ctx
    await expensive_and_cheap(ctx, direct, vk)
    await store.set_settings(ctx.db, auto=True)
    travel(clock, msk(25))
    await autopilot.daily_job(ctx)
    await bot.drain()
    if change == "age":
        clock.advance(25 * 3600)
    elif change == "cap":
        await store.set_settings(ctx.db, cap_rub=1000)
    elif change == "paused":
        await store.set_state(ctx.db, "vk55", CampaignState.PAUSED, PausedBy.ADMIN)
    else:
        await store.set_budget(ctx.db, "vk55", Budget(550_00, DAY), date(2026, 11, 24))
    admin = bot.person(ADMIN_ID, "Админ")

    await bot(admin.press("Поднять до 650 ₽"))

    assert admin.screen_text == texts.promo_proposal_expired(reason)
    assert vk.mutations() == []
    assert await ctx.db.fetchval("SELECT status FROM promo_actions WHERE src = 'vk55'") == "expired"


async def test_test_mode_proposals_have_no_buttons_and_cannot_be_approved(bot: Bot, api: FakeMaxApi,
                                                                          clock: FakeClock, direct: FakeAdPlatform,
                                                                          vk: FakeAdPlatform) -> None:
    await expensive_and_cheap(bot.ctx, direct, vk)
    travel(clock, msk(25))
    await autopilot.daily_job(bot.ctx)
    await bot.drain()
    assert api.button_texts(ADMIN_ID) == []
    admin = bot.person(ADMIN_ID, "Админ")
    await bot.forge(admin, f"pa:{await proposal_id(bot.ctx)}")
    assert admin.last_text == texts.PROMO_PROPOSAL_DRY and vk.mutations() == []


async def test_proposals_expire_after_a_day_even_without_a_tap(ctx: AppContext, clock: FakeClock,
                                                               direct: FakeAdPlatform, vk: FakeAdPlatform) -> None:
    await expensive_and_cheap(ctx, direct, vk)
    travel(clock, msk(25))
    await autopilot.daily_job(ctx)
    travel(clock, msk(26, 10, 30))
    await autopilot.daily_job(ctx)
    statuses = [row[0] for row in await ctx.db.fetchall("SELECT status FROM promo_actions ORDER BY id")]
    assert statuses == ["expired", "expired", "proposed", "proposed"]


async def test_the_guard_acts_only_on_the_cap_and_the_season(ctx: AppContext, api: FakeMaxApi, clock: FakeClock,
                                                             direct: FakeAdPlatform) -> None:
    direct.add("701", "Поиск", budget=Budget(7000_00, WEEK))  # 1000 ₽ a day
    await autopilot.register(ctx, Platform.DIRECT, "701", "")
    await store.set_settings(ctx.db, auto=True)
    direct.spend_on("701", date(2026, 11, 18), 900_00)  # expensive, but that is the daily run's business
    direct.spend_on("701", date(2026, 11, 20), 900_00)

    await autopilot.guard_job(ctx)

    assert direct.mutations() == [] and await report_and_messages(ctx, api) == []
    assert direct.calls[-1] == ("daily_spend", ("701",), date(2026, 11, 20), date(2026, 11, 20)), "today only"
    direct.spend_on("701", date(2026, 11, 20), 14000_01)
    clock.advance(2 * 3600)

    await autopilot.guard_job(ctx)

    assert direct.mutations() == [("suspend", "701")]
    (message,) = await report_and_messages(ctx, api)
    reason = texts.promo_reason_cap(cap_kop=15000_00, spent_kop=14000_01, projected_kop=15000_01)
    assert message == f"Защита расходов:\nСделал: «Поиск» — пауза: {reason}."
    campaign = await store.get_campaign(ctx.db, "yd701")
    assert campaign is not None and campaign.paused_by == PausedBy.CAP


async def test_the_guard_sleeps_in_test_mode(ctx: AppContext, api: FakeMaxApi, direct: FakeAdPlatform) -> None:
    direct.add("701", "Поиск", budget=Budget(7000_00, WEEK))
    await autopilot.register(ctx, Platform.DIRECT, "701", "")
    direct.spend_on("701", date(2026, 11, 20), 20000_00)
    calls = len(direct.calls)
    await autopilot.guard_job(ctx)
    assert len(direct.calls) == calls and await report_and_messages(ctx, api) == []


async def test_after_the_season_everything_stops(ctx: AppContext, api: FakeMaxApi, clock: FakeClock,
                                                 direct: FakeAdPlatform, vk: FakeAdPlatform) -> None:
    await expensive_and_cheap(ctx, direct, vk)
    await store.set_settings(ctx.db, auto=True)
    travel(clock, msk(13, month=12))
    await autopilot.daily_job(ctx)
    assert direct.mutations() == [("suspend", "701")] and vk.mutations() == [("suspend", "55")]
    (report,) = await report_and_messages(ctx, api)
    assert report.count("пауза: сезон закончился.") == 2


async def test_auth_errors_alert_once_per_six_hours_while_the_other_platform_works(
    ctx: AppContext, api: FakeMaxApi, clock: FakeClock, direct: FakeAdPlatform, vk: FakeAdPlatform
) -> None:
    direct.add("701", "Поиск", budget=Budget(3000_00, WEEK))
    vk.add("55", "Сайт", budget=Budget(500_00, DAY))
    await autopilot.register(ctx, Platform.DIRECT, "701", "")
    await autopilot.register(ctx, Platform.VK, "55", "")
    vk.spend_on("55", date(2026, 11, 20), 100_00)
    direct.fail("list_campaigns", AuthError("53", "Неверный OAuth-токен"), times=3)
    alert = texts.promo_auth_alert(platform="yd", code="53", detail="Неверный OAuth-токен")

    await autopilot.daily_job(ctx)

    alert_text, report = await report_and_messages(ctx, api)
    assert alert_text == alert
    assert alert.startswith("Яндекс Директ не принял токен (ошибка 53)") and "YANDEX_DIRECT_TOKEN" in alert
    assert texts.promo_auth_note("yd") in report
    assert (await store.spend_totals(ctx.db, date(2026, 11, 20), date(2026, 11, 18)))["vk55"].total_kop == 100_00

    await store.set_settings(ctx.db, auto=True)
    clock.advance(2 * 3600)
    await autopilot.guard_job(ctx)
    assert await report_and_messages(ctx, api) == [alert_text, report], "throttled"
    clock.advance(6 * 3600)
    await autopilot.guard_job(ctx)
    assert (await report_and_messages(ctx, api))[-1] == texts.with_suppressed(alert, 1)


async def test_platforms_without_credentials_are_skipped_with_a_note(ctx: AppContext, api: FakeMaxApi,
                                                                     direct: FakeAdPlatform) -> None:
    campaign, checked = await autopilot.register(ctx, Platform.VK, "77", "Сайт")
    assert not checked and campaign.state == CampaignState.OTHER
    await autopilot.daily_job(ctx)
    (report,) = await report_and_messages(ctx, api)
    assert texts.promo_no_credentials("vk") in report
    assert "VK «Сайт»" in report


async def test_a_failed_pause_is_reported_with_what_to_do(ctx: AppContext, api: FakeMaxApi, clock: FakeClock,
                                                          direct: FakeAdPlatform, vk: FakeAdPlatform) -> None:
    await expensive_and_cheap(ctx, direct, vk)
    await store.set_settings(ctx.db, auto=True)
    direct.fail("suspend", Transient("timeout"))
    travel(clock, msk(25))
    await autopilot.daily_job(ctx)
    (report, _proposal) = await report_and_messages(ctx, api)
    assert ("Не получилось: «Поиск» — пауза (по 22 ноября потрачено 900 ₽, а игр на 3+ участника нет) не удалась: "
            "площадка не ответила. Остановите кампанию вручную в кабинете или командой /ads stop.") in report
    row = await ctx.db.fetchone("SELECT status, error FROM promo_actions WHERE src = 'yd701'")
    assert row is not None and tuple(row) == ("failed", texts.PROMO_ERROR_DOWN)
    campaign = await store.get_campaign(ctx.db, "yd701")
    assert campaign is not None and campaign.state == CampaignState.ACTIVE


async def test_the_report_counts_links_and_the_channel(ctx: AppContext, api: FakeMaxApi, clock: FakeClock) -> None:
    await links.create_link(ctx.db, "phabr", "Хабр", ADMIN_ID, clock.now())
    await games_from(ctx.db, "phabr", 1, seen=msk(20))
    await person(ctx.db, "s:phabr", msk(20))
    await store.mark_post(ctx.db, "ch01", PostStatus.SENT, clock.now())
    await store.mark_post(ctx.db, "ch02", PostStatus.SKIPPED, clock.now())
    await games_from(ctx.db, "ch01", 1, seen=msk(20))

    await autopilot.daily_job(ctx)

    (report,) = await report_and_messages(ctx, api)
    assert report.endswith("Кампаний под управлением пока нет. Подключить: /ads add yd НОМЕР или /ads add vk НОМЕР.\n"
                           "Ссылки: habr: 2 чел., 1 игра, 1 на 3+, оплат 0 ₽\n"
                           "Канал MAX: постов 1, пришло 1 чел., игр 1.")


async def test_nothing_to_report_sends_nothing(ctx: AppContext, api: FakeMaxApi) -> None:
    await autopilot.daily_job(ctx)
    assert await report_and_messages(ctx, api) == []


def test_the_jobs_exist_only_with_promo_enabled(config: Config) -> None:
    jobs = {job.name: job for job in jobs_for(config)}
    assert jobs["promo_daily"].schedule == DailyAt(time(10, 0))
    assert jobs["promo_guard"].schedule == Every(2 * 3600)
    assert jobs["promo_channel"].schedule == Every(600)
    disabled = dataclasses.replace(config, promo=dataclasses.replace(config.promo, enabled=False))
    assert not {name for name in (job.name for job in jobs_for(disabled)) if name.startswith("promo")}


async def test_admin_paused_campaigns_stay_admin_paused_when_resumed_in_the_cabinet(
    ctx: AppContext, clock: FakeClock, direct: FakeAdPlatform, vk: FakeAdPlatform
) -> None:
    await expensive_and_cheap(ctx, direct, vk)
    await store.set_settings(ctx.db, auto=True)
    await autopilot.stop_all(ctx, ADMIN_ID)
    direct.add("701", "Поиск", budget=Budget(3000_00, WEEK))  # the owner started it again in the cabinet
    travel(clock, msk(25))
    await autopilot.daily_job(ctx)
    campaign = await store.get_campaign(ctx.db, "yd701")
    assert campaign is not None and (campaign.state, campaign.paused_by) == (CampaignState.ACTIVE, PausedBy.ADMIN)
    assert direct.mutations() == [("suspend", "701")], "rule 3 leaves it to the admin"
    assert await ctx.db.fetchval("SELECT COUNT(*) FROM promo_actions WHERE status = ?",
                                 (ActionStatus.PROPOSED,)) == 0, "no raise for the admin-paused VK campaign"


async def test_spend_is_fetched_since_registration_then_for_a_week(ctx: AppContext, clock: FakeClock,
                                                                   direct: FakeAdPlatform) -> None:
    direct.add("701", "Поиск", budget=Budget(3000_00, WEEK))
    await autopilot.register(ctx, Platform.DIRECT, "701", "")
    travel(clock, msk(30))
    await autopilot.daily_job(ctx)
    assert direct.calls[-1] == ("daily_spend", ("701",), date(2026, 11, 20), date(2026, 11, 30))
    direct.spend_on("701", date(2026, 11, 29), 100_00)
    clock.advance(timedelta(days=1).total_seconds())
    await autopilot.daily_job(ctx)
    await autopilot.daily_job(ctx)
    assert direct.calls[-1] == ("daily_spend", ("701",), date(2026, 11, 25), date(2026, 12, 1))
