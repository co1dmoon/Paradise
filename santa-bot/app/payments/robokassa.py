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

ResultURL (docs.robokassa.ru/ru/notifications-and-redirects, checked 2026-09-25):
Robokassa sends OutSum, InvId, SignatureValue (plus Fee, EMail, PaymentMethod,
IncCurrLabel, IsTest and any Shp_ parameters) by GET or POST, as set in the cabinet.
The signature is hash('OutSum:InvId:Password#2[:Shp_a=1:Shp_b=2…]') over the values
exactly as received: OutSum has two decimals in test mode and six in live mode
('490.000000'). The shop must answer 'OK{InvId}', otherwise Robokassa retries.

``process_result`` verifies the notice, then marks the payment paid, applies the
tier, records the event and queues every notification in ONE transaction, so a
confirmation is applied and announced exactly once, however often it is replayed.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlencode

from app import repo
from app.config import Config
from app.context import AppContext
from app.core import billing, texts
from app.core.models import PaymentStatus
from app.handlers import notices

log = logging.getLogger(__name__)

PAYMENT_URL = "https://auth.robokassa.ru/Merchant/Index.aspx"
MAX_DESCRIPTION = 100
BAD_SIGNATURE_ALERT_INTERVAL = 10 * 60.0
_INV_ID = re.compile(r"^[1-9][0-9]{0,17}$")
_KEPT_FIELDS = ("OutSum", "InvId", "Fee", "PaymentMethod", "IncCurrLabel", "IsTest")


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


# --- ResultURL ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResultReply:
    """What the ResultURL route answers: HTTP status and a text/plain body."""

    status: int
    text: str


@dataclass(frozen=True, slots=True)
class ResultNotice:
    """A payment notification with OutSum and InvId kept exactly as received (they are signed)."""

    out_sum: str
    inv_id_text: str
    signature: str
    shp: tuple[tuple[str, str], ...]
    amount: Decimal

    @property
    def inv_id(self) -> int:
        return int(self.inv_id_text)

    @classmethod
    def parse(cls, params: Mapping[str, str]) -> ResultNotice | None:
        """None when a field is missing or malformed (nothing to verify)."""
        out_sum, inv_id, signature = (params.get(name, "") for name in ("OutSum", "InvId", "SignatureValue"))
        if not (_INV_ID.match(inv_id) and signature):
            return None
        try:
            amount = Decimal(out_sum)
        except InvalidOperation:
            return None
        if not amount.is_finite():
            return None
        shp = tuple(sorted((key, value) for key, value in params.items() if key.startswith("Shp_")))
        return cls(out_sum, inv_id, signature, shp, amount)


def result_signature(config: Config, notice: ResultNotice) -> str:
    """hash('OutSum:InvId:Password#2[:Shp_…=…]') over the received strings (test password in test mode)."""
    _, password2 = config.robokassa_passwords
    parts = [notice.out_sum, notice.inv_id_text, password2, *(f"{key}={value}" for key, value in notice.shp)]
    return sign(parts, config.robokassa_hash)


def signature_matches(expected: str, received: str) -> bool:
    """Case-insensitive, constant-time comparison (Robokassa may send upper-case hex)."""
    return hmac.compare_digest(expected.lower().encode(), received.strip().lower().encode())


def ok_reply(inv_id: int) -> ResultReply:
    return ResultReply(200, f"OK{inv_id}")


async def process_result(ctx: AppContext, params: Mapping[str, str]) -> ResultReply:
    """Handle one ResultURL notification (§7): verify, apply once, answer 'OK{InvId}'."""
    config = ctx.config
    if not config.payments_enabled:
        log.warning("robokassa notice ignored: payments are disabled")
        return ResultReply(503, "payments disabled")
    notice = ResultNotice.parse(params)
    if notice is None:
        log.warning("robokassa notice without valid OutSum, InvId or SignatureValue")
        return ResultReply(400, "bad request")
    if not signature_matches(result_signature(config, notice), notice.signature):
        log.warning("robokassa notice with a bad signature", extra={"inv_id": notice.inv_id})
        await ctx.alerts.alert("robokassa:signature", texts.bad_signature_alert(notice.inv_id_text),
                               min_interval=BAD_SIGNATURE_ALERT_INTERVAL)
        return ResultReply(400, "bad signature")
    payment = await repo.get_payment(ctx.db, notice.inv_id)
    if payment is None:
        log.error("robokassa notice for an unknown InvId", extra={"inv_id": notice.inv_id})
        await ctx.alerts.alert(f"robokassa:unknown:{notice.inv_id}", texts.unknown_invoice_alert(notice.inv_id))
        return ResultReply(400, "unknown InvId")
    if notice.amount != payment.amount_rub:
        log.error("robokassa amount mismatch",
                  extra={"inv_id": notice.inv_id, "out_sum": notice.out_sum, "expected": payment.amount_rub})
        await ctx.alerts.alert(f"robokassa:amount:{notice.inv_id}", texts.amount_mismatch_alert(
            inv_id=notice.inv_id, received=notice.out_sum, expected=payment.amount_rub))
        return ResultReply(400, "amount mismatch")
    if payment.status != PaymentStatus.CREATED:
        return ok_reply(notice.inv_id)
    await _confirm(ctx, notice.inv_id, payment.game_id, raw=_raw(params))
    return ok_reply(notice.inv_id)


async def _confirm(ctx: AppContext, inv_id: int, game_id: int | None, *, raw: str) -> None:
    """Mark paid, apply the tier and queue the notifications atomically, under the game lock."""
    prices = await ctx.prices()
    lock = ctx.locks.game(game_id) if game_id is not None else contextlib.nullcontext()
    async with lock, ctx.db.transaction() as tx:
        outcome = await billing.confirm_payment(tx, inv_id, prices, raw=raw, now=ctx.clock.now())
        if outcome is None or outcome.already_processed:
            return
        await notices.payment_applied(ctx, outcome)
    log.info("payment confirmed", extra={
        "inv_id": inv_id, "tier": outcome.payment.tier, "amount": outcome.payment.amount_rub,
        "activated": len(outcome.activated),
    })


def _raw(params: Mapping[str, str]) -> str:
    """What we keep of the notice: amounts and method, never the payer's e-mail or the signature."""
    return json.dumps({name: params[name] for name in _KEPT_FIELDS if name in params}, ensure_ascii=False)
