"""Website (§8): pages, security headers, attribution, counter, payment pages, /healthz, CSV export."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import pytest
from aiohttp.test_utils import TestServer

from app import repo
from app.config import Config, load_config
from app.context import CTX_KEY, AppContext
from app.core.analytics import Event, record
from app.core.clock import FakeClock
from app.core.models import PaymentStatus
from app.main import build_context, create_app
from app.web import export, pages
from tests.bot import Bot
from tests.site import full_game_with_waiting, pay_click, payment_of, site_client
from tools.fake_max import FakeMaxApi

PAGES = ("/", "/offer", "/privacy", "/consent", "/terms", "/contacts", "/pay/success?InvId=5", "/pay/fail")
MAX_PAGE_BYTES = 60 * 1024
URL = re.compile(r"""(?:https?:)?//[^\s"'<>)]+""")
ALLOWED_HOSTS = ("max.ru", "mc.yandex.ru")
BOT_LINK = "https://max.ru/santa_test_bot"


def external_urls(html: str, own_base: str) -> list[str]:
    """Absolute URLs in the page that point anywhere but max.ru, mc.yandex.ru or the site itself."""
    urls = [url for url in URL.findall(html) if not url.startswith(own_base)]
    return [url for url in urls if re.sub(r"^(?:https?:)?//", "", url).split("/")[0].split("?")[0]
            not in ALLOWED_HOSTS]


@pytest.fixture
async def metrica_ctx(env: dict[str, str], api: FakeMaxApi, clock: FakeClock) -> AsyncIterator[AppContext]:
    config = load_config({**env, "METRICA_ID": "12345678"}, announce=lambda _: None)
    context = await build_context(config, api=api, clock=clock)
    yield context
    await context.db.close()


# --- every page -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", PAGES)
async def test_page_is_small_self_contained_and_protected(ctx: AppContext, path: str) -> None:
    async with site_client(ctx) as client:
        response = await client.get(path)
        body = await response.read()
    assert response.status == 200 and response.content_type == "text/html"
    assert len(body) < MAX_PAGE_BYTES
    html = body.decode()
    assert external_urls(html, ctx.config.public_base_url) == []
    assert "mc.yandex.ru" not in html
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "mc.yandex.ru" not in csp and "unsafe-inline" not in csp
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in csp
    assert "<script" not in html and "style=" not in html
    assert "Иванов Иван Иванович" in html and "123456789012" in html


async def test_static_files_and_robots(ctx: AppContext) -> None:
    async with site_client(ctx) as client:
        css = await client.get("/static/site.css")
        assert css.status == 200 and len(await css.read()) < MAX_PAGE_BYTES
        assert "Content-Security-Policy" in css.headers
        robots = await client.get("/robots.txt")
        assert "Disallow: /pay/" in await robots.text()


async def test_metrica_is_allowed_only_when_configured(metrica_ctx: AppContext) -> None:
    async with site_client(metrica_ctx) as client:
        landing = await client.get("/")
        html = await landing.text()
        offer = await (await client.get("/offer")).text()
    assert 'src="/static/metrica.js" data-counter="12345678"' in html
    assert "https://mc.yandex.ru" in landing.headers["Content-Security-Policy"]
    assert "metrica.js" not in offer
    assert "Яндекс Метрика" in await site_client_get(metrica_ctx, "/privacy")


async def site_client_get(ctx: AppContext, path: str) -> str:
    async with site_client(ctx) as client:
        return await (await client.get(path)).text()


# --- the landing ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("query", "payload"), [
    ("", "s_site"),
    ("?utm_source=yd", "s_yd"),
    ("?src=VK-Ads_2026!", "s_vkads2026"),
    ("?utm_source=%3Cscript%3E", "s_script"),
    ("?utm_source=abcdefghijklmnopqrstuvwxyz", "s_abcdefghijklmnop"),
    ("?utm_source=%D0%BF%D1%80%D0%B8%D0%B2%D0%B5%D1%82", "s_site"),
])
async def test_open_bot_button_carries_the_sanitized_source(ctx: AppContext, query: str, payload: str) -> None:
    html = await site_client_get(ctx, "/" + query)
    assert f'href="{BOT_LINK}?start={payload}"' in html
    assert "<script" not in html


