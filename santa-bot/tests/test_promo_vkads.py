"""PROMO_SPEC §3.2: the VK Ads client — token lifecycle, request bodies, statistics and limits."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import date, timedelta

import pytest

from app.core.clock import FakeClock
from app.db import Database
from app.promo.platforms import (
    AuthError,
    Budget,
    BudgetKind,
    CampaignInfo,
    CampaignState,
    Platform,
    PlatformError,
    RateLimited,
    SpendRow,
    Transient,
)
from app.promo.store import DbTokenStore
from app.promo.vkads import VkAdsClient
from tests.ads_server import Reply, ScriptedServer, scripted_server

SECRET = "vk-client-secret-XYZ"
TOKEN = "POST /api/v2/oauth2/token.json"
DELETE = "POST /api/v2/oauth2/token/delete.json"
PLANS = "GET /api/v2/ad_plans.json"
MASS = "POST /api/v2/ad_plans/mass_action.json"
STATS = "GET /api/v2/statistics/ad_plans/day.json"
EMPTY_PLANS = Reply(body={"count": 0, "offset": 0, "items": []})


def token_reply(access: str, refresh: str = "refresh-1", expires_in: object = "86400") -> Reply:
    return Reply(body={"access_token": access, "token_type": "bearer", "scope": "", "expires_in": expires_in,
                       "refresh_token": refresh})


@pytest.fixture
async def server() -> AsyncIterator[ScriptedServer]:
    async with scripted_server() as scripted:
        yield scripted


@pytest.fixture
def tokens(db: Database) -> DbTokenStore:
    return DbTokenStore(db, Platform.VK)


@pytest.fixture
async def vk(server: ScriptedServer, tokens: DbTokenStore, clock: FakeClock) -> AsyncIterator[VkAdsClient]:
    client = VkAdsClient("vk-client-1", SECRET, tokens, vat_pct=22, clock=clock, base_url=server.url("/api/"))
    yield client
    await client.close()


async def test_a_token_is_issued_once_stored_and_reused(server: ScriptedServer, vk: VkAdsClient,
                                                         tokens: DbTokenStore, clock: FakeClock) -> None:
    server.reply(TOKEN, token_reply("access-1"))
    server.reply(PLANS, EMPTY_PLANS, EMPTY_PLANS)

    await vk.list_campaigns(["11"])
    await vk.list_campaigns(["11"])

    assert server.routes() == [TOKEN, PLANS, PLANS]
    assert server.requests[0].form == {"grant_type": "client_credentials", "client_id": "vk-client-1",
                                       "client_secret": SECRET}
    assert all(seen.headers["Authorization"] == "Bearer access-1" for seen in server.requests[1:])
    stored = await tokens.load()
    assert stored is not None and (stored.access_token, stored.refresh_token) == ("access-1", "refresh-1")
    assert stored.expires_at == clock.now() + timedelta(seconds=86400)


async def test_a_token_close_to_expiry_is_refreshed(server: ScriptedServer, vk: VkAdsClient,
                                                     tokens: DbTokenStore, clock: FakeClock) -> None:
    server.reply(TOKEN, token_reply("access-1", expires_in=86400), token_reply("access-2", "refresh-2"))
    server.reply(PLANS, EMPTY_PLANS, EMPTY_PLANS)
    await vk.list_campaigns(["11"])
    clock.advance(86400 - 300)

    await vk.list_campaigns(["11"])

    assert server.requests[2].form == {"grant_type": "refresh_token", "refresh_token": "refresh-1",
                                       "client_id": "vk-client-1", "client_secret": SECRET}
    assert server.requests[3].headers["Authorization"] == "Bearer access-2"
    stored = await tokens.load()
    assert stored is not None and stored.refresh_token == "refresh-2"


async def test_a_refused_refresh_falls_back_to_a_new_token(server: ScriptedServer, vk: VkAdsClient,
                                                           clock: FakeClock) -> None:
    server.reply(TOKEN, token_reply("access-1"), Reply(400, {"error": "invalid_grant"}), token_reply("access-2"))
    server.reply(PLANS, EMPTY_PLANS, EMPTY_PLANS)
    await vk.list_campaigns(["11"])
    clock.advance(86400)

    await vk.list_campaigns(["11"])

    assert [seen.form.get("grant_type") for seen in server.requests if seen.route == TOKEN] == [
        "client_credentials", "refresh_token", "client_credentials"]
    assert server.requests[-1].headers["Authorization"] == "Bearer access-2"


async def test_the_token_limit_deletes_old_tokens_once_and_retries(server: ScriptedServer, vk: VkAdsClient) -> None:
    server.reply(TOKEN, Reply(403, {"error": "token_limit_exceeded"}), token_reply("access-9"))
    server.reply(DELETE, Reply(200, {}))
    server.reply(PLANS, EMPTY_PLANS)

    await vk.list_campaigns(["11"])

    assert server.routes() == [TOKEN, DELETE, TOKEN, PLANS]
    assert server.requests[1].form == {"client_id": "vk-client-1", "client_secret": SECRET}


async def test_a_persistent_token_limit_is_an_auth_error(server: ScriptedServer, vk: VkAdsClient) -> None:
    server.reply(TOKEN, Reply(403, {}), Reply(403, {}))
    server.reply(DELETE, Reply(200, {}))
    with pytest.raises(AuthError) as error:
        await vk.list_campaigns(["11"])
    assert error.value.code == "token_limit"
    assert server.routes() == [TOKEN, DELETE, TOKEN], "the deletion is tried exactly once"


@pytest.mark.parametrize(("code", "then"), [
    ("expired_token", "refresh_token"), ("invalid_token", "client_credentials"),
])
async def test_a_rejected_token_is_renewed_and_the_call_repeated(server: ScriptedServer, vk: VkAdsClient,
                                                                  code: str, then: str) -> None:
    server.reply(TOKEN, token_reply("access-1"), token_reply("access-2"))
    server.reply(PLANS, Reply(401, {"code": code, "message": "..."}), EMPTY_PLANS)

    assert await vk.list_campaigns(["11"]) == []

    assert server.routes() == [TOKEN, PLANS, TOKEN, PLANS]
    assert server.requests[2].form["grant_type"] == then
    assert server.requests[3].headers["Authorization"] == "Bearer access-2"


@pytest.mark.parametrize("code", ["invalid_client", "invalid_user", "revoked_token"])
async def test_account_problems_are_auth_errors(server: ScriptedServer, vk: VkAdsClient, code: str) -> None:
    server.reply(TOKEN, token_reply("access-1"))
    server.reply(PLANS, Reply(401, {"code": code, "message": "no"}))
    with pytest.raises(AuthError) as error:
        await vk.list_campaigns(["11"])
    assert error.value.code == code


async def test_wrong_client_credentials(server: ScriptedServer, vk: VkAdsClient) -> None:
    server.reply(TOKEN, Reply(401, {"error": "invalid_client", "error_description": "Invalid client id or secret"}))
    with pytest.raises(AuthError) as error:
        await vk.list_campaigns(["11"])
    assert error.value.code == "invalid_client"


async def test_campaigns_are_read_with_their_daily_budgets(server: ScriptedServer, vk: VkAdsClient) -> None:
    server.reply(TOKEN, token_reply("access-1"))
    server.reply(PLANS, Reply(body={"count": 4, "offset": 0, "items": [
        {"id": 11, "name": "Сайт", "status": "active", "budget_limit_day": "300.00", "budget_limit": "6000"},
        {"id": 12, "name": "Сайт 2", "status": "blocked", "budget_limit_day": None, "budget_limit": "5000"},
        {"id": 13, "name": "Удалена", "status": "deleted", "budget_limit_day": "0"},
        {"id": 14, "name": "Странная", "status": "active", "budget_limit_day": "NaN"},
    ]}))

    campaigns = await vk.list_campaigns(["11", "12", "13", "14"])

    assert server.requests[1].query == {"_id__in": "11,12,13,14",
                                        "fields": "id,name,status,budget_limit_day,budget_limit", "limit": "50"}
    assert campaigns == [
        CampaignInfo("11", "Сайт", CampaignState.ACTIVE, Budget(366_00, BudgetKind.DAY)),
        CampaignInfo("12", "Сайт 2", CampaignState.PAUSED, None),
        CampaignInfo("13", "Удалена", CampaignState.OTHER, None),
        CampaignInfo("14", "Странная", CampaignState.ACTIVE, None),
    ]


async def test_mass_action_bodies(server: ScriptedServer, vk: VkAdsClient) -> None:
    server.reply(TOKEN, token_reply("access-1"))
    server.reply(MASS, Reply(204), Reply(204), Reply(204))

    await vk.suspend("11")
    await vk.resume("11")
    await vk.set_budget("11", 470_00, BudgetKind.DAY)

    assert [seen.json for seen in server.requests if seen.route == MASS] == [
        [{"id": 11, "status": "blocked"}], [{"id": 11, "status": "active"}], [{"id": 11, "budget_limit_day": "385.24"}],
    ]
    with pytest.raises(PlatformError):
        await vk.set_budget("11", 470_00, BudgetKind.WEEK)


async def test_statistics_are_net_and_become_gross(server: ScriptedServer, vk: VkAdsClient) -> None:
    server.reply(TOKEN, token_reply("access-1"))
    server.reply(STATS, Reply(body={"items": [
        {"id": 11, "rows": [{"date": "2026-11-28", "base": {"shows": 900, "clicks": 12, "spent": "100.00"}},
                            {"date": "2026-11-29", "base": {"shows": 0, "clicks": 0, "spent": "0"}}],
         "total": {"base": {"spent": "100.00"}}},
    ], "total": {}}))

    spend = await vk.daily_spend(["11"], date(2026, 11, 28), date(2026, 11, 29))

    assert server.requests[1].query == {"id": "11", "date_from": "2026-11-28", "date_to": "2026-11-29",
                                        "metrics": "base"}
    assert spend == {("11", date(2026, 11, 28)): SpendRow(122_00, 12), ("11", date(2026, 11, 29)): SpendRow(0, 0)}


async def test_rate_limit_headers_are_respected(server: ScriptedServer, vk: VkAdsClient, clock: FakeClock) -> None:
    server.reply(TOKEN, token_reply("access-1", expires_in=10 * 86400))
    server.reply(PLANS, Reply(body={"items": []}, headers={"X-RateLimit-RPS-Remaining": "0"}),
                 Reply(body={"items": []}, headers={"X-RateLimit-Hourly-Remaining": "0"}),
                 Reply(body={"items": []}), Reply(429, {"error": {"code": "throttled"}}))
    started = clock.monotonic()
    await vk.list_campaigns(["11"])
    await vk.list_campaigns(["11"])
    assert clock.monotonic() - started == 1, "an exhausted second is waited out"

    with pytest.raises(RateLimited):
        await vk.list_campaigns(["11"])
    assert server.routes().count(PLANS) == 2, "an exhausted hour stops calls without asking"
    clock.advance(3600)
    await vk.list_campaigns(["11"])
    with pytest.raises(RateLimited):
        await vk.list_campaigns(["11"])


@pytest.mark.parametrize(("reply", "error"), [
    (Reply(400, {"error": {"code": "unknown_ad_plans", "message": "Unknown ad_plans: 99"}}), PlatformError),
    (Reply(403, {"error": {"code": "ERR_ACCESS_DENIED", "message": "Нет прав"}}), AuthError),
    (Reply(500, "oops"), Transient),
])
async def test_errors(server: ScriptedServer, vk: VkAdsClient, reply: Reply, error: type[Exception]) -> None:
    server.reply(TOKEN, token_reply("access-1"))
    server.reply(MASS, reply)
    with pytest.raises(error):
        await vk.suspend("99")


async def test_tokens_are_never_logged(server: ScriptedServer, vk: VkAdsClient,
                                       caplog: pytest.LogCaptureFixture) -> None:
    server.reply(TOKEN, token_reply("access-SECRET-1", "refresh-SECRET-1"), Reply(400, {"error": "invalid_grant"}),
                 token_reply("access-SECRET-2"))
    server.reply(PLANS, Reply(401, {"code": "expired_token"}), EMPTY_PLANS)
    with caplog.at_level(logging.DEBUG):
        await vk.list_campaigns(["11"])
    ours = [record for record in caplog.records if record.name.startswith("app.")]  # the app logs at INFO and up
    text = " ".join(f"{record.getMessage()} {record.__dict__}" for record in ours)
    assert "vk ads token received" in text
    assert "SECRET" not in text
