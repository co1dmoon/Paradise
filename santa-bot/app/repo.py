"""Thin data-access layer: one small function per query, returning ``core.models`` records.

Business rules live in ``app.core``; these functions do not check permissions.
All take a ``Db`` (the Database or an open transaction) and explicit timestamps.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from app.core.clock import from_iso, to_iso
from app.core.models import (
    SETTING_KEYS,
    Game,
    GameStatus,
    JoinVia,
    Participant,
    ParticipantStatus,
    Payment,
    PaymentProvider,
    PaymentStatus,
    Relay,
    RelayDirection,
    Report,
    Settings,
    StateKind,
    Tier,
    User,
    UserState,
    from_row,
)
from app.db import Db

STATE_TTL = timedelta(minutes=30)

# --- users -------------------------------------------------------------------


async def get_user(db: Db, user_id: int) -> User | None:
    row = await db.fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return None if row is None else from_row(User, row)


async def insert_user(db: Db, user_id: int, first_source: str, now: datetime) -> bool:
    """Create the minimal pre-consent row (§5.1). Returns False if the user already exists."""
    result = await db.execute(
        "INSERT OR IGNORE INTO users (user_id, first_seen_at, first_source) VALUES (?, ?, ?)",
        (user_id, to_iso(now), first_source),
    )
    return result.rowcount == 1


async def set_consent(
    db: Db, user_id: int, name: str, username: str | None, version: str, now: datetime
) -> None:
    await db.execute(
        "UPDATE users SET consent_at = ?, consent_version = ?, max_name = ?, username = ?"
        " WHERE user_id = ?",
        (to_iso(now), version, name, username, user_id),
    )


async def update_profile(db: Db, user_id: int, name: str, username: str | None) -> None:
    """Refresh the MAX name of a consented user (names change over time)."""
    await db.execute(
        "UPDATE users SET max_name = ?, username = ? WHERE user_id = ? AND consent_at IS NOT NULL",
        (name, username, user_id),
    )


async def set_dm_ok(db: Db, user_id: int, ok: bool) -> None:
    """Whether private messages reach the user; only writes when the value changes."""
    await db.execute("UPDATE users SET dm_ok = ? WHERE user_id = ? AND dm_ok <> ?", (ok, user_id, ok))


async def set_blocked(db: Db, user_id: int, blocked: bool) -> bool:
    result = await db.execute("UPDATE users SET blocked = ? WHERE user_id = ?", (blocked, user_id))
    return result.rowcount == 1


async def bump_games_created(db: Db, user_id: int, day: str) -> int:
    """Increment the per-day game counter (resetting it on a new day); returns the new count."""
    await db.execute(
        "UPDATE users SET games_created_today = CASE WHEN games_created_day = ?"
        " THEN games_created_today + 1 ELSE 1 END, games_created_day = ? WHERE user_id = ?",
        (day, day, user_id),
    )
    return int(await db.fetchval("SELECT games_created_today FROM users WHERE user_id = ?", (user_id,)))


# --- user_state ----------------------------------------------------------------


async def set_state(
    db: Db,
    user_id: int,
    kind: StateKind,
    now: datetime,
    *,
    game_id: int | None = None,
    data: Mapping[str, Any] | None = None,
) -> None:
    """Replace the user's pending input; it expires after 30 minutes."""
    await db.execute(
        "INSERT INTO user_state (user_id, kind, game_id, data, expires_at) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (user_id) DO UPDATE SET kind = excluded.kind, game_id = excluded.game_id,"
        " data = excluded.data, expires_at = excluded.expires_at",
        (user_id, kind, game_id, json.dumps(dict(data or {}), ensure_ascii=False), to_iso(now + STATE_TTL)),
    )


