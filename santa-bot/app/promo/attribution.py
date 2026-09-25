"""Per-source metrics from the bot's own database (PROMO_SPEC §2).

A source S is what the landing page or a deep link put into ``users.first_source`` as
's:S': yd<campaignId>, vk<adPlanId>, ch<NN> (own channel posts) or p<slug> (manual links).
For each source:

- users: people whose first touch carried S. Counted from the ``user_first_seen`` events,
  which keep the source after the nightly retention deletes people who never consented;
- organizers: those people who organized at least one game;
- games: the games they organized (whatever source the game itself has);
- games3: those games that reached 3 active participants. The participants table keeps
  only the current status, so "reached at any point" is read as: the game was drawn (a
  draw needs 3 active participants and ``drawn_at`` survives later departures,
  finishing and cancellation) or it has 3+ active participants now. A game that had 3
  and lost some before a draw is not counted — the undercount errs on the cautious side;
- games3_matured: games3 counting only organizers first seen before the cohort cutoff;
- paid_rub: paid and granted payments of those games (refunds excluded);
- downstream_games: games created from one of those games (``source_game_id``, one level).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.clock import to_iso
from app.db import Db

_GAME_REACHED_THREE = (
    "(g.drawn_at IS NOT NULL OR (SELECT COUNT(*) FROM participants p"
    " WHERE p.game_id = g.id AND p.status = 'active') >= 3)"
)


@dataclass(frozen=True, slots=True)
class SourceMetrics:
    users: int = 0
    organizers: int = 0
    games: int = 0
    games3: int = 0
    games3_matured: int = 0
    paid_rub: int = 0
    downstream_games: int = 0


def cohort_cutoff(today: date, lag_days: int, tz: ZoneInfo) -> datetime:
    """Today 00:00 in Moscow minus PROMO_LAG_DAYS: newer people and days are not matured yet."""
    return datetime.combine(today - timedelta(days=lag_days), time(), tzinfo=tz)


def total(metrics: Iterable[SourceMetrics]) -> SourceMetrics:
    """The sum over several sources (sources never share a person: first_source is set once)."""
    items = list(metrics)
    return SourceMetrics(**{f.name: sum(getattr(m, f.name) for m in items) for f in fields(SourceMetrics)})


async def source_metrics(db: Db, sources: Sequence[str], cutoff: datetime) -> dict[str, SourceMetrics]:
    """Metrics for each source (without the 's:' prefix); sources without data get zeros."""
    if not sources:
        return {}
    tags = [f"s:{source}" for source in sources]
    marks = ", ".join("?" for _ in tags)
    users = await _counts(
        db,
        "SELECT json_extract(props, '$.source') AS tag, COUNT(*) AS n FROM events"
        f" WHERE type = 'user_first_seen' AND json_extract(props, '$.source') IN ({marks}) GROUP BY tag",
        tags,
    )
    games = {
        row["tag"]: row
        for row in await db.fetchall(
            "SELECT u.first_source AS tag, COUNT(DISTINCT g.organizer_id) AS organizers, COUNT(*) AS games,"
            f" SUM({_GAME_REACHED_THREE}) AS games3,"
            f" SUM(u.first_seen_at < ? AND {_GAME_REACHED_THREE}) AS games3_matured"
            f" FROM users u JOIN games g ON g.organizer_id = u.user_id WHERE u.first_source IN ({marks})"
            " GROUP BY u.first_source",
            (to_iso(cutoff), *tags),
        )
    }
    paid = await _counts(
        db,
        "SELECT u.first_source AS tag, SUM(pay.amount_rub) AS n FROM users u"
        " JOIN games g ON g.organizer_id = u.user_id"
        " JOIN payments pay ON pay.game_id = g.id AND pay.status IN ('paid', 'granted')"
        f" WHERE u.first_source IN ({marks}) GROUP BY u.first_source",
        tags,
    )
    downstream = await _counts(
        db,
        "SELECT u.first_source AS tag, COUNT(*) AS n FROM users u"
        " JOIN games g ON g.organizer_id = u.user_id JOIN games child ON child.source_game_id = g.id"
        f" WHERE u.first_source IN ({marks}) GROUP BY u.first_source",
        tags,
    )
    result = {}
    for source, tag in zip(sources, tags, strict=True):
        row = games.get(tag)
        result[source] = SourceMetrics(
            users=users.get(tag, 0),
            organizers=int(row["organizers"]) if row else 0,
            games=int(row["games"]) if row else 0,
            games3=int(row["games3"] or 0) if row else 0,
            games3_matured=int(row["games3_matured"] or 0) if row else 0,
            paid_rub=paid.get(tag, 0),
            downstream_games=downstream.get(tag, 0),
        )
    return result


async def _counts(db: Db, sql: str, params: Sequence[str]) -> dict[str, int]:
    return {row["tag"]: int(row["n"] or 0) for row in await db.fetchall(sql, tuple(params))}
