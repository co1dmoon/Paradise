"""MAX webhook endpoint (§8) and end-to-end scenario 7 (§14): a wrong secret gets 404, nothing is processed."""

from __future__ import annotations

from typing import Any

from app.config import Config
from app.context import AppContext
from app.core import texts
from app.web.routes import WEBHOOK_SECRET_HEADER
from tests.site import site_client
from tools import fake_max
from tools.fake_max import FakeMaxApi


async def processed(ctx: AppContext) -> int:
    return int(await ctx.db.fetchval("SELECT COUNT(*) FROM processed_updates"))


def headers(config: Config, secret: str | None = None) -> dict[str, str]:
    return {WEBHOOK_SECRET_HEADER: config.max_webhook_secret if secret is None else secret}


async def test_wrong_secrets_get_404_and_nothing_is_processed(ctx: AppContext, api: FakeMaxApi, config: Config) -> None:
    update = fake_max.bot_started(501)
    good_path = config.webhook_path
    attempts: list[tuple[str, dict[str, str]]] = [
        (good_path, headers(config, "wrong-secret-0123456789abcdefghij")),
        (good_path, headers(config, config.max_webhook_secret[:-1])),
        (good_path, {}),
        ("/max/webhook/wrongpath0123456789abcd", headers(config)),
        (good_path[:-1], headers(config)),
    ]
    async with site_client(ctx) as client:
        for path, sent_headers in attempts:
            response = await client.post(path, json=update, headers=sent_headers)
            assert response.status == 404, (path, sent_headers)
        assert (await client.get(good_path, headers=headers(config))).status == 405
    await ctx.wait_background()
    assert await processed(ctx) == 0 and api.calls == [] and ctx.runtime.last_update_at is None


async def test_single_update_is_answered_at_once_and_processed(ctx: AppContext, api: FakeMaxApi,
                                                                config: Config) -> None:
    async with site_client(ctx) as client:
        response = await client.post(config.webhook_path, json=fake_max.bot_started(501, name="Ольга"),
                                     headers=headers(config))
        assert response.status == 200 and await response.json() == {"ok": True}
    await ctx.wait_background()
    assert api.last_text(501) == texts.consent(config.public_base_url)


async def test_batch_is_processed_in_order_and_retries_are_deduplicated(
    ctx: AppContext, api: FakeMaxApi, config: Config
) -> None:
    start = fake_max.bot_started(501, name="Ольга")
    batch: dict[str, Any] = {"updates": [start, fake_max.message_created(501, "/whoami"), "мусор", 42]}
    async with site_client(ctx) as client:
        for _ in range(2):
            response = await client.post(config.webhook_path, json=batch, headers=headers(config))
            assert response.status == 200
            await ctx.wait_background()
    assert api.texts_to(501) == [texts.consent(config.public_base_url), texts.whoami(501)]
    assert await processed(ctx) == 2


async def test_invalid_json_is_a_bad_request(ctx: AppContext, config: Config) -> None:
    async with site_client(ctx) as client:
        response = await client.post(config.webhook_path, data=b"{not json", headers=headers(config))
        assert response.status == 400