async def set_state_if_idle(db: Db, user_id: int, kind: StateKind, now: datetime, *, game_id: int) -> bool:
    """Like ``set_state``, unless another input is still pending (someone else's action must not
    interrupt what the user is typing). Returns whether the state was set."""
    result = await db.execute(
        "INSERT INTO user_state (user_id, kind, game_id, data, expires_at) VALUES (?, ?, ?, '{}', ?)"
        " ON CONFLICT (user_id) DO UPDATE SET kind = excluded.kind, game_id = excluded.game_id,"
        " data = excluded.data, expires_at = excluded.expires_at WHERE user_state.expires_at <= ?",
        (user_id, kind, game_id, to_iso(now + STATE_TTL), to_iso(now)),
    )
    return result.rowcount == 1


async def get_state(db: Db, user_id: int, now: datetime) -> UserState | None:
    """The pending input, or None (an expired state is deleted)."""
    row = await db.fetchone("SELECT * FROM user_state WHERE user_id = ?", (user_id,))
    if row is None:
        return None
    if row["expires_at"] <= to_iso(now):
        await clear_state(db, user_id)
        return None
    return from_row(UserState, row)


async def clear_state(db: Db, user_id: int) -> bool:
    result = await db.execute("DELETE FROM user_state WHERE user_id = ?", (user_id,))
    return result.rowcount == 1


# --- games -----------------------------------------------------------------------


async def get_game(db: Db, game_id: int) -> Game | None:
    row = await db.fetchone("SELECT * FROM games WHERE id = ?", (game_id,))
    return None if row is None else from_row(Game, row)


async def get_game_by_code(db: Db, code: str) -> Game | None:
    row = await db.fetchone("SELECT * FROM games WHERE code = ?", (code.upper(),))
    return None if row is None else from_row(Game, row)


async def code_exists(db: Db, code: str) -> bool:
    return await db.fetchval("SELECT 1 FROM games WHERE code = ?", (code,)) is not None


async def insert_game(db: Db, fields: Mapping[str, Any]) -> int:
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    result = await db.execute(f"INSERT INTO games ({columns}) VALUES ({placeholders})", tuple(fields.values()))
    assert result.lastrowid is not None
    return result.lastrowid


_GAME_UPDATABLE = frozenset(
    {
        "title", "organizer_participates", "budget_text", "exchange_date", "status", "tier",
        "participant_limit", "anon_chat", "reminder_on", "group_chat_id", "group_card_mid",
        "drawn_at", "finished_at", "cancelled_at", "reveal_done", "last_join_notice_at",
        "last_waiting_notice_at", "last_wish_reminder_at", "org_nudge_sent", "pre_exchange_sent",
        "redraw_count",
    }
)


async def update_game(db: Db, game_id: int, **fields: Any) -> None:
    """Update whitelisted columns, e.g. ``update_game(db, 7, title='5Б', anon_chat=False)``.

    ``datetime`` values are stored as UTC ISO text and ``date`` values as 'YYYY-MM-DD'.
    """
    unknown = set(fields) - _GAME_UPDATABLE
    if unknown:
        raise ValueError(f"cannot update game columns {sorted(unknown)}")
    assignments = ", ".join(f"{column} = ?" for column in fields)
    values = [_db_value(value) for value in fields.values()]
    await db.execute(f"UPDATE games SET {assignments} WHERE id = ?", (*values, game_id))


def _db_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return to_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


async def games_in_status(db: Db, status: GameStatus) -> list[Game]:
    rows = await db.fetchall("SELECT * FROM games WHERE status = ? ORDER BY id", (status,))
    return [from_row(Game, row) for row in rows]


async def games_of_user(db: Db, user_id: int) -> list[tuple[Game, Participant | None]]:
    """Games the user organizes or takes part in (active or waiting), newest first."""
    rows = await db.fetchall(
        "SELECT * FROM participants WHERE user_id = ? AND status IN ('active', 'waiting')", (user_id,)
    )
    mine = {row["game_id"]: from_row(Participant, row) for row in rows}
    marks = ", ".join("?" for _ in mine)
    games = await db.fetchall(
        f"SELECT * FROM games WHERE organizer_id = ? OR id IN ({marks}) ORDER BY created_at DESC, id DESC",
        (user_id, *mine),
    )
    return [(from_row(Game, row), mine.get(row["id"])) for row in games]


