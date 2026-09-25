"""Domain records mirroring the database rows (see migrations/001_init.sql).

Timestamps stay as UTC ISO strings (see ``core.clock``); ``exchange_date`` is a
``date``; integer flags become ``bool``.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any, TypeVar


class GameStatus(StrEnum):
    COLLECTING = "collecting"
    DRAWN = "drawn"
    FINISHED = "finished"
    CANCELLED = "cancelled"


class Tier(StrEnum):
    FREE = "free"
    S = "S"
    M = "M"
    L = "L"


class ParticipantStatus(StrEnum):
    ACTIVE = "active"
    WAITING = "waiting"
    LEFT = "left"
    REMOVED = "removed"


class JoinVia(StrEnum):
    LINK = "link"
    CODE = "code"
    GROUP = "group"
    REF = "ref"
    ORGANIZER = "organizer"


class PaymentStatus(StrEnum):
    CREATED = "created"
    PAID = "paid"
    REFUNDED = "refunded"
    GRANTED = "granted"


class PaymentProvider(StrEnum):
    ROBOKASSA = "robokassa"
    MANUAL = "manual"


class RelayDirection(StrEnum):
    TO_RECEIVER = "to_receiver"
    TO_SANTA = "to_santa"


class StateKind(StrEnum):
    """What free text the bot currently expects from a user (``user_state.kind``)."""

    RESUME = "resume"
    TITLE = "title"
    BUDGET_CUSTOM = "budget_custom"
    DATE_CUSTOM = "date_custom"
    WISHES = "wishes"
    DISPLAY_NAME = "display_name"
    CODE = "code"
    RELAY_TO_RECEIVER = "relay_to_receiver"
    RELAY_TO_SANTA = "relay_to_santa"
    REPLY_RELAY = "reply_relay"
    ADMIN_INPUT = "admin_input"


@dataclass(frozen=True, slots=True)
class User:
    user_id: int
    max_name: str | None
    username: str | None
    first_seen_at: str
    first_source: str
    consent_at: str | None
    consent_version: str | None
    dm_ok: bool
    blocked: bool
    games_created_today: int
    games_created_day: str | None

    @property
    def has_consent(self) -> bool:
        return self.consent_at is not None


@dataclass(frozen=True, slots=True)
class UserState:
    user_id: int
    kind: StateKind
    game_id: int | None
    data: dict[str, Any]
    expires_at: str


@dataclass(frozen=True, slots=True)
class Game:
    id: int
    code: str
    title: str
    organizer_id: int
    organizer_participates: bool
    budget_text: str
    exchange_date: date | None
    status: GameStatus
    tier: Tier
    participant_limit: int
    anon_chat: bool
    reminder_on: bool
    group_chat_id: int | None
    group_card_mid: str | None
    source_game_id: int | None
    source: str
    created_at: str
    drawn_at: str | None
    finished_at: str | None
    cancelled_at: str | None
    reveal_done: bool
    last_join_notice_at: str | None
    last_waiting_notice_at: str | None
    last_wish_reminder_at: str | None
    org_nudge_sent: bool
    pre_exchange_sent: bool
    redraw_count: int


@dataclass(frozen=True, slots=True)
class Participant:
    id: int
    game_id: int
    user_id: int
    display_name: str
    wishes: str | None
    status: ParticipantStatus
    joined_at: str
    via: JoinVia
    gift_ready: bool
    result_dm_ok: bool | None


@dataclass(frozen=True, slots=True)
class Payment:
    inv_id: int
    game_id: int | None
    payer_id: int | None
    tier: Tier
    amount_rub: int
    status: PaymentStatus
    provider: PaymentProvider
    created_at: str
    paid_at: str | None
    raw: str | None


@dataclass(frozen=True, slots=True)
class Relay:
    id: int
    game_id: int
    from_id: int
    to_id: int
    direction: RelayDirection
    text: str
    created_at: str


@dataclass(frozen=True, slots=True)
class Report:
    id: int
    relay_id: int | None
    game_id: int | None
    reporter_id: int
    reported_id: int
    text: str
    created_at: str
    resolved: bool


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime-editable settings (table ``settings``); env seeds them once."""

    free_limit: int
    price_S: int
    price_M: int
    price_L: int
    limit_S: int
    limit_M: int
    limit_L: int
    maintenance: bool


SETTING_KEYS: tuple[str, ...] = tuple(f.name for f in dataclasses.fields(Settings))

T = TypeVar("T")

_CONVERTERS: dict[str, Any] = {
    "bool": bool,
    "bool | None": bool,
    "date | None": date.fromisoformat,
    "dict[str, Any]": json.loads,
    "GameStatus": GameStatus,
    "Tier": Tier,
    "ParticipantStatus": ParticipantStatus,
    "JoinVia": JoinVia,
    "PaymentStatus": PaymentStatus,
    "PaymentProvider": PaymentProvider,
    "RelayDirection": RelayDirection,
    "StateKind": StateKind,
}


def from_row(cls: type[T], row: sqlite3.Row | Mapping[str, Any]) -> T:
    """Build a record from a DB row, converting flags, enums, dates and JSON by annotation."""
    values: dict[str, Any] = {}
    for field in dataclasses.fields(cls):  # type: ignore[arg-type]
        value = row[field.name]
        convert = _CONVERTERS.get(str(field.type))
        values[field.name] = convert(value) if convert is not None and value is not None else value
    return cls(**values)
