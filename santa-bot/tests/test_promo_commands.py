"""PROMO_SPEC §7: /ads, /link, /links and /channel through the webhook path; only admins may use them."""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from app.core import texts
from app.core.clock import FakeClock
from app.promo import content, store
from app.promo.platforms import AuthError, Budget, BudgetKind, CampaignState, Platform, Transient
from app.promo.rules import PausedBy
from tests.bot import ADMIN_ID, Bot
from tests.promo_helpers import game, msk, person, travel
from tools import fake_max
from tools.fake_ads import FakeAdPlatform
from tools.fake_max import FakeMaxApi, FakeUser

WEEK = BudgetKind.WEEK
SITE = "https://santa.example.ru"


@pytest.fixture
def env(env: dict[str, str]) -> dict[str, str]:
    return {**env, "PROMO_ENABLED": "1", "PROMO_START": "11-15", "PROMO_MAX_CHANNEL_ID": "-500"}


@pytest.fixture
def admin(bot: Bot) -> FakeUser:
    return bot.person(ADMIN_ID, "Админ")


@pytest.fixture
def direct(bot: Bot) -> FakeAdPlatform:
    platform = FakeAdPlatform(Platform.DIRECT)
    platform.add("701234567", "Тайный Санта — поиск", budget=Budget(3000_00, WEEK))
    platform.add("702", "Запасная", budget=Budget(2100_00, WEEK))
    bot.ctx.ad_platforms[Platform.DIRECT] = platform
    return platform


async def test_ads_add_checks_the_campaign_and_gives_the_tracking_links(bot: Bot, admin: FakeUser,
                                                                         direct: FakeAdPlatform) -> None:
    await bot(admin.say("/ads add yd 701234567"))

    reply = admin.last_text
    assert reply.startswith("Подключил: Яндекс Директ «Тайный Санта — поиск», источник yd701234567.")
    assert f"\n{SITE}/?src=yd701234567\n" in reply
    assert f"\n{SITE}/?src=yd{{campaign_id}}\n" in reply, "one URL for every Direct campaign"
    assert "Проверить кампанию не смог" not in reply
    campaign = await store.get_campaign(bot.ctx.db, "yd701234567")
    assert campaign is not None and campaign.plan_budget == Budget(3000_00, WEEK)
    assert campaign.state == CampaignState.ACTIVE


async def test_ads_add_vk_without_credentials_registers_with_a_warning(bot: Bot, admin: FakeUser) -> None:
    await bot(admin.say("/ads add vk 55 Сайт для офисов"))
    reply = admin.last_text
    assert reply.startswith("Подключил: VK Реклама «Сайт для офисов», источник vk55.")
    assert "Проверить кампанию не смог" in reply
    assert f"{SITE}/?src=vk55" in reply and "\nsrc=vk{{ad_plan_id}}\n" in reply


@pytest.mark.parametrize("command", ["/ads add", "/ads add ok 1", "/ads add yd", "/ads add yd 12a",
                                     "/ads add yd 123456789012345"])
async def test_ads_add_usage(bot: Bot, admin: FakeUser, command: str) -> None:
    await bot(admin.say(command))
    assert admin.last_text == texts.PROMO_ADD_USAGE


async def test_ads_add_refusals(bot: Bot, admin: FakeUser, direct: FakeAdPlatform) -> None:
    await bot(admin.say("/ads add yd 999"))
    assert admin.last_text == texts.promo_not_found(platform="yd", external_id="999")
    direct.fail("list_campaigns", AuthError("53", "Неверный токен"))
    await bot(admin.say("/ads add yd 701234567"))
    assert admin.last_text == texts.promo_auth_alert(platform="yd", code="53", detail="Неверный токен")
    direct.fail("list_campaigns", Transient("timeout"))
    await bot(admin.say("/ads add yd 701234567"))
    assert admin.last_text == texts.promo_unreachable(platform="yd", detail=texts.PROMO_ERROR_DOWN)
    assert await store.enabled_campaigns(bot.ctx.db) == []