# --- participants ----------------------------------------------------------------


async def get_participant(db: Db, game_id: int, user_id: int) -> Participant | None:
    row = await db.fetchone(
        "SELECT * FROM participants WHERE game_id = ? AND user_id = ?", (game_id, user_id)
    )
    return None if row is None else from_row(Participant, row)


async def participants(
    db: Db, game_id: int, *statuses: ParticipantStatus
) -> list[Participant]:
    """Participants in join order, optionally filtered by status."""
    wanted = statuses or tuple(ParticipantStatus)
    marks = ", ".join("?" for _ in wanted)
    rows = await db.fetchall(
        f"SELECT * FROM participants WHERE game_id = ? AND status IN ({marks})"
        " ORDER BY joined_at, id",
        (game_id, *wanted),
    )
    return [from_row(Participant, row) for row in rows]


async def count_participants(db: Db, game_id: int, status: ParticipantStatus) -> int:
    return int(
        await db.fetchval(
            "SELECT COUNT(*) FROM participants WHERE game_id = ? AND status = ?", (game_id, status)
        )
    )


async def upsert_participant(
    db: Db,
    game_id: int,
    user_id: int,
    display_name: str,
    status: ParticipantStatus,
    via: JoinVia,
    now: datetime,
) -> Participant:
    """Insert, or re-activate a row that has left (keeping name and wishes)."""
    await db.execute(
        "INSERT INTO participants (game_id, user_id, display_name, status, joined_at, via)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (game_id, user_id) DO UPDATE SET status = excluded.status,"
        " joined_at = excluded.joined_at, via = excluded.via",
        (game_id, user_id, display_name, status, to_iso(now), via),
    )
    participant = await get_participant(db, game_id, user_id)
    assert participant is not None
    return participant


async def set_participant_status(
    db: Db, game_id: int, user_id: int, status: ParticipantStatus
) -> None:
    await db.execute(
        "UPDATE participants SET status = ? WHERE game_id = ? AND user_id = ?", (status, game_id, user_id)
    )


async def set_display_name(db: Db, game_id: int, user_id: int, name: str) -> None:
    await db.execute(
        "UPDATE participants SET display_name = ? WHERE game_id = ? AND user_id = ?", (name, game_id, user_id)
    )


async def set_wishes(db: Db, game_id: int, user_id: int, wishes: str) -> None:
    await db.execute(
        "UPDATE participants SET wishes = ? WHERE game_id = ? AND user_id = ?", (wishes, game_id, user_id)
    )


async def set_gift_ready(db: Db, game_id: int, user_id: int) -> None:
    await db.execute(
        "UPDATE participants SET gift_ready = 1 WHERE game_id = ? AND user_id = ?", (game_id, user_id)
    )


async def set_result_dm_ok(db: Db, game_id: int, user_id: int, ok: bool | None) -> int:
    result = await db.execute(
        "UPDATE participants SET result_dm_ok = ? WHERE game_id = ? AND user_id = ?", (ok, game_id, user_id)
    )
    return result.rowcount


async def joined_since(db: Db, game_id: int, since: str | None, exclude_user: int) -> list[Participant]:
    """Active participants who joined after ``since`` (ISO), for join notices (§5.4)."""
    rows = await db.fetchall(
        "SELECT * FROM participants WHERE game_id = ? AND status = 'active' AND user_id <> ?"
        " AND joined_at > ? ORDER BY joined_at, id",
        (game_id, exclude_user, since or ""),
    )
    return [from_row(Participant, row) for row in rows]


# --- exclusions ------------------------------------------------------------------


