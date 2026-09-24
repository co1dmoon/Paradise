"""Event recording (§6.9) and the numbers behind /stats and the digest (§9).

Events never carry wishes, relay texts or names; ``props`` hold small numbers and tags.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from app.core.clock import to_iso
from app.db import Db


class Event(StrEnum):
    USER_FIRST_SEEN = "user_first_seen"
    CONSENT = "consent"
    GAME_CREATED = "game_created"
    INVITE_OPEN = "invite_open"
    JOIN = "join"
    JOIN_WAITING = "join_waiting"
    WISHES_SAVED = "wishes_saved"
    DRAW_DONE = "draw_done"
    RESULT_DM_FAILED = "result_dm_failed"
    RELAY_SENT = "relay_sent"
    REPORT = "report"
    PAY_CLICK = "pay_click"
    PAY_SUCCESS = "pay_success"
    REF_CLICK = "ref_click"
    GROUP_ADDED = "group_added"
    REVEAL = "reveal"


async def record(
    db: Db,
    event: Event,
    now: datetime,
    *,
    user_id: int | None = None,
    game_id: int | None = None,
    **props: str | int | float | bool | None,
) -> None:
    await db.execute(
        "INSERT INTO events (ts, type, user_id, game_id, props) VALUES (?, ?, ?, ?, ?)",
        (to_iso(now), event, user_id, game_id, json.dumps(props, ensure_ascii=False)),
    )


@dataclass(frozen=True, slots=True)
class Period:
    label: str
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class StatsBlock:
    new_users: int
    consents: int
    games_created: int
    games_3plus: int
    draws: int
    draw_size_avg: float | None
    draw_size_median: float | None
    games_hit_limit: int
    paid_games: int
    revenue_rub: int
    conversion: float | None
    participant_to_organizer: float | None
    ref_games: int
    result_dm_failure_rate: float | None


@dataclass(frozen=True, slots=True)
class StatsReport:
    blocks: tuple[tuple[Period, StatsBlock], ...]
    open_reports: int
    organizer_sources: tuple[tuple[str, int], ...]


def season_start(today: date) -> date:
    """The season starts on September 1 (of last year until September)."""
    return date(today.year if today.month >= 9 else today.year - 1, 9, 1)


def stats_periods(now: datetime, tz: ZoneInfo) -> list[Period]:
    """Today, the last 7 calendar days and the season, all ending at the next local midnight."""
    today = now.astimezone(tz).date()
    end = datetime.combine(today + timedelta(days=1), time(), tzinfo=tz)
    return [
        Period("Сегодня", end - timedelta(days=1), end),
        Period("7 дней", end - timedelta(days=7), end),
        Period("Сезон", datetime.combine(season_start(today), time(), tzinfo=tz), end),
    ]


async def build_stats_report(db: Db, now: datetime, tz: ZoneInfo) -> StatsReport:
    periods = stats_periods(now, tz)
    week = periods[1]
    blocks = tuple([(period, await collect_stats(db, period.start, period.end)) for period in periods])
    return StatsReport(
        blocks=blocks,
        open_reports=await open_reports(db),
        organizer_sources=tuple(await organizer_sources(db, week.start, week.end)),
    )


async def collect_stats(db: Db, start: datetime, end: datetime) -> StatsBlock:
    span = (to_iso(start), to_iso(end))
    draw_sizes = [
        int(row[0])
        for row in await db.fetchall(
            "SELECT json_extract(props, '$.n') FROM events"
            " WHERE type = 'draw_done' AND ts >= ? AND ts < ?",
            span,
        )
        if row[0] is not None
    ]
    games_hit_limit = await _count_events(db, Event.JOIN_WAITING, start, end, distinct_games=True)
    paid_games, revenue = await db.fetchone(
        "SELECT COUNT(DISTINCT COALESCE(game_id, -inv_id)), COALESCE(SUM(amount_rub), 0) FROM payments"
        " WHERE status IN ('paid', 'granted') AND amount_rub > 0 AND paid_at >= ? AND paid_at < ?",
        span,
    ) or (0, 0)
    failed_results = await _count_events(db, Event.RESULT_DM_FAILED, start, end)
    return StatsBlock(
        new_users=await _count_events(db, Event.USER_FIRST_SEEN, start, end),
        consents=await _count_events(db, Event.CONSENT, start, end),
        games_created=await _count_events(db, Event.GAME_CREATED, start, end),
        games_3plus=await _games_with_three(db, span),
        draws=len(draw_sizes),
        draw_size_avg=statistics.fmean(draw_sizes) if draw_sizes else None,
        draw_size_median=statistics.median(draw_sizes) if draw_sizes else None,
        games_hit_limit=games_hit_limit,
        paid_games=int(paid_games),
        revenue_rub=int(revenue),
        conversion=_ratio(int(paid_games), games_hit_limit),
        participant_to_organizer=await _participant_to_organizer(db, span),
        ref_games=int(
            await db.fetchval(
                "SELECT COUNT(*) FROM events WHERE type = 'game_created' AND ts >= ? AND ts < ?"
                " AND json_extract(props, '$.source') = 'ref'",
                span,
            )
        ),
        result_dm_failure_rate=_ratio(failed_results, sum(draw_sizes)),
    )


async def open_reports(db: Db) -> int:
    return int(await db.fetchval("SELECT COUNT(*) FROM reports WHERE resolved = 0"))


async def organizer_sources(db: Db, start: datetime, end: datetime) -> list[tuple[str, int]]:
    """Where new games came from: games.source values with counts, most frequent first."""
    rows = await db.fetchall(
        "SELECT COALESCE(json_extract(props, '$.source'), 'direct') AS source, COUNT(*) AS n FROM events"
        " WHERE type = 'game_created' AND ts >= ? AND ts < ? GROUP BY source ORDER BY n DESC, source",
        (to_iso(start), to_iso(end)),
    )
    return [(row["source"], int(row["n"])) for row in rows]


async def _count_events(
    db: Db, event: Event, start: datetime, end: datetime, *, distinct_games: bool = False
) -> int:
    what = "COUNT(DISTINCT game_id)" if distinct_games else "COUNT(*)"
    return int(
        await db.fetchval(
            f"SELECT {what} FROM events WHERE type = ? AND ts >= ? AND ts < ?",
            (event, to_iso(start), to_iso(end)),
        )
    )


async def _games_with_three(db: Db, span: tuple[str, str]) -> int:
    return int(
        await db.fetchval(
            "SELECT COUNT(*) FROM games g WHERE g.created_at >= ? AND g.created_at < ? AND"
            " (SELECT COUNT(*) FROM participants p WHERE p.game_id = g.id AND p.status = 'active') >= 3",
            span,
        )
    )


async def _participant_to_organizer(db: Db, span: tuple[str, str]) -> float | None:
    """Distinct users who organized a game after first joining another game / distinct participants."""
    participants = int(
        await db.fetchval(
            "SELECT COUNT(DISTINCT user_id) FROM participants"
            " WHERE via <> 'organizer' AND joined_at >= ? AND joined_at < ?",
            span,
        )
    )
    converted = int(
        await db.fetchval(
            "SELECT COUNT(DISTINCT g.organizer_id) FROM games g"
            " WHERE g.created_at >= ? AND g.created_at < ? AND EXISTS ("
            "   SELECT 1 FROM participants p JOIN games other ON other.id = p.game_id"
            "   WHERE p.user_id = g.organizer_id AND other.organizer_id <> g.organizer_id"
            "   AND p.joined_at < g.created_at)",
            span,
        )
    )
    return _ratio(converted, participants)


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None
