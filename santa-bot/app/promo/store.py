"""Data access for the ad autopilot's tables (migration 003_promo.sql) and its settings.

One small function per query, like ``app.repo``; each takes a ``Db`` (the database or an
open transaction). Promo settings live in the shared ``settings`` table under ``promo_*``
keys: seeded from the env on the first start with PROMO_ENABLED=1, then the database wins.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any

from app.config import PromoConfig
from app.core.clock import from_iso, to_iso
from app.db import Db
from app.promo.platforms import Budget, BudgetKind, CampaignInfo, CampaignState, Platform, SpendLimit, SpendRow
from app.promo.rules import ActionKind, PausedBy
from app.promo.vkads import StoredToken

SUSPEND_ALL_SRC = "*"


class ActionMode(StrEnum):
    DRY = "dry"
    AUTO = "auto"
    ADMIN = "admin"


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    APPLIED = "applied"
    FAILED = "failed"
    DECLINED = "declined"
    EXPIRED = "expired"


class PostStatus(StrEnum):
    SENT = "sent"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PromoCampaign:
    src: str
    platform: Platform
    external_id: str
    name: str
    state: CampaignState
    budget: Budget | None
    previous_budget_kop: int | None  # the budget(s) replaced on last_budget_change_day
    limit: SpendLimit | None
    pays_per_conversion: bool
    registered_at: datetime
    enabled: bool
    last_budget_change_day: date | None
    paused_by: PausedBy | None
    changed_at: datetime | None  # the bot's last change of state or budget
    spend_checked_at: datetime | None  # the last successful fetch of the spend


@dataclass(frozen=True, slots=True)
class PromoAction:
    id: int
    ts: datetime
    src: str
    action: ActionKind
    params: dict[str, Any]
    reason: str
    mode: ActionMode
    status: ActionStatus
    error: str | None
    decided_by: int | None


@dataclass(frozen=True, slots=True)
class PromoLink:
    src: str
    title: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SpendTotals:
    total_kop: int = 0
    matured_kop: int = 0  # days before the cohort cutoff
    yesterday_kop: int = 0


@dataclass(frozen=True, slots=True)
class PromoSettings:
    """Runtime-editable autopilot settings (``/ads auto``, ``/ads cap``, ``/ads rules``, ``/channel on``)."""

    auto: bool
    cap_rub: int
    pause_cpa_rub: int
    scale_cpa_rub: int
    min_spend_rub: int
    max_raise_pct: int
    lag_days: int
    channel_on: bool


_SETTING_PREFIX = "promo_"
_SETTING_NAMES = tuple(field.name for field in fields(PromoSettings))


def default_settings(config: PromoConfig) -> PromoSettings:
    """What the env seeds: test mode and the channel off until an admin switches them on."""
    return PromoSettings(
        auto=False, cap_rub=config.cap_rub, pause_cpa_rub=config.pause_cpa_rub,
        scale_cpa_rub=config.scale_cpa_rub, min_spend_rub=config.min_spend_rub,
        max_raise_pct=config.max_raise_pct, lag_days=config.lag_days, channel_on=False,
    )


# --- settings ------------------------------------------------------------------------------------------


async def seed_settings(db: Db, defaults: PromoSettings) -> None:
    await db.executemany(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
        [(_SETTING_PREFIX + name, str(int(getattr(defaults, name)))) for name in _SETTING_NAMES],
    )


async def get_settings(db: Db) -> PromoSettings:
    rows = await db.fetchall("SELECT key, value FROM settings WHERE key LIKE 'promo\\_%' ESCAPE '\\'")
    values = {row["key"].removeprefix(_SETTING_PREFIX): int(row["value"]) for row in rows}
    missing = [name for name in _SETTING_NAMES if name not in values]
    if missing:
        raise LookupError(f"promo settings not seeded: {missing}")
    return PromoSettings(
        auto=bool(values["auto"]), cap_rub=values["cap_rub"], pause_cpa_rub=values["pause_cpa_rub"],
        scale_cpa_rub=values["scale_cpa_rub"], min_spend_rub=values["min_spend_rub"],
        max_raise_pct=values["max_raise_pct"], lag_days=values["lag_days"], channel_on=bool(values["channel_on"]),
    )


async def set_settings(db: Db, **values: int | bool) -> None:
    unknown = set(values) - set(_SETTING_NAMES)
    if unknown:
        raise ValueError(f"unknown promo settings {sorted(unknown)}")
    await db.executemany(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        [(_SETTING_PREFIX + name, str(int(value))) for name, value in values.items()],
    )


# --- campaigns -----------------------------------------------------------------------------------------


def campaign_src(platform: Platform, external_id: str) -> str:
    """The attribution source of a campaign: yd701234567, vk123456."""
    return f"{platform}{external_id}"


async def get_campaign(db: Db, src: str) -> PromoCampaign | None:
    row = await db.fetchone("SELECT * FROM promo_campaigns WHERE src = ?", (src,))
    return None if row is None else _campaign(row)


async def enabled_campaigns(db: Db) -> list[PromoCampaign]:
    rows = await db.fetchall("SELECT * FROM promo_campaigns WHERE enabled = 1 ORDER BY platform DESC, src")
    return [_campaign(row) for row in rows]


async def register_campaign(db: Db, platform: Platform, info: CampaignInfo, now: datetime) -> PromoCampaign:
    """Start managing a campaign; registering it again re-enables it and keeps its history."""
    src = campaign_src(platform, info.id)
    await db.execute(
        "INSERT INTO promo_campaigns (src, platform, external_id, name, registered_at) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (src) DO UPDATE SET name = excluded.name, enabled = 1",
        (src, platform, info.id, info.name, to_iso(now)),
    )
    await _write_info(db, src, info)
    campaign = await get_campaign(db, src)
    assert campaign is not None
    return campaign


async def disable_campaign(db: Db, src: str) -> None:
    await db.execute("UPDATE promo_campaigns SET enabled = 0 WHERE src = ?", (src,))


async def save_campaign_info(db: Db, src: str, info: CampaignInfo, *, today: date, fetched_at: datetime) -> None:
    """What the platform reported at ``fetched_at`` (the name stays as registered). Nothing is written
    when the bot changed the campaign after that moment: the fetched values are older.

    A budget changed in the cabinet counts as changed today (on that day the old one may still
    spend). A campaign resumed in the cabinet is no longer paused by the autopilot; an admin's
    pause stays on record until /ads resume, so rules 5–6 leave that campaign alone.
    """
    current = await get_campaign(db, src)
    if current is None or (current.changed_at is not None and current.changed_at >= fetched_at):
        return
    if current.budget is not None and info.budget is not None and info.budget != current.budget:
        await _record_budget_change(db, current, today)
    await _write_info(db, src, info)


async def _write_info(db: Db, src: str, info: CampaignInfo) -> None:
    budget, limit = info.budget, info.limit
    await db.execute(
        "UPDATE promo_campaigns SET state = ?, budget_kind = ?, budget_kop = ?, limit_kop = ?, limit_start = ?,"
        " limit_end = ?, pays_per_conversion = ?,"
        " paused_by = CASE WHEN ? = 'paused' OR paused_by = 'admin' THEN paused_by END WHERE src = ?",
        (info.state, budget.kind if budget else None, budget.kop if budget else None, limit.kop if limit else None,
         _iso_date(limit.start if limit else None), _iso_date(limit.end if limit else None),
         int(info.pays_per_conversion), info.state, src),
    )


async def mark_missing(db: Db, src: str, *, fetched_at: datetime) -> None:
    """The platform no longer lists the campaign (deleted in the cabinet)."""
    await db.execute(
        "UPDATE promo_campaigns SET state = 'other' WHERE src = ? AND (changed_at IS NULL OR changed_at < ?)",
        (src, to_iso(fetched_at)),
    )


async def set_state(db: Db, src: str, state: CampaignState, paused_by: PausedBy | None, now: datetime) -> None:
    await db.execute("UPDATE promo_campaigns SET state = ?, paused_by = ?, changed_at = ? WHERE src = ?",
                     (state, paused_by, to_iso(now), src))


async def set_budget(db: Db, src: str, budget: Budget, *, today: date, now: datetime) -> None:
    """The bot changed the budget; the one it replaced may still spend today."""
    current = await get_campaign(db, src)
    if current is not None:
        await _record_budget_change(db, current, today)
    await db.execute("UPDATE promo_campaigns SET budget_kop = ?, budget_kind = ?, changed_at = ? WHERE src = ?",
                     (budget.kop, budget.kind, to_iso(now), src))


async def _record_budget_change(db: Db, campaign: PromoCampaign, today: date) -> None:
    """Remember the budget being replaced (added to any replaced earlier the same day)."""
    earlier = (campaign.previous_budget_kop or 0) if campaign.last_budget_change_day == today else 0
    replaced = earlier + (campaign.budget.kop if campaign.budget else 0)
    await db.execute(
        "UPDATE promo_campaigns SET previous_budget_kop = ?, last_budget_change_day = ? WHERE src = ?",
        (replaced, today.isoformat(), campaign.src),
    )


async def spend_within(db: Db, src: str, start: date | None, end: date | None) -> int:
    """The stored spend of a campaign between two days (inclusive); without dates, all of it."""
    return int(await db.fetchval(
        "SELECT COALESCE(SUM(cost_kop), 0) FROM promo_spend WHERE src = ? AND day BETWEEN ? AND ?",
        (src, (start or date.min).isoformat(), (end or date.max).isoformat()),
    ))


def _campaign(row: Any) -> PromoCampaign:
    return PromoCampaign(
        src=row["src"],
        platform=Platform(row["platform"]),
        external_id=row["external_id"],
        name=row["name"],
        state=CampaignState(row["state"]),
        budget=_budget(row["budget_kop"], row["budget_kind"]),
        previous_budget_kop=row["previous_budget_kop"],
        limit=None if row["limit_kop"] is None else SpendLimit(
            row["limit_kop"], _date(row["limit_start"]), _date(row["limit_end"])),
        pays_per_conversion=bool(row["pays_per_conversion"]),
        registered_at=from_iso(row["registered_at"]),
        enabled=bool(row["enabled"]),
        last_budget_change_day=_date(row["last_budget_change_day"]),
        paused_by=PausedBy(row["paused_by"]) if row["paused_by"] else None,
        changed_at=from_iso(row["changed_at"]) if row["changed_at"] else None,
        spend_checked_at=from_iso(row["spend_checked_at"]) if row["spend_checked_at"] else None,
    )


def _budget(kop: int | None, kind: str | None) -> Budget | None:
    return None if kop is None or kind is None else Budget(kop, BudgetKind(kind))


def _date(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)


def _iso_date(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


# --- spend -----------------------------------------------------------------------------------------------


async def replace_spend(
    db: Db, srcs: Sequence[str], date_from: date, date_to: date, rows: Mapping[tuple[str, date], SpendRow],
    fetched_at: datetime,
) -> None:
    """Store a fetched range for these campaigns (only those whose spend was fetched): platforms
    revise recent days and omit days without spend, so the range replaces what was stored."""
    marks = ", ".join("?" for _ in srcs)
    async with db.transaction() as tx:
        await tx.execute(
            f"DELETE FROM promo_spend WHERE src IN ({marks}) AND day BETWEEN ? AND ?",
            (*srcs, date_from.isoformat(), date_to.isoformat()),
        )
        await tx.executemany(
            "INSERT INTO promo_spend (src, day, cost_kop, clicks, fetched_at) VALUES (?, ?, ?, ?, ?)",
            [(src, day.isoformat(), row.cost_kop, row.clicks, to_iso(fetched_at))
             for (src, day), row in rows.items() if src in srcs],
        )
        await tx.execute(f"UPDATE promo_campaigns SET spend_checked_at = ? WHERE src IN ({marks})",
                         (to_iso(fetched_at), *srcs))


async def spend_totals(db: Db, today: date, cutoff: date) -> dict[str, SpendTotals]:
    """Per source: all spend, spend on days before ``cutoff`` and yesterday's spend."""
    yesterday = (today - timedelta(days=1)).isoformat()
    rows = await db.fetchall(
        "SELECT src, SUM(cost_kop) AS total, SUM(CASE WHEN day < ? THEN cost_kop ELSE 0 END) AS matured,"
        " SUM(CASE WHEN day = ? THEN cost_kop ELSE 0 END) AS yesterday FROM promo_spend GROUP BY src",
        (cutoff.isoformat(), yesterday),
    )
    return {row["src"]: SpendTotals(row["total"], row["matured"], row["yesterday"]) for row in rows}