def _ordered(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


async def exclusions(db: Db, game_id: int) -> list[tuple[int, int]]:
    """Excluded pairs (user_a < user_b) in the order they were added."""
    rows = await db.fetchall(
        "SELECT user_a, user_b FROM exclusions WHERE game_id = ? ORDER BY rowid", (game_id,)
    )
    return [(row["user_a"], row["user_b"]) for row in rows]


async def add_exclusion(db: Db, game_id: int, a: int, b: int) -> bool:
    result = await db.execute(
        "INSERT OR IGNORE INTO exclusions (game_id, user_a, user_b) VALUES (?, ?, ?)",
        (game_id, *_ordered(a, b)),
    )
    return result.rowcount == 1


async def delete_exclusion(db: Db, game_id: int, a: int, b: int) -> bool:
    result = await db.execute(
        "DELETE FROM exclusions WHERE game_id = ? AND user_a = ? AND user_b = ?", (game_id, *_ordered(a, b))
    )
    return result.rowcount == 1


async def delete_exclusions_of(db: Db, game_id: int, user_id: int) -> None:
    await db.execute(
        "DELETE FROM exclusions WHERE game_id = ? AND (user_a = ? OR user_b = ?)", (game_id, user_id, user_id)
    )


# --- assignments -----------------------------------------------------------------


async def assignments(db: Db, game_id: int) -> dict[int, int]:
    rows = await db.fetchall("SELECT giver_id, receiver_id FROM assignments WHERE game_id = ?", (game_id,))
    return {row["giver_id"]: row["receiver_id"] for row in rows}


async def receiver_of(db: Db, game_id: int, giver_id: int) -> int | None:
    return await db.fetchval(
        "SELECT receiver_id FROM assignments WHERE game_id = ? AND giver_id = ?", (game_id, giver_id)
    )


async def giver_of(db: Db, game_id: int, receiver_id: int) -> int | None:
    return await db.fetchval(
        "SELECT giver_id FROM assignments WHERE game_id = ? AND receiver_id = ?", (game_id, receiver_id)
    )


async def replace_assignments(db: Db, game_id: int, pairs: Mapping[int, int]) -> None:
    await db.execute("DELETE FROM assignments WHERE game_id = ?", (game_id,))
    await db.executemany(
        "INSERT INTO assignments (game_id, giver_id, receiver_id) VALUES (?, ?, ?)",
        [(game_id, giver, receiver) for giver, receiver in pairs.items()],
    )


async def delete_assignments_of(db: Db, game_id: int, user_id: int) -> None:
    await db.execute(
        "DELETE FROM assignments WHERE game_id = ? AND (giver_id = ? OR receiver_id = ?)",
        (game_id, user_id, user_id),
    )


async def insert_assignment(db: Db, game_id: int, giver_id: int, receiver_id: int) -> None:
    await db.execute(
        "INSERT INTO assignments (game_id, giver_id, receiver_id) VALUES (?, ?, ?)",
        (game_id, giver_id, receiver_id),
    )


# --- payments --------------------------------------------------------------------


async def get_payment(db: Db, inv_id: int) -> Payment | None:
    row = await db.fetchone("SELECT * FROM payments WHERE inv_id = ?", (inv_id,))
    return None if row is None else from_row(Payment, row)


async def insert_payment(
    db: Db,
    game_id: int,
    payer_id: int | None,
    tier: Tier,
    amount_rub: int,
    status: PaymentStatus,
    provider: PaymentProvider,
    now: datetime,
) -> Payment:
    paid_at = to_iso(now) if status in (PaymentStatus.PAID, PaymentStatus.GRANTED) else None
    result = await db.execute(
        "INSERT INTO payments (game_id, payer_id, tier, amount_rub, status, provider, created_at, paid_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (game_id, payer_id, tier, amount_rub, status, provider, to_iso(now), paid_at),
    )
    assert result.lastrowid is not None
    payment = await get_payment(db, result.lastrowid)
    assert payment is not None
    return payment


async def reusable_payment(
    db: Db, game_id: int, payer_id: int, tier: Tier, amount_rub: int, since: datetime
) -> Payment | None:
    """A 'created' payment with the same game, tier, payer and amount younger than ``since``."""
    row = await db.fetchone(
        "SELECT * FROM payments WHERE game_id = ? AND payer_id = ? AND tier = ? AND amount_rub = ?"
        " AND status = 'created' AND created_at > ? ORDER BY inv_id DESC LIMIT 1",
        (game_id, payer_id, tier, amount_rub, to_iso(since)),
    )
    return None if row is None else from_row(Payment, row)


async def mark_payment(
    db: Db, inv_id: int, status: PaymentStatus, now: datetime | None = None, raw: str | None = None
) -> None:
    await db.execute(
        "UPDATE payments SET status = ?, paid_at = COALESCE(?, paid_at), raw = COALESCE(?, raw)"
        " WHERE inv_id = ?",
        (status, None if now is None else to_iso(now), raw, inv_id),
    )


async def paid_sum(db: Db, game_id: int) -> int:
    """Rubles paid or granted for a game (refunded payments do not count)."""
    return int(
        await db.fetchval(
            "SELECT COALESCE(SUM(amount_rub), 0) FROM payments"
            " WHERE game_id = ? AND status IN ('paid', 'granted')",
            (game_id,),
        )
    )


async def payments_of_game(db: Db, game_id: int) -> list[Payment]:
    rows = await db.fetchall("SELECT * FROM payments WHERE game_id = ? ORDER BY inv_id", (game_id,))
    return [from_row(Payment, row) for row in rows]


# --- relays and reports ------------------------------------------------------------


async def insert_relay(
    db: Db, game_id: int, from_id: int, to_id: int, direction: RelayDirection, text: str, now: datetime
) -> Relay:
    result = await db.execute(
        "INSERT INTO relay_messages (game_id, from_id, to_id, direction, text, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (game_id, from_id, to_id, direction, text, to_iso(now)),
    )
    relay = await get_relay(db, int(result.lastrowid or 0))
    assert relay is not None
    return relay


async def get_relay(db: Db, relay_id: int) -> Relay | None:
    row = await db.fetchone("SELECT * FROM relay_messages WHERE id = ?", (relay_id,))
    return None if row is None else from_row(Relay, row)


async def count_relays_since(db: Db, game_id: int, from_id: int, since: datetime) -> int:
    return int(
        await db.fetchval(
            "SELECT COUNT(*) FROM relay_messages WHERE game_id = ? AND from_id = ? AND created_at > ?",
            (game_id, from_id, to_iso(since)),
        )
    )


async def insert_report(
    db: Db, relay_id: int | None, game_id: int | None, reporter_id: int, reported_id: int, text: str,
    now: datetime,
) -> Report:
    result = await db.execute(
        "INSERT INTO reports (relay_id, game_id, reporter_id, reported_id, text, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (relay_id, game_id, reporter_id, reported_id, text, to_iso(now)),
    )
    report = await get_report(db, int(result.lastrowid or 0))
    assert report is not None
    return report


async def get_report(db: Db, report_id: int) -> Report | None:
    row = await db.fetchone("SELECT * FROM reports WHERE id = ?", (report_id,))
    return None if row is None else from_row(Report, row)


async def report_exists(db: Db, relay_id: int, reporter_id: int) -> bool:
    return (
        await db.fetchval(
            "SELECT 1 FROM reports WHERE relay_id = ? AND reporter_id = ?", (relay_id, reporter_id)
        )
        is not None
    )


async def resolve_report(db: Db, report_id: int) -> bool:
    result = await db.execute("UPDATE reports SET resolved = 1 WHERE id = ? AND resolved = 0", (report_id,))
    return result.rowcount == 1


# --- settings --------------------------------------------------------------------


async def seed_settings(db: Db, defaults: Settings) -> None:
    """Insert env defaults for missing keys only: after the first start the DB wins (§3)."""
    await db.executemany(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
        [(key, str(int(getattr(defaults, key)))) for key in SETTING_KEYS],
    )


async def get_settings(db: Db) -> Settings:
    rows = await db.fetchall("SELECT key, value FROM settings")
    values = {row["key"]: row["value"] for row in rows}
    missing = [key for key in SETTING_KEYS if key not in values]
    if missing:
        raise LookupError(f"settings not seeded: {missing}")
    return Settings(
        free_limit=int(values["free_limit"]),
        price_S=int(values["price_S"]),
        price_M=int(values["price_M"]),
        price_L=int(values["price_L"]),
        limit_S=int(values["limit_S"]),
        limit_M=int(values["limit_M"]),
        limit_L=int(values["limit_L"]),
        maintenance=values["maintenance"] == "1",
    )


async def set_setting(db: Db, key: str, value: int | bool) -> None:
    if key not in SETTING_KEYS:
        raise ValueError(f"unknown setting {key!r}")
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, str(int(value))),
    )


