"""Deep-link start payloads (§6.7), game codes and source attribution (§6.6, §6.8).

Grammar (anything else is an empty payload, i.e. ``None``):

- ``j_CODE``  join a game
- ``n_CODE``  create a new game, referred by that game
- ``s_src``   source tag, sanitized to [a-z0-9]{1,16}
- ``p_INVID`` return from a payment
- ``gc_ID`` / ``gcmID``  create a game for a group chat (negative ids use ``gcm``)
- ``o_CODE``  open the organizer panel
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6
MAX_START_PAYLOAD = 128
MAX_SOURCE = 16
DEFAULT_SITE_SOURCE = "site"
DIRECT_SOURCE = "direct"

_CODE_TEXT = re.compile(r"^\s*(?:код\s*)?([A-Za-z2-9]{6})\s*$", re.IGNORECASE)
_CODE = re.compile(r"[A-Za-z2-9]{6}")
_SOURCE_JUNK = re.compile(r"[^a-z0-9]")


@dataclass(frozen=True, slots=True)
class JoinPayload:
    code: str


@dataclass(frozen=True, slots=True)
class NewGamePayload:
    code: str


@dataclass(frozen=True, slots=True)
class SourcePayload:
    source: str


@dataclass(frozen=True, slots=True)
class PaymentReturnPayload:
    inv_id: int


@dataclass(frozen=True, slots=True)
class GroupPayload:
    chat_id: int


@dataclass(frozen=True, slots=True)
class OrganizerPayload:
    code: str


StartPayload = (
    JoinPayload | NewGamePayload | SourcePayload | PaymentReturnPayload | GroupPayload | OrganizerPayload
)


def generate_code(rng: random.Random) -> str:
    return "".join(rng.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_code(raw: str) -> str | None:
    """Upper-cased code if ``raw`` looks like one, else None. Existence is checked in the DB."""
    raw = raw.strip()
    return raw.upper() if _CODE.fullmatch(raw) else None


def extract_code(text: str) -> str | None:
    """Code from a private message such as 'abc123' or 'код ABC123' (§5.3)."""
    match = _CODE_TEXT.match(text)
    return match.group(1).upper() if match else None


def sanitize_source(raw: str | None) -> str:
    """utm_source/src value reduced to [a-z0-9]{1,16}; 'site' when nothing is left."""
    cleaned = _SOURCE_JUNK.sub("", (raw or "").lower())[:MAX_SOURCE]
    return cleaned or DEFAULT_SITE_SOURCE


def parse_start_payload(raw: str | None) -> StartPayload | None:
    if not raw or len(raw) > MAX_START_PAYLOAD:
        return None
    raw = raw.strip()
    prefix, _, value = raw.partition("_")
    if raw.startswith("gcm"):
        return _group(raw[3:], negative=True)
    if prefix == "gc":
        return _group(value, negative=False)
    if prefix in ("j", "n", "o"):
        return _code_payload(prefix, value)
    if prefix == "s":
        source = _SOURCE_JUNK.sub("", value.lower())[:MAX_SOURCE]
        return SourcePayload(source) if source else None
    if prefix == "p" and value.isascii() and value.isdigit():
        return PaymentReturnPayload(int(value))
    return None


def _code_payload(prefix: str, value: str) -> JoinPayload | NewGamePayload | OrganizerPayload | None:
    code = normalize_code(value)
    if code is None:
        return None
    if prefix == "j":
        return JoinPayload(code)
    return NewGamePayload(code) if prefix == "n" else OrganizerPayload(code)


def _group(digits: str, *, negative: bool) -> GroupPayload | None:
    if not (digits.isascii() and digits.isdigit()):
        return None
    chat_id = int(digits)
    return GroupPayload(-chat_id if negative else chat_id)


def first_source(payload: StartPayload | None) -> str:
    """Value for users.first_source: 'j:CODE', 'n:CODE', 's:src' or 'direct' (§6.8)."""
    match payload:
        case JoinPayload(code):
            return f"j:{code}"
        case NewGamePayload(code):
            return f"n:{code}"
        case SourcePayload(source):
            return f"s:{source}"
        case _:
            return DIRECT_SOURCE


def join_payload(code: str) -> str:
    return f"j_{code}"


def new_game_payload(code: str) -> str:
    return f"n_{code}"


def source_payload(source: str) -> str:
    return f"s_{sanitize_source(source)}"


def payment_return_payload(inv_id: int) -> str:
    return f"p_{inv_id}"


def group_payload(chat_id: int) -> str:
    return f"gcm{-chat_id}" if chat_id < 0 else f"gc_{chat_id}"


def organizer_payload(code: str) -> str:
    return f"o_{code}"


def deep_link(bot_username: str, payload: str | None = None) -> str:
    """https://max.ru/<bot>?start=<payload> (payload up to 128 characters)."""
    if payload is None:
        return f"https://max.ru/{bot_username}"
    if len(payload) > MAX_START_PAYLOAD:
        raise ValueError(f"start payload longer than {MAX_START_PAYLOAD}: {payload!r}")
    return f"https://max.ru/{bot_username}?start={payload}"