async def total_spend(db: Db) -> int:
    """Everything spent on every campaign ever registered (removed ones spent real money too)."""
    return int(await db.fetchval("SELECT COALESCE(SUM(cost_kop), 0) FROM promo_spend"))


async def has_spend(db: Db, src: str) -> bool:
    return await db.fetchval("SELECT 1 FROM promo_spend WHERE src = ? LIMIT 1", (src,)) is not None


# --- actions ---------------------------------------------------------------------------------------------


async def insert_action(
    db: Db,
    *,
    now: datetime,
    src: str,
    action: ActionKind,
    params: Mapping[str, Any],
    reason: str,
    mode: ActionMode,
    status: ActionStatus,
    error: str | None = None,
    decided_by: int | None = None,
) -> int:
    result = await db.execute(
        "INSERT INTO promo_actions (ts, src, action, params, reason, mode, status, error, decided_by)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (to_iso(now), src, action, json.dumps(dict(params), ensure_ascii=False), reason, mode, status, error,
         decided_by),
    )
    assert result.lastrowid is not None
    return result.lastrowid


async def get_action(db: Db, action_id: int) -> PromoAction | None:
    row = await db.fetchone("SELECT * FROM promo_actions WHERE id = ?", (action_id,))
    return None if row is None else _action(row)