# --- processed updates -------------------------------------------------------------


async def mark_update_processed(db: Db, key: str, now: datetime) -> bool:
    """Record an update key; False if it was already seen (a webhook retry)."""
    result = await db.execute(
        "INSERT OR IGNORE INTO processed_updates (key, ts) VALUES (?, ?)", (key, to_iso(now))
    )
    return result.rowcount == 1


async def prune_processed_updates(db: Db, before: datetime) -> int:
    result = await db.execute("DELETE FROM processed_updates WHERE ts < ?", (to_iso(before),))
    return result.rowcount



async def detach_group_chat(db: Db, chat_id: int) -> int:
    """The bot left a group chat (§5.10): its games continue in link mode."""
    result = await db.execute(
        "UPDATE games SET group_chat_id = NULL, group_card_mid = NULL WHERE group_chat_id = ?", (chat_id,)
    )
    return result.rowcount


# --- scheduler (§10) ---------------------------------------------------------------


async def job_runs(db: Db) -> dict[str, datetime]:
    """When each periodic job last ran."""
    rows = await db.fetchall("SELECT name, last_run_at FROM job_runs")
    return {row["name"]: from_iso(row["last_run_at"]) for row in rows}


async def record_job_run(db: Db, name: str, at: datetime) -> None:
    await db.execute(
        "INSERT INTO job_runs (name, last_run_at) VALUES (?, ?)"
        " ON CONFLICT (name) DO UPDATE SET last_run_at = excluded.last_run_at",
        (name, to_iso(at)),
    )


