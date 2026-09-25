from __future__ import annotations

import random

import pytest

from app.core.payloads import (
    CODE_ALPHABET,
    GroupPayload,
    JoinPayload,
    NewGamePayload,
    OrganizerPayload,
    PaymentReturnPayload,
    SourcePayload,
    deep_link,
    extract_code,
    first_source,
    generate_code,
    group_payload,
    join_payload,
    parse_start_payload,
    sanitize_source,
    source_payload,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("j_ABC234", JoinPayload("ABC234")),
        ("j_abc234", JoinPayload("ABC234")),
        ("n_XYZ789", NewGamePayload("XYZ789")),
        ("o_QWE234", OrganizerPayload("QWE234")),
        ("s_yd", SourcePayload("yd")),
        ("s_VK", SourcePayload("vk")),
        ("s_yandex-direct", SourcePayload("yandexdirect")),
        ("p_42", PaymentReturnPayload(42)),
        ("gc_123456", GroupPayload(123456)),
        ("gcm987", GroupPayload(-987)),
    ],
)
def test_valid_payloads(raw: str, expected: object) -> None:
    assert parse_start_payload(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [None, "", "hello", "j_", "j_ABC", "j_ABC2345", "j_АБВГДЕ", "p_", "p_12a", "gc_", "gc_x1", "gcm", "s_",
     "s_!!!", "x_ABC234", "j_" + "A" * 200],
)
def test_anything_else_is_an_empty_payload(raw: str | None) -> None:
    assert parse_start_payload(raw) is None


def test_group_payload_round_trip() -> None:
    for chat_id in (123, -456):
        assert parse_start_payload(group_payload(chat_id)) == GroupPayload(chat_id)


def test_long_source_is_truncated_to_16() -> None:
    assert parse_start_payload("s_" + "a" * 30) == SourcePayload("a" * 16)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("yd", "yd"), ("VK", "vk"), ("  Telegram-Ads 2026 ", "telegramads2026"), ("", "site"), (None, "site"),
     ("<script>", "script"), ("---", "site"), ("a" * 40, "a" * 16)],
)
def test_sanitize_source(raw: str | None, expected: str) -> None:
    assert sanitize_source(raw) == expected


def test_source_payload_is_sanitized() -> None:
    assert source_payload("Yandex Direct!") == "s_yandexdirect"


@pytest.mark.parametrize(
    ("text", "code"),
    [("abc234", "ABC234"), ("  ABC234 ", "ABC234"), ("код abc234", "ABC234"), ("Код  QWE789", "QWE789"),
     ("КОД xyz234", "XYZ234"), ("hello!", None), ("abc2345", None), ("код", None), ("мой код abc234", None)],
)
def test_extract_code_from_text(text: str, code: str | None) -> None:
    assert extract_code(text) == code


def test_first_source_tags() -> None:
    assert first_source(JoinPayload("ABC234")) == "j:ABC234"
    assert first_source(NewGamePayload("ABC234")) == "n:ABC234"
    assert first_source(SourcePayload("yd")) == "s:yd"
    assert first_source(PaymentReturnPayload(1)) == "direct"
    assert first_source(None) == "direct"


def test_generated_codes_use_the_alphabet() -> None:
    rng = random.Random(7)
    codes = {generate_code(rng) for _ in range(200)}
    assert all(len(code) == 6 and set(code) <= set(CODE_ALPHABET) for code in codes)
    assert not set("IO01") & set(CODE_ALPHABET)


def test_deep_link() -> None:
    assert deep_link("se1234567_bot", join_payload("ABC234")) == "https://max.ru/se1234567_bot?start=j_ABC234"
    assert deep_link("se1234567_bot") == "https://max.ru/se1234567_bot"
    with pytest.raises(ValueError):
        deep_link("bot", "x" * 129)
