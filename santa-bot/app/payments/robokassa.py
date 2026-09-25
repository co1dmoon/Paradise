"""Robokassa payment URL and signatures (§7).

Checked against docs.robokassa.ru (pay-interface, fiscalization) on 2026-09-24:

- The payment page is https://auth.robokassa.ru/Merchant/Index.aspx (GET or POST).
- The request signature is hash('MerchantLogin:OutSum:InvId:Password#1'), with the
  URL-encoded Receipt JSON inserted before the password when a receipt is sent:
  'MerchantLogin:OutSum:InvId:Receipt:Password#1'. No Shp_ parameters: InvId alone
  identifies our payment row.
- The hash is md5 by default; sha256 and sha512 are also supported (ROBOKASSA_HASH).
  Signatures are lowercase hex.

Unverified: the docs recommend POST for Receipt and do not describe it in a GET
link. We sign the URL-encoded JSON (as the docs say) and put that encoded string
into the query, where it is encoded once more; Robokassa decodes the query once
and sees exactly the string we signed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from urllib.parse import quote, urlencode

from app.config import Config

PAYMENT_URL = "https://auth.robokassa.ru/Merchant/Index.aspx"
MAX_DESCRIPTION = 100


def format_out_sum(amount_rub: int) -> str:
    """OutSum as we send it: whole rubles with two decimals, e.g. '490.00'."""
    return f"{amount_rub}.00"


def sign(parts: Sequence[str], algorithm: str) -> str:
    """Lowercase hex hash of the parts joined with ':' (md5, sha256 or sha512)."""
    return hashlib.new(algorithm, ":".join(parts).encode("utf-8")).hexdigest()


def receipt_json(description: str, amount_rub: int) -> str:
    """The fiscal receipt: one service item without VAT (НПД), as minified JSON."""
    item = {"name": description, "quantity": 1, "sum": amount_rub, "tax": "none"}
    return json.dumps({"items": [item]}, ensure_ascii=False, separators=(",", ":"))


def request_signature(config: Config, out_sum: str, inv_id: int, receipt: str | None = None) -> str:
    """SignatureValue for the payment link; ``receipt`` is the already URL-encoded Receipt."""
    password1, _ = config.robokassa_passwords
    parts = [config.robokassa_merchant_login, out_sum, str(inv_id)]
    if receipt is not None:
        parts.append(receipt)
    return sign([*parts, password1], config.robokassa_hash)


def build_payment_url(config: Config, *, inv_id: int, amount_rub: int, description: str) -> str:
    """The Robokassa payment page for one payment row (test mode adds IsTest=1)."""
    if not description or len(description) > MAX_DESCRIPTION:
        raise ValueError(f"description must be 1-{MAX_DESCRIPTION} characters")
    out_sum = format_out_sum(amount_rub)
    receipt = quote(receipt_json(description, amount_rub), safe="") if config.robokassa_send_receipt else None
    params = {
        "MerchantLogin": config.robokassa_merchant_login,
        "OutSum": out_sum,
        "InvId": str(inv_id),
        "Description": description,
        "SignatureValue": request_signature(config, out_sum, inv_id, receipt),
        "Culture": "ru",
    }
    if receipt is not None:
        params["Receipt"] = receipt
    if config.robokassa_test:
        params["IsTest"] = "1"
    return f"{PAYMENT_URL}?{urlencode(params, quote_via=quote)}"