async def test_landing_shows_live_prices_and_the_copy(ctx: AppContext) -> None:
    await repo.set_setting(ctx.db, "price_S", 590)
    await repo.set_setting(ctx.db, "free_limit", 12)
    html = await site_client_get(ctx, "/")
    assert "Тайный Санта прямо в чате MAX — без сайтов, почты и регистрации" in html
    assert "590 ₽" in html and "2 490 ₽" in html and "До 12 участников" in html
    assert "Вы — Тайный Санта для: Мария К." in html
    for word in ("розыгрыш", "конкурс", "приз", "вишлист"):
        assert not re.search(rf"\b{word}", html, re.IGNORECASE), word


async def test_draw_counter_is_hidden_below_50_and_cached_for_10_minutes(ctx: AppContext, clock: FakeClock) -> None:
    async def draws(n: int, *, redraw: bool = False) -> None:
        for _ in range(n):
            await record(ctx.db, Event.DRAW_DONE, clock.now(), n=5, redraw=redraw)

    await draws(49)
    await draws(5, redraw=True)
    async with site_client(ctx) as client:
        assert "Уже проведено" not in await (await client.get("/")).text()
        await draws(3)
        assert "Уже проведено" not in await (await client.get("/")).text()
        clock.advance(pages.COUNTER_TTL)
        assert "Уже проведено 52 жеребьёвки" in await (await client.get("/")).text()


def test_russian_plural() -> None:
    forms = ("жеребьёвка", "жеребьёвки", "жеребьёвок")
    assert [pages.plural(n, *forms) for n in (1, 2, 5, 11, 21, 52, 112, 1000)] == [
        "жеребьёвка", "жеребьёвки", "жеребьёвок", "жеребьёвок", "жеребьёвка", "жеребьёвки", "жеребьёвок",
        "жеребьёвок"]


# --- legal pages ----------------------------------------------------------------------------------------------


async def test_legal_pages_carry_the_required_statements(ctx: AppContext) -> None:
    offer = await site_client_get(ctx, "/offer")
    assert "предоставление доступа к расширенным функциям программы (чат-бота) для организации обмена подарками" \
        in offer
    assert "14 дней" in offer and "490 ₽" in offer and "налог на профессиональный доход" in offer.lower()
    privacy = await site_client_get(ctx, "/privacy")
    assert "в Российской Федерации" in privacy and "180 дней" in privacy and "30 дней" in privacy
    assert "help@santa.example.ru" in privacy and "Яндекс Метрика" not in privacy
    consent = await site_client_get(ctx, "/consent")
    assert "статья 9" in consent and "2026-10" in consent and "отозвать согласие" in consent
    terms = await site_client_get(ctx, "/terms")
    assert "с 14 лет" in terms and "не является лотереей, конкурсом" in terms and "Пожаловаться" in terms


# --- payment pages --------------------------------------------------------------------------------------------


async def test_success_and_fail_pages_link_back_and_never_change_state(bot: Bot, clock: FakeClock) -> None:
    late = bot.person(250, "Опоздавший")
    game = await full_game_with_waiting(bot, clock, late)
    payment = await pay_click(bot, late, game)
    query = {"OutSum": "490.00", "InvId": str(payment.inv_id), "SignatureValue": "whatever", "Culture": "ru"}
    async with site_client(bot.ctx) as client:
        success = await (await client.get("/pay/success", params=query)).text()
        posted = await client.post("/pay/success", data=query)
        fail = await (await client.get("/pay/fail", params=query)).text()
        junk = await (await client.get("/pay/success", params={"InvId": "12; DROP"})).text()
    assert "Спасибо! Оплата получена — вернитесь в MAX" in success
    assert f'href="{BOT_LINK}?start=p_{payment.inv_id}"' in success and posted.status == 200
    assert "Оплата не прошла — попробуйте ещё раз" in fail and f"start=p_{payment.inv_id}" in fail
    assert f'href="{BOT_LINK}"' in junk and "start=p_" not in junk
    assert (await payment_of(bot.ctx, payment.inv_id)).status == PaymentStatus.CREATED
    assert (await bot.game(game.id)).participant_limit == 10