async def finish_action(
    db: Db, action_id: int, status: ActionStatus, *, error: str | None = None, decided_by: int | None = None
) -> bool:
    """Close a proposal; False when it was no longer proposed (someone decided first)."""
    result = await db.execute(
        "UPDATE promo_actions SET status = ?, error = ?, decided_by = ? WHERE id = ? AND status = 'proposed'",
        (status, error, decided_by, action_id),
    )
    return result.rowcount == 1


async def open_proposals(db: Db) -> list[PromoAction]:
    """Budget raises made in autopilot mode that wait for an admin."""
    rows = await db.fetchall(
        "SELECT * FROM promo_actions WHERE status = 'proposed' AND mode = 'auto' ORDER BY id"
    )
    return [_action(row) for row in rows]


async def expire_proposals(db: Db, before: datetime) -> int:
    result = await db.execute(
        "UPDATE promo_actions SET status = 'expired', error = 'older than a day'"
        " WHERE status = 'proposed' AND ts < ?",
        (to_iso(before),),
    )
    return result.rowcount


async def expire_open_proposals(db: Db, reason: str) -> int:
    """Close the autopilot's open proposals (it was switched off)."""
    result = await db.execute(
        "UPDATE promo_actions SET status = 'expired', error = ? WHERE status = 'proposed' AND mode = 'auto'",
        (reason,),
    )
    return result.rowcount