async def test_ads_status(bot: Bot, admin: FakeUser, direct: FakeAdPlatform) -> None:
    await bot(admin.say("/ads"))
    assert admin.last_text.endswith(texts.PROMO_NO_CAMPAIGNS)
    await bot(admin.say("/ads add yd 701234567"))
    direct.spend_on("701234567", date(2026, 11, 19), 490_00)
    await bot(admin.say("/ads"))
    assert admin.last_text.splitlines()[0] == "Реклама. Режим: тест — ничего не меняю."
    assert "Пороги: останавливаю кампанию, если игра на 3+ участника обходится дороже 250 ₽" in admin.last_text
    assert ("Яндекс «Тайный Санта — поиск» (yd701234567): идёт · вчера 0 ₽, всего 0 ₽ · игр на 3+: 0 · — ₽ за игру"
            " · оплат 0 ₽, окупаемость — · бюджет 3 000 ₽ в неделю") in admin.last_text

    db = bot.ctx.db
    first = await game(db, await person(db, "s:yd701234567", msk(10)), active=3, created=msk(10))
    await game(db, await person(db, "direct", msk(12)), active=2, created=msk(12), source_game=first)
    await bot(admin.say("/ads"))
    assert ("игр на 3+: 1 · 0 ₽ за игру · оплат 0 ₽, окупаемость — · участники этих игр устроили ещё 1 игру"
            " · бюджет") in admin.last_text, "the games that people from the ad's games organized later"
    await bot(admin.say("/ads help"))
    assert admin.last_text == texts.PROMO_ADS_USAGE


async def test_ads_auto_on_and_off(bot: Bot, admin: FakeUser) -> None:
    await bot(admin.say("/ads auto on"))
    assert admin.last_text.startswith("Автопилот включён. Что я теперь делаю сам:")
    assert "лимит 15 000 ₽ (сейчас потрачено 0 ₽)" in admin.last_text
    assert (await store.get_settings(bot.ctx.db)).auto
    await bot(admin.say("/ads auto off"))
    assert admin.last_text == texts.PROMO_AUTO_OFF and not (await store.get_settings(bot.ctx.db)).auto
    await bot(admin.say("/ads auto maybe"))
    assert admin.last_text == texts.PROMO_AUTO_USAGE


async def test_ads_stop_and_resume(bot: Bot, admin: FakeUser, direct: FakeAdPlatform, clock: FakeClock) -> None:
    for campaign_id in ("701234567", "702"):
        await bot(admin.say(f"/ads add yd {campaign_id}"))

    await bot(admin.say("/ads stop"))

    assert direct.mutations() == [("suspend", "701234567"), ("suspend", "702")]
    assert admin.last_text == ("Остановил все кампании (автопилот их больше не запустит):\n"
                               "«Тайный Санта — поиск» — остановлена\n«Запасная» — остановлена\n"
                               "Запустить снова: /ads resume ИСТОЧНИК.")
    stopped = await store.get_campaign(bot.ctx.db, "yd702")
    assert stopped is not None and (stopped.state, stopped.paused_by) == (CampaignState.PAUSED, PausedBy.ADMIN)
    assert await bot.ctx.db.fetchval("SELECT status FROM promo_actions WHERE action = 'suspend_all'") == "applied"

    await bot(admin.say("/ads resume YD702"))
    assert admin.last_text == texts.promo_resumed("Запасная")
    resumed = await store.get_campaign(bot.ctx.db, "yd702")
    assert resumed is not None and (resumed.state, resumed.paused_by) == (CampaignState.ACTIVE, None)

    await store.set_settings(bot.ctx.db, cap_rub=500)
    await bot(admin.say("/ads resume yd701234567"))
    assert admin.last_text == texts.promo_over_cap(cap_kop=500_00, projected_kop=429_00 + 300_00)
    travel(clock, msk(13, month=12))
    await bot(admin.say("/ads resume yd701234567"))
    assert admin.last_text == texts.promo_resume_out_of_season(ended=True)
    assert direct.mutations()[-1] == ("resume", "702")
    await bot(admin.say("/ads resume yd000"))
    assert admin.last_text == texts.promo_unknown_src("yd000")


