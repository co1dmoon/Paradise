from __future__ import annotations

from pathlib import Path

import pytest

from app.config import SECRETS_FILE, ConfigError, load_config


def minimal_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"DOMAIN": "santa.example.ru", "DATA_DIR": str(tmp_path), **extra}


def test_site_only_config_starts_with_warnings(tmp_path: Path) -> None:
    config = load_config(minimal_env(tmp_path), announce=lambda _: None)
    assert config.public_base_url == "https://santa.example.ru"
    assert not config.bot_enabled and not config.payments_enabled
    assert any("MAX_BOT_TOKEN" in warning for warning in config.warnings)
    assert config.default_settings.free_limit == 10 and config.default_settings.price_L == 2490
    assert config.db_path == tmp_path / "santa.db"
    assert config.tz.key == "Europe/Moscow"


def test_full_config(config) -> None:
    assert config.bot_enabled and config.payments_enabled
    assert config.robokassa_test and config.robokassa_passwords == ("test-pass-1", "test-pass-2")
    assert config.admin_user_ids == (9000,)
    assert config.webhook_url == "https://santa.example.ru/max/webhook/pathsecret0123456789abcd"
    assert config.max_api_base == "https://platform-api2.max.ru"


def test_missing_secrets_are_generated_once_and_reused(tmp_path: Path) -> None:
    announced: list[str] = []
    first = load_config(minimal_env(tmp_path), announce=announced.append)
    assert len(announced) == 1 and "MAX_WEBHOOK_SECRET" in announced[0]
    assert len(first.max_webhook_secret) == 32 and len(first.webhook_path_secret) == 24
    assert (tmp_path / SECRETS_FILE).stat().st_mode & 0o777 == 0o600
    second = load_config(minimal_env(tmp_path), announce=announced.append)
    assert len(announced) == 1, "secrets are printed only when generated"
    assert second.webhook_path_secret == first.webhook_path_secret
    third = load_config(minimal_env(tmp_path, WEBHOOK_PATH_SECRET="my-own-path-secret-123"), announce=announced.append)
    assert third.webhook_path_secret == "my-own-path-secret-123"


def test_all_problems_are_reported_in_russian(tmp_path: Path) -> None:
    env = minimal_env(
        tmp_path,
        MAX_BOT_TOKEN="t",
        MODE="longpoll",
        MAX_API_BASE="https://platform-api.max.ru",
        ROBOKASSA_MERCHANT_LOGIN="shop",
        ROBOKASSA_HASH="sha1",
        ROBOKASSA_TEST="maybe",
        ADMIN_USER_IDS="12,abc",
        OWNER_INN="123",
        SUPPORT_EMAIL="nope",
        PRICE_M="100",
        TZ="Mars/Olympus",
        DIGEST_FROM="1-11",
        S3_BUCKET="b",
    )
    with pytest.raises(ConfigError) as error:
        load_config(env, announce=lambda _: None)
    text = str(error.value)
    for fragment in ("MAX_BOT_USERNAME", "MODE", "platform-api2", "ROBOKASSA_TEST_PASSWORD1", "ROBOKASSA_HASH",
                     "ROBOKASSA_TEST", "ADMIN_USER_IDS", "OWNER_INN", "SUPPORT_EMAIL", "Цена тарифа M", "TZ",
                     "DIGEST_FROM", "S3_ENDPOINT", "OWNER_FULL_NAME"):
        assert fragment in text, fragment


def test_https_required_except_localhost(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(minimal_env(tmp_path, PUBLIC_BASE_URL="http://santa.example.ru"), announce=lambda _: None)
    local = load_config(
        {"PUBLIC_BASE_URL": "http://localhost:8080", "DATA_DIR": str(tmp_path), "MODE": "polling"},
        announce=lambda _: None,
    )
    assert local.public_base_url == "http://localhost:8080"


def test_username_normalized(tmp_path: Path) -> None:
    env = minimal_env(tmp_path, MAX_BOT_TOKEN="t", MAX_BOT_USERNAME="https://max.ru/se1234567_bot",
                      OWNER_FULL_NAME="Иванов И.И.", OWNER_INN="123456789012", SUPPORT_EMAIL="a@b.ru")
    assert load_config(env, announce=lambda _: None).max_bot_username == "se1234567_bot"


def test_live_mode_needs_live_passwords(tmp_path: Path) -> None:
    env = minimal_env(tmp_path, ROBOKASSA_MERCHANT_LOGIN="shop", ROBOKASSA_TEST="0", OWNER_FULL_NAME="И",
                      OWNER_INN="123456789012", SUPPORT_EMAIL="a@b.ru", ROBOKASSA_PASSWORD1="p1")
    with pytest.raises(ConfigError, match="ROBOKASSA_PASSWORD2"):
        load_config(env, announce=lambda _: None)
    config = load_config({**env, "ROBOKASSA_PASSWORD2": "p2"}, announce=lambda _: None)
    assert config.robokassa_passwords == ("p1", "p2")
