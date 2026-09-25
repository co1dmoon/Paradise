"""Robokassa payment link and request signature (§7)."""

from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from app.config import Config, load_config
from app.payments import robokassa

DESCRIPTION = "Расширение игры Тайный Санта до 30 участников"


def query_of(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query, keep_blank_values=True).items()}


def config_with(env: dict[str, str], **overrides: str) -> Config:
    return load_config({**env, **overrides}, announce=lambda _: None)


def test_signature_vector_md5_and_other_hashes() -> None:
    expected = hashlib.md5(b"shop:490.00:17:secret").hexdigest()
    assert robokassa.sign(["shop", "490.00", "17", "secret"], "md5") == expected
    assert robokassa.sign(["a", "b"], "sha256") == hashlib.sha256(b"a:b").hexdigest()
    assert robokassa.sign(["a", "b"], "sha512") == hashlib.sha512(b"a:b").hexdigest()
    assert robokassa.format_out_sum(2490) == "2490.00"


def test_test_mode_link(config: Config) -> None:
    url = robokassa.build_payment_url(config, inv_id=17, amount_rub=490, description=DESCRIPTION)
    assert url.startswith(robokassa.PAYMENT_URL + "?")
    assert "+" not in url  # spaces are %20, as the page expects
    query = query_of(url)
    assert query == {
        "MerchantLogin": "santa-shop",
        "OutSum": "490.00",
        "InvId": "17",
        "Description": DESCRIPTION,
        "SignatureValue": hashlib.md5(b"santa-shop:490.00:17:test-pass-1").hexdigest(),
        "Culture": "ru",
        "IsTest": "1",
    }


def test_live_mode_uses_password1_and_the_configured_hash(env: dict[str, str]) -> None:
    live = config_with(env, ROBOKASSA_TEST="0", ROBOKASSA_PASSWORD1="live-1", ROBOKASSA_PASSWORD2="live-2",
                       ROBOKASSA_HASH="sha256")
    query = query_of(robokassa.build_payment_url(live, inv_id=5, amount_rub=990, description=DESCRIPTION))
    assert "IsTest" not in query
    assert query["SignatureValue"] == hashlib.sha256(b"santa-shop:990.00:5:live-1").hexdigest()


def test_receipt_is_signed_url_encoded_and_sent_when_enabled(env: dict[str, str]) -> None:
    with_receipt = config_with(env, ROBOKASSA_SEND_RECEIPT="1")
    query = query_of(robokassa.build_payment_url(with_receipt, inv_id=8, amount_rub=490, description=DESCRIPTION))
    encoded = query["Receipt"]
    assert json.loads(unquote(encoded)) == {
        "items": [{"name": DESCRIPTION, "quantity": 1, "sum": 490, "tax": "none"}]
    }
    signed = f"santa-shop:490.00:8:{encoded}:test-pass-1".encode()
    assert query["SignatureValue"] == hashlib.md5(signed).hexdigest()


def test_description_must_fit(config: Config) -> None:
    with pytest.raises(ValueError):
        robokassa.build_payment_url(config, inv_id=1, amount_rub=490, description="x" * 101)