def _action(row: Any) -> PromoAction:
    return PromoAction(
        id=row["id"], ts=from_iso(row["ts"]), src=row["src"], action=ActionKind(row["action"]),
        params=json.loads(row["params"]), reason=row["reason"], mode=ActionMode(row["mode"]),
        status=ActionStatus(row["status"]), error=row["error"], decided_by=row["decided_by"],
    )


# --- links ------------------------------------------------------------------------------------------------


async def insert_link(db: Db, src: str, title: str, admin_id: int, now: datetime) -> bool:
    result = await db.execute(
        "INSERT OR IGNORE INTO promo_links (src, title, created_at, created_by) VALUES (?, ?, ?, ?)",
        (src, title, to_iso(now), admin_id),
    )
    return result.rowcount == 1


async def get_link(db: Db, src: str) -> PromoLink | None:
    row = await db.fetchone("SELECT * FROM promo_links WHERE src = ?", (src,))
    return None if row is None else PromoLink(row["src"], row["title"], from_iso(row["created_at"]))


async def all_links(db: Db) -> list[PromoLink]:
    rows = await db.fetchall("SELECT * FROM promo_links ORDER BY created_at, src")
    return [PromoLink(row["src"], row["title"], from_iso(row["created_at"])) for row in rows]


# --- channel posts -----------------------------------------------------------------------------------------