async def test_ads_budget(bot: Bot, admin: FakeUser, direct: FakeAdPlatform) -> None:
    await bot(admin.say("/ads add yd 701234567"))

    await bot(admin.say("/ads budget yd701234567 2000"))

    assert direct.mutations() == [("set_budget", "701234567", 2000_00, WEEK)]
    assert admin.last_text == ("Бюджет «Тайный Санта — поиск»: 2 000 ₽ в неделю с НДС, в кабинете будет "
                               "1 639,34 ₽ без НДС.")
    campaign = await store.get_campaign(bot.ctx.db, "yd701234567")
    assert campaign is not None and campaign.budget == Budget(2000_00, WEEK)
    await bot(admin.say("/ads budget yd701234567 300"))
    assert admin.last_text == texts.promo_budget_too_small(366_00)
    await store.set_settings(bot.ctx.db, cap_rub=1000)
    await bot(admin.say("/ads budget yd701234567 20000"))
    assert admin.last_text.startswith("Не делаю: с этим расходы за следующий день могут дойти до 2 857 ₽")
    direct.fail("set_budget", AuthError("53"))
    await bot(admin.say("/ads budget yd701234567 400"))
    assert admin.last_text.startswith("Не удалось поменять бюджет «Тайный Санта — поиск»: площадка не приняла ключи")
    for bad in ("/ads budget yd701234567", "/ads budget yd701234567 много", "/ads budget 2000"):
        await bot(admin.say(bad))
        assert admin.last_text == texts.PROMO_BUDGET_USAGE, bad


async def test_ads_cap_rules_and_remove(bot: Bot, admin: FakeUser, direct: FakeAdPlatform) -> None:
    await bot(admin.say("/ads cap 20000"))
    assert admin.last_text == "Общий лимит расходов: 20 000 ₽ с НДС. Уже потрачено 0 ₽."
    await bot(admin.say("/ads rules 300 100 600 20"))
    settings = await store.get_settings(bot.ctx.db)
    assert (settings.cap_rub, settings.pause_cpa_rub, settings.scale_cpa_rub, settings.min_spend_rub,
            settings.max_raise_pct, settings.lag_days) == (20000, 300, 100, 600, 20, 2)
    assert admin.last_text.startswith("Пороги: останавливаю кампанию, если игра на 3+ участника обходится дороже 300 ₽")
    await bot(admin.say("/ads rules 300 100 600 20 0"))
    assert (await store.get_settings(bot.ctx.db)).lag_days == 0
    assert "Игры считаю с задержкой 0 дн." in admin.last_text
    for bad in ("/ads rules 100 300", "/ads rules 300 100 600 150", "/ads rules 300", "/ads rules 300 100 0",
                "/ads rules 300 100 600 20 15", "/ads rules 300 100 600 20 2 1", "/ads rules 300 100 -5",
                "/ads cap", "/ads cap 0"):
        await bot(admin.say(bad))
        assert admin.last_text in (texts.PROMO_RULES_USAGE, texts.PROMO_CAP_USAGE), bad
    await bot(admin.say("/ads add yd 702"))
    await bot(admin.say("/ads remove yd702"))
    assert admin.last_text == texts.promo_removed(name="Запасная", src="yd702")
    assert await store.enabled_campaigns(bot.ctx.db) == [] and direct.mutations() == []
    await bot(admin.say("/ads remove yd702"))
    assert admin.last_text == texts.promo_unknown_src("yd702")
    await bot(admin.say("/ads remove"))
    assert admin.last_text == texts.PROMO_SRC_USAGE


async def test_link_and_links(bot: Bot, admin: FakeUser) -> None:
    await bot(admin.say("/links"))
    assert admin.last_text == texts.LINKS_EMPTY

    await bot(admin.say("/link Habr Пост на Хабре"))

    assert admin.last_text == (
        "Ссылка «Пост на Хабре» (phabr) готова.\n"
        "На сайт (лучше для постов и сообщений — там есть описание и цены):\n"
        f"{SITE}/?src=phabr\nСразу в бота:\nhttps://max.ru/santa_test_bot?start=s_phabr\nЧто она принесла — /links."
    )
    await bot(admin.say("/link habr другое название"))
    assert admin.last_text.startswith("Такая ссылка уже есть: «Пост на Хабре» (phabr).")
    for bad in ("/link", "/link хабр", "/link habr-post", "/link abcdefghijklmnop"):
        await bot(admin.say(bad))
        assert admin.last_text == texts.LINK_USAGE, bad

    reader = bot.person(501, "Читатель")
    await bot.onboard(reader, "s_phabr")
    await bot(admin.say("/links"))
    assert admin.last_text == ("Ссылки (люди, игры, игры на 3+ участника, оплаты):\n"
                               "«Пост на Хабре» — habr: 1 чел., 0 игр, 0 на 3+, оплат 0 ₽")