# --- /healthz and the site-only start ---------------------------------------------------------------------------


async def test_healthz_reports_state(ctx: AppContext, clock: FakeClock) -> None:
    ctx.runtime.last_update_at = clock.now()
    async with site_client(ctx) as client:
        response = await client.get("/healthz")
        assert response.status == 200
        assert await response.json() == {
            "ok": True, "db_ok": True, "outbox_pending": 0, "last_update_at": "2026-11-20T09:00:00.000+00:00",
            "webhook_registered": False, "bot_enabled": True, "payments_enabled": True,
        }


async def test_site_runs_without_bot_token_and_robokassa(env: dict[str, str], clock: FakeClock) -> None:
    site_only = load_config({**env, "MAX_BOT_TOKEN": "", "MAX_BOT_USERNAME": "", "ROBOKASSA_MERCHANT_LOGIN": "",
                             "OWNER_FULL_NAME": "", "OWNER_INN": "", "SUPPORT_EMAIL": ""},
                            announce=lambda _: None)
    api = FakeMaxApi(clock=clock)
    server = TestServer(create_app(site_only, api=api, clock=clock))
    await server.start_server()
    try:
        ctx = server.app[CTX_KEY]
        async with site_client(ctx) as client:
            for path in PAGES:
                response = await client.get(path)
                assert response.status == 200, path
            landing = await (await client.get("/")).text()
            assert "max.ru/" not in landing and "Бот проходит проверку в MAX" in landing
            assert "(не указано)" in landing
            health = await (await client.get("/healthz")).json()
            assert (health["bot_enabled"], health["payments_enabled"]) == (False, False)
            result = await client.post("/pay/robokassa/result", data={"OutSum": "1", "InvId": "1",
                                                                      "SignatureValue": "x"})
            assert result.status == 503
            hook = await client.post(f"/max/webhook/{site_only.webhook_path_secret}", json={},
                                     headers={"X-Max-Bot-Api-Secret": site_only.max_webhook_secret})
            assert hook.status == 404
    finally:
        await server.close()
    assert api.calls == []


# --- P1: CSV export ------------------------------------------------------------------------------------------------


async def test_csv_export_needs_the_token_and_never_contains_wishes(bot: Bot, clock: FakeClock, config: Config) -> None:
    late = bot.person(250, "Опоздавший")
    game = await full_game_with_waiting(bot, clock, late)
    await pay_click(bot, late, game)
    await repo.set_wishes(bot.ctx.db, game.id, 201, "Секретные носки")
    await repo.update_game(bot.ctx.db, game.id, title="=HYPERLINK(1)")
    token = config.admin_export_token
    async with site_client(bot.ctx) as client:
        assert (await client.get("/admin/export/games.csv")).status == 404
        assert (await client.get("/admin/export/games.csv", params={"token": "wrong"})).status == 404
        assert (await client.get("/admin/export/users.csv", params={"token": token})).status == 404
        tables = {}
        for table in sorted(export.TABLES):
            response = await client.get(f"/admin/export/{table}.csv", params={"token": token})
            assert response.status == 200 and response.content_type == "text/csv"
            assert "attachment" in response.headers["Content-Disposition"]
            tables[table] = (await response.read()).decode("utf-8-sig")
    assert tables["payments"].startswith("inv_id,game_id,payer_id,tier,amount_rub,status")
    assert ",490,created,robokassa," in tables["payments"]
    assert "'=HYPERLINK(1)" in tables["games"] and ",10,1," in tables["games"]
    assert "Секретные носки" not in "".join(tables.values())
    assert "draw_done" not in tables["events"] and "pay_click" in tables["events"]