async def mark_post(db: Db, post_id: str, status: PostStatus, now: datetime) -> bool:
    """Record a calendar post as sent or skipped; False when it was already recorded."""
    result = await db.execute(
        "INSERT OR IGNORE INTO promo_posts_sent (post_id, sent_at, status) VALUES (?, ?, ?)",
        (post_id, to_iso(now), status),
    )
    return result.rowcount == 1


async def resend_post(db: Db, post_id: str, now: datetime) -> bool:
    """An admin sends again a post that was skipped as overdue or refused by MAX."""
    result = await db.execute(
        "UPDATE promo_posts_sent SET status = 'sent', sent_at = ? WHERE post_id = ?"
        " AND status IN ('skipped', 'failed')",
        (to_iso(now), post_id),
    )
    return result.rowcount == 1


async def set_post_outbox(db: Db, post_id: str, outbox_id: int) -> None:
    await db.execute("UPDATE promo_posts_sent SET outbox_id = ? WHERE post_id = ?", (outbox_id, post_id))


async def mark_post_failed(db: Db, outbox_id: int) -> str | None:
    """MAX refused the post sent as outbox row ``outbox_id``; returns its id."""
    post_id = await db.fetchval("SELECT post_id FROM promo_posts_sent WHERE outbox_id = ?", (outbox_id,))
    if post_id is not None:
        await db.execute("UPDATE promo_posts_sent SET status = 'failed' WHERE post_id = ?", (post_id,))
    return None if post_id is None else str(post_id)


async def post_statuses(db: Db) -> dict[str, PostStatus]:
    rows = await db.fetchall("SELECT post_id, status FROM promo_posts_sent")
    return {row["post_id"]: PostStatus(row["status"]) for row in rows}


# --- VK Ads tokens ------------------------------------------------------------------------------------------


class DbTokenStore:
    """``vkads.TokenStore`` on the ``promo_tokens`` table. Tokens are never logged."""

    def __init__(self, db: Db, platform: Platform) -> None:
        self._db = db
        self._platform = platform

    async def load(self) -> StoredToken | None:
        row = await self._db.fetchone("SELECT * FROM promo_tokens WHERE platform = ?", (self._platform,))
        if row is None:
            return None
        return StoredToken(row["access_token"], row["refresh_token"], from_iso(row["expires_at"]))

    async def save(self, token: StoredToken) -> None:
        await self._db.execute(
            "INSERT INTO promo_tokens (platform, access_token, refresh_token, expires_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (platform) DO UPDATE SET access_token = excluded.access_token,"
            " refresh_token = excluded.refresh_token, expires_at = excluded.expires_at",
            (self._platform, token.access_token, token.refresh_token, to_iso(token.expires_at)),
        )

    async def clear(self) -> None:
        await self._db.execute("DELETE FROM promo_tokens WHERE platform = ?", (self._platform,))


# --- channels the bot was added to (P1) ------------------------------------------------------------------------


async def remember_channel(db: Db, chat_id: int, now: datetime) -> None:
    await db.execute("INSERT OR IGNORE INTO promo_channels (chat_id, added_at) VALUES (?, ?)", (chat_id, to_iso(now)))


async def known_channels(db: Db) -> list[int]:
    rows = await db.fetchall("SELECT chat_id FROM promo_channels ORDER BY added_at, chat_id")
    return [row["chat_id"] for row in rows]
