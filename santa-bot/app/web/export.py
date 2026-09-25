"""P1 CSV export for the owner (§8): payments, games and events. Never wishes or relay texts.

Opened in Excel, so the file starts with a BOM (added by the route) and any cell that
begins with '=', '+', '-', '@', tab or CR is prefixed with an apostrophe (formula injection).
"""

from __future__ import annotations

import csv
import io
from typing import Any

from app.db import Db

_GAME_COUNT = "(SELECT COUNT(*) FROM participants p WHERE p.game_id = g.id AND p.status = '{}')"
COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "payments": tuple((name, name) for name in (
        "inv_id", "game_id", "payer_id", "tier", "amount_rub", "status", "provider", "created_at", "paid_at")),
    "games": (
        ("id", "g.id"), ("code", "g.code"), ("title", "g.title"), ("organizer_id", "g.organizer_id"),
        ("status", "g.status"), ("tier", "g.tier"), ("participant_limit", "g.participant_limit"),
        ("active", _GAME_COUNT.format("active")), ("waiting", _GAME_COUNT.format("waiting")),
        ("source", "g.source"), ("source_game_id", "g.source_game_id"), ("exchange_date", "g.exchange_date"),
        ("created_at", "g.created_at"), ("drawn_at", "g.drawn_at"), ("finished_at", "g.finished_at"),
        ("cancelled_at", "g.cancelled_at"),
    ),
    "events": tuple((name, name) for name in ("id", "ts", "type", "user_id", "game_id", "props")),
}
_FROM = {"payments": "payments ORDER BY inv_id", "games": "games g ORDER BY g.id", "events": "events ORDER BY id"}
TABLES = frozenset(COLUMNS)
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


async def to_csv(db: Db, table: str) -> str:
    """The whole table as CSV text with a header row; ``table`` must be one of ``TABLES``."""
    columns = COLUMNS[table]
    rows = await db.fetchall(f"SELECT {', '.join(sql for _, sql in columns)} FROM {_FROM[table]}")
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([name for name, _ in columns])
    writer.writerows([[_cell(value) for value in row] for row in rows])
    return buffer.getvalue()


def _cell(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value
