"""PROMO_SPEC §10: config validation, gross/net money, the platform clients and the landing's source."""

from __future__ import annotations

import dataclasses
import re
from datetime import time
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import ConfigError, load_config
from app.context import AppContext
from app.core.clock import FakeClock
from app.main import build_context
from app.promo import store
from app.promo.autopilot import build_platforms
from app.promo.direct import SANDBOX_URL, DirectClient
from app.promo.platforms import Platform, gross_kop, kop_from_rub, minimum_gross_kop, net_kop
from app.promo.vkads import VkAdsClient
from app.tls import build_ssl_context
from tests.conftest import BASE_ENV
from tests.site import site_client
from tools.fake_max import FakeMaxApi


def promo_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {**BASE_ENV, "DATA_DIR": str(tmp_path), **extra}


def test_defaults_are_the_spec_values(tmp_path: Path) -> None:
    promo = load_config(promo_env(tmp_path), announce=lambda _: None).promo
    assert not promo.enabled and not promo.direct_configured and not promo.vk_configured
    assert (promo.cap_rub, promo.pause_cpa_rub, promo.scale_cpa_rub, promo.min_spend_rub, promo.max_raise_pct,
            promo.lag_days, promo.vat_pct) == (15000, 250, 150, 500, 30, 2, 22)
    assert (promo.season_start, promo.season_end, promo.report_time) == ("11-24", "12-12", time(10, 0))
    assert (promo.channel_id, promo.channel_ad_label, promo.direct_sandbox) == (None, "", False)


def test_malformed_values_stop_the_start_with_every_problem(tmp_path: Path) -> None:
    env = promo_env(
        tmp_path, PROMO_ENABLED="maybe", PROMO_CAP_RUB="15k", PROMO_MAX_RAISE_PCT="150", PROMO_LAG_DAYS="30",
        PROMO_START="1-24", PROMO_END="13-01", PROMO_VAT_PCT="99", PROMO_REPORT_TIME="25:00",
        PROMO_MAX_CHANNEL_ID="мой канал", VK_ADS_CLIENT_ID="123", PROMO_PAUSE_CPA_RUB="200",
        PROMO_SCALE_CPA_RUB="200", PROMO_CHANNEL_AD_LABEL="x" * 301, YANDEX_DIRECT_SANDBOX="да-нет",
    )
    with pytest.raises(ConfigError) as error:
        load_config(env, announce=lambda _: None)
    text = str(error.value)
    for fragment in ("PROMO_ENABLED", "PROMO_CAP_RUB", "PROMO_MAX_RAISE_PCT", "PROMO_LAG_DAYS", "PROMO_START",
                     "PROMO_END", "PROMO_VAT_PCT", "PROMO_REPORT_TIME", "PROMO_MAX_CHANNEL_ID", "VK_ADS_CLIENT_SECRET",
                     "PROMO_SCALE_CPA_RUB", "PROMO_CHANNEL_AD_LABEL", "YANDEX_DIRECT_SANDBOX"):
        assert fragment in text, fragment


def test_missing_credentials_only_switch_a_platform_off(tmp_path: Path) -> None:
    config = load_config(promo_env(tmp_path, PROMO_ENABLED="1", PROMO_MAX_CHANNEL_ID="-71234567"),
                         announce=lambda _: None)
    assert config.promo.enabled and config.promo.channel_id == -71234567
    assert any("VK_ADS_CLIENT_ID" in warning for warning in config.warnings)


async def test_clients_are_built_for_configured_platforms(tmp_path: Path, ctx: AppContext, clock: FakeClock) -> None:
    ssl_context = build_ssl_context()
    enabled = load_config(promo_env(tmp_path, PROMO_ENABLED="1", YANDEX_DIRECT_TOKEN="y0_token",
                                    YANDEX_DIRECT_SANDBOX="1", VK_ADS_CLIENT_ID="1", VK_ADS_CLIENT_SECRET="s"),
                          announce=lambda _: None)
    platforms = build_platforms(enabled, ctx.db, clock, ssl_context)
    assert isinstance(platforms[Platform.DIRECT], DirectClient) and isinstance(platforms[Platform.VK], VkAdsClient)
    assert platforms[Platform.DIRECT]._base == SANDBOX_URL  # type: ignore[attr-defined]
    for client in platforms.values():
        await client.close()
    disabled = dataclasses.replace(enabled, promo=dataclasses.replace(enabled.promo, enabled=False))
    assert build_platforms(disabled, ctx.db, clock, ssl_context) == {}


async def test_settings_are_seeded_once_and_the_database_wins(tmp_path: Path, clock: FakeClock) -> None:
    first = load_config(promo_env(tmp_path, PROMO_ENABLED="1", PROMO_CAP_RUB="6000"), announce=lambda _: None)
    ctx = await build_context(first, api=FakeMaxApi(clock=clock), clock=clock)
    settings = await store.get_settings(ctx.db)
    assert (settings.auto, settings.cap_rub, settings.channel_on) == (False, 6000, False), "test mode by default"
    await store.set_settings(ctx.db, cap_rub=9000)
    await ctx.db.close()
    again = load_config(promo_env(tmp_path, PROMO_ENABLED="1", PROMO_CAP_RUB="12000"), announce=lambda _: None)
    ctx = await build_context(again, api=FakeMaxApi(clock=clock), clock=clock)
    assert (await store.get_settings(ctx.db)).cap_rub == 9000
    await ctx.db.close()


def test_money_is_gross_integer_kopecks() -> None:
    assert kop_from_rub("1496.50") == 149650 and kop_from_rub("0,5") == 50 and kop_from_rub(Decimal("4.005")) == 401
    assert gross_kop(300_00, 22) == 366_00 and minimum_gross_kop(22) == 366_00
    assert gross_kop(4918_03, 22) == 6000_00, "VK: paying 6 000 ₽ puts 4 918,03 ₽ on the balance"
    assert net_kop(470_00, 22) == 385_24 and gross_kop(net_kop(470_00, 22), 22) <= 470_00
    for bad in ("abc", "NaN", ""):
        with pytest.raises(ValueError):
            kop_from_rub(bad)


async def test_the_landing_prefers_the_explicit_source(ctx: AppContext) -> None:
    async with site_client(ctx) as client:
        html = await (await client.get("/?utm_source=vk_ads&src=vk55")).text()
    assert "start=s_vk55" in html, "platform UTM tags must not replace our tracking source"


def test_every_promo_variable_is_documented_in_env_example() -> None:
    root = Path(__file__).resolve().parent.parent
    config_source = (root / "app" / "config.py").read_text(encoding="utf-8")
    names = set(re.findall(r'"((?:PROMO|YANDEX_DIRECT|VK_ADS)_[A-Z_]+)"', config_source))
    example = (root / ".env.example").read_text(encoding="utf-8")
    assert len(names) == 17
    assert [name for name in sorted(names) if f"\n{name}=" not in example] == []