_NOTICE_COLUMNS = {
    ParticipantStatus.ACTIVE: "last_join_notice_at",
    ParticipantStatus.WAITING: "last_waiting_notice_at",
}


async def games_with_newcomers(db: Db, status: ParticipantStatus, noticed_before: datetime) -> list[Game]:
    """Collecting games where someone other than the organizer got ``status`` (active: joined,
    waiting: queued) after the last notice of that kind, if that notice is older than
    ``noticed_before`` (§5.4, §5.6 throttling)."""
    column = _NOTICE_COLUMNS[status]
    rows = await db.fetchall(
        f"SELECT * FROM games g WHERE g.status = 'collecting' AND (g.{column} IS NULL OR g.{column} <= ?)"
        " AND EXISTS (SELECT 1 FROM participants p WHERE p.game_id = g.id AND p.status = ?"
        f" AND p.user_id <> g.organizer_id AND p.joined_at > COALESCE(g.{column}, '')) ORDER BY g.id",
        (to_iso(noticed_before), status),
    )
    return [from_row(Game, row) for row in rows]


async def games_to_nudge(db: Db, first: date, last: date) -> list[Game]:
    """Collecting games not nudged yet whose exchange date is between ``first`` and ``last`` (§5.9 b)."""
    rows = await db.fetchall(
        "SELECT * FROM games WHERE status = 'collecting' AND org_nudge_sent = 0"
        " AND exchange_date BETWEEN ? AND ? ORDER BY id",
        (first.isoformat(), last.isoformat()),
    )
    return [from_row(Game, row) for row in rows]


async def games_to_remind(db: Db, exchange_date: date) -> list[Game]:
    """Drawn games with the reminder on, not reminded yet, exchanging on ``exchange_date`` (§5.9 c)."""
    rows = await db.fetchall(
        "SELECT * FROM games WHERE status = 'drawn' AND reminder_on = 1 AND pre_exchange_sent = 0"
        " AND exchange_date = ? ORDER BY id",
        (exchange_date.isoformat(),),
    )
    return [from_row(Game, row) for row in rows]
