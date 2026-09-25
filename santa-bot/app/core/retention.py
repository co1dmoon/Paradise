"""The end of a game's life and data retention (§10), run nightly by the scheduler.

Ending games (00:10 MSK):
- a drawn game becomes finished 3 days after its exchange date (§10);
- abandoned games end as well, otherwise retention could never delete them: a game
  that was never drawn is cancelled 30 days after its exchange date, and a game
  without a date ends 180 days after it was created (finished if it was drawn).
  Nobody is notified: these are not messages §5.9 allows.

Retention (03:30 MSK):
- finished and cancelled games are deleted with all their rows (participants, wishes,
  exclusions, pairs, anonymous messages, complaints, queued messages) 180 days after
  the exchange date, or 180 days after creation when there is no date. Payments stay
  for accounting; their game_id becomes NULL;
- anonymous messages are deleted after 30 days, and so are closed complaints, which
  quote them; open complaints wait for an admin but lose the quoted text (the admins
  got it when the complaint was made);
- users who never consented are deleted after 7 days; users with no games for 365
  days are deleted (counted from the date of their last game, see ``last_game_at``);
  events and payments of deleted users lose the user id;
- payments older than 180 days lose payer_id, events older than 180 days lose user_id;
- expired pending inputs (``user_state``) are deleted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from app.core.clock import to_iso
from app.db import Db

FINISH_AFTER = timedelta(days=3)
ABANDON_AFTER = timedelta(days=30)
UNDATED_GAME_LIFETIME = timedelta(days=180)
GAME_RETENTION = timedelta(days=180)
RELAY_RETENTION = timedelta(days=30)
UNCONSENTED_RETENTION = timedelta(days=7)
INACTIVE_USER_RETENTION = timedelta(days=365)
PAYER_RETENTION = timedelta(days=180)
EVENT_USER_RETENTION = timedelta(days=180)

# Users that may be deleted: never consented for 7 days, or no games for 365 days.
_DELETABLE_USERS = (
    "SELECT u.user_id FROM users u WHERE"
    " NOT EXISTS (SELECT 1 FROM games g WHERE g.organizer_id = u.user_id)"
    " AND NOT EXISTS (SELECT 1 FROM participants p WHERE p.user_id = u.user_id)"
    " AND ((u.consent_at IS NULL AND u.first_seen_at <= :unconsented)"
    "  OR (u.consent_at IS NOT NULL AND COALESCE(u.last_game_at, u.consent_at) <= :inactive))"
)


@dataclass(frozen=True, slots=True)
class EndedGames:
    finished: int
    cancelled: int


@dataclass(frozen=True, slots=True)
class PurgeReport:
    games: int
    relays: int
    reports: int
    users: int
    payments_anonymized: int
    events_anonymized: int
    expired_inputs: int


async def end_games(db: Db, now: datetime, today: date) -> EndedGames:
    """Finish drawn games after their exchange and end abandoned ones (see the module docstring)."""
    stamp, undated_before = to_iso(now), to_iso(now - UNDATED_GAME_LIFETIME)
    async with db.transaction() as tx:
        finished = await tx.execute(
            "UPDATE games SET status = 'finished', finished_at = ? WHERE status = 'drawn'"
            " AND ((exchange_date IS NOT NULL AND exchange_date <= ?)"
            "  OR (exchange_date IS NULL AND created_at <= ?))",
            (stamp, (today - FINISH_AFTER).isoformat(), undated_before),
        )
        cancelled = await tx.execute(
            "UPDATE games SET status = 'cancelled', cancelled_at = ? WHERE status = 'collecting'"
            " AND ((exchange_date IS NOT NULL AND exchange_date <= ?)"
            "  OR (exchange_date IS NULL AND created_at <= ?))",
            (stamp, (today - ABANDON_AFTER).isoformat(), undated_before),
        )
    return EndedGames(finished.rowcount, cancelled.rowcount)


async def purge(db: Db, now: datetime, today: date) -> PurgeReport:
    """Delete and anonymize what §10 says; one transaction."""
    async with db.transaction() as tx:
        games = await _delete_old_games(tx, now, today)
        relays = await tx.execute(
            "DELETE FROM relay_messages WHERE created_at <= ?", (to_iso(now - RELAY_RETENTION),)
        )
        reports = await tx.execute(
            "DELETE FROM reports WHERE resolved = 1 AND created_at <= ?", (to_iso(now - RELAY_RETENTION),)
        )
        await tx.execute(
            "UPDATE reports SET text = '' WHERE resolved = 0 AND text <> '' AND created_at <= ?",
            (to_iso(now - RELAY_RETENTION),),
        )
        users = await _delete_users(tx, now)
        payments = await tx.execute(
            "UPDATE payments SET payer_id = NULL WHERE payer_id IS NOT NULL AND created_at <= ?",
            (to_iso(now - PAYER_RETENTION),),
        )
        events = await tx.execute(
            "UPDATE events SET user_id = NULL WHERE user_id IS NOT NULL AND ts <= ?",
            (to_iso(now - EVENT_USER_RETENTION),),
        )
        inputs = await tx.execute("DELETE FROM user_state WHERE expires_at <= ?", (to_iso(now),))
    return PurgeReport(
        games=games,
        relays=relays.rowcount,
        reports=reports.rowcount,
        users=users,
        payments_anonymized=payments.rowcount,
        events_anonymized=events.rowcount,
        expired_inputs=inputs.rowcount,
    )


async def _delete_old_games(db: Db, now: datetime, today: date) -> int:
    rows = await db.fetchall(
        "SELECT id, exchange_date, created_at FROM games WHERE status IN ('finished', 'cancelled')"
        " AND ((exchange_date IS NOT NULL AND exchange_date <= ?)"
        "  OR (exchange_date IS NULL AND created_at <= ?))",
        ((today - GAME_RETENTION).isoformat(), to_iso(now - GAME_RETENTION)),
    )
    for row in rows:
        game_id = row["id"]
        played = row["created_at"] if row["exchange_date"] is None else _midnight(row["exchange_date"])
        await db.execute(
            "UPDATE users SET last_game_at = MAX(COALESCE(last_game_at, ''), ?) WHERE user_id IN ("
            " SELECT user_id FROM participants WHERE game_id = ? UNION SELECT organizer_id FROM games WHERE id = ?)",
            (played, game_id, game_id),
        )
        await db.execute("DELETE FROM reports WHERE game_id = ?", (game_id,))
        await db.execute("DELETE FROM outbox WHERE game_id = ?", (game_id,))
        await db.execute("DELETE FROM games WHERE id = ?", (game_id,))
    return len(rows)


async def _delete_users(db: Db, now: datetime) -> int:
    bounds = {
        "unconsented": to_iso(now - UNCONSENTED_RETENTION),
        "inactive": to_iso(now - INACTIVE_USER_RETENTION),
    }
    await db.execute(f"UPDATE events SET user_id = NULL WHERE user_id IN ({_DELETABLE_USERS})", bounds)
    await db.execute(f"UPDATE payments SET payer_id = NULL WHERE payer_id IN ({_DELETABLE_USERS})", bounds)
    deleted = await db.execute(f"DELETE FROM users WHERE user_id IN ({_DELETABLE_USERS})", bounds)
    return deleted.rowcount


def _midnight(day: str) -> str:
    """'2026-12-25' as a stored timestamp, comparable with consent_at."""
    return to_iso(datetime.combine(date.fromisoformat(day), time(), tzinfo=timezone.utc))