async def test_channel_commands(bot: Bot, admin: FakeUser, api: FakeMaxApi, clock: FakeClock) -> None:
    await bot(admin.say("/channel"))
    status = admin.last_text
    assert status.startswith("Посты в канал MAX: выключены. Канал: -500.\nКалендарь: 16 постов, отправлено 0, "
                             "пропущено 0.\nСледующие:")
    assert ("Следующие:\nch05 — 19 ноября, 12:00: Бюджет подарка: как договориться и никого не смутить\n"
            "ch06 — 23 ноября, 12:00: 10 идей подарков до 500 ₽\n"
            "ch07 — 26 ноября, 12:00: 10 идей подарков до 1000 ₽\n") in status, "a post a day late still goes out"

    await bot(admin.say("/channel test"))
    preview, post = api.messages_to(ADMIN_ID)[-2:]
    assert preview.text == "Так будет выглядеть пост ch05 (по плану — 19 ноября, 12:00):"
    assert post.text == content.post_by_id("ch05").text  # type: ignore[union-attr]
    assert [button.text for button in post.buttons] == [texts.BTN_PROMO_CHANNEL]

    await bot(admin.say("/channel on"))
    assert admin.last_text == texts.channel_on(channel_id=-500) and (await store.get_settings(bot.ctx.db)).channel_on
    await bot(admin.say("/channel off"))
    assert admin.last_text == texts.CHANNEL_OFF

    await bot(admin.say("/channel send CH03"))
    assert admin.last_text == texts.channel_sent("ch03")
    await bot.drain()
    ch03 = content.post_by_id("ch03")
    assert ch03 is not None and [message.text for message in api.messages_in_chat(-500)] == [ch03.text]
    await bot(admin.say("/channel send ch03"))
    assert admin.last_text == texts.channel_already_sent("ch03")
    await bot(admin.say("/channel send ch99"))
    assert admin.last_text == texts.channel_unknown_post("ch99")
    await bot(admin.say("/channel sometimes"))
    assert admin.last_text == texts.CHANNEL_USAGE


async def test_channel_needs_its_id(bot: Bot, admin: FakeUser) -> None:
    config = bot.ctx.config
    bot.ctx.config = dataclasses.replace(config, promo=dataclasses.replace(config.promo, channel_id=None))
    for command in ("/channel on", "/channel send ch01"):
        await bot(admin.say(command))
        assert admin.last_text == texts.CHANNEL_NO_ID


async def test_the_bot_added_to_a_channel_tells_the_admins_its_id(bot: Bot, admin: FakeUser, api: FakeMaxApi) -> None:
    await bot(fake_max.bot_added(-777, 300, is_channel=True))
    await bot.drain()
    assert api.messages_in_chat(-777) == [], "nothing is posted into the channel"
    assert api.last_text(ADMIN_ID) == texts.channel_added(-777)
    await bot(admin.say("/channel"))
    assert "Каналы, куда добавили бота: -777" in admin.last_text


async def test_only_admins_and_only_with_promo_enabled(bot: Bot, admin: FakeUser) -> None:
    ivan = bot.person(501, "Иван")
    await bot.onboard(ivan)
    for command in ("/ads", "/ads stop", "/link x", "/links", "/channel"):
        await bot(ivan.say(command))
        assert ivan.last_text == texts.UNKNOWN_INPUT, command
    await bot.forge(ivan, "pa:1")
    assert ivan.last_text == texts.NOT_ALLOWED
    await bot.forge(ivan, "pd:1")
    assert ivan.last_text == texts.NOT_ALLOWED

    config = bot.ctx.config
    bot.ctx.config = dataclasses.replace(config, promo=dataclasses.replace(config.promo, enabled=False))
    for command in ("/ads", "/link habr", "/links", "/channel"):
        await bot(admin.say(command))
        assert admin.last_text == texts.PROMO_DISABLED, command


async def test_admin_help_lists_the_promo_commands(bot: Bot, admin: FakeUser) -> None:
    await bot(admin.say("/admin"))
    assert "/ads — реклама" in admin.last_text and "/link СЛОВО" in admin.last_text and "/channel" in admin.last_text
