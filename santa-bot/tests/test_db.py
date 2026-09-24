from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from app import repo
from app.config import Config
from app.core.clock import FakeClock
from app.core.models import StateKind
from app.db import Database


async def test_pragmas_and_idempotent_migrations(db: Database) -> None:
    assert await db.fetchval("PRAGMA journal_mode") == "wal"
    assert await db.fetchval("PRAGMA foreign_keys") == 1
    assert await db.migrate() == []
    tables = {row[0] for row in await db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"users", "user_state", "games", "participants", "exclusions", "assignments", "payments",
            "relay_messages", "reports", "events", "settings", "outbox", "processed_updates"} <= tables


async def test_foreign_keys_are_enforced(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO participants (game_id, user_id, display_name, status, joined_at, via)"
            " VALUES (999, 1, 'x', 'active', 'now', 'link')"
        )


async def test_settings_seeded_once_then_db_wins(db: Database, config: Config) -> None:
    await repo.set_setting(db, "price_S", 590)
    await repo.seed_settings(db, replace(config.default_settings, price_S=100))
    settings = await repo.get_settings(db)
    assert settings.price_S == 590 and settings.limit_L == 300 and settings.maintenance is False
    await repo.set_setting(db, "maintenance", True)
    assert (await repo.get_settings(db)).maintenance is True
    with pytest.raises(ValueError):
        await repo.set_setting(db, "price_XL", 1)


async def test_transaction_rolls_back_and_nests(db: Database, clock: FakeClock) -> None:
    with pytest.raises(RuntimeError):
        async with db.transaction() as tx:
            await repo.insert_user(tx, 1, "direct", clock.now())
            async with db.transaction():
                await repo.insert_user(db, 2, "direct", clock.now())
            raise RuntimeError("boom")
    assert await repo.get_user(db, 1) is None and await repo.get_user(db, 2) is None


async def test_transactions_do_not_interleave(db: Database, clock: FakeClock) -> None:
    async def slow_writer() -> None:
        async with db.transaction() as tx:
            await repo.insert_user(tx, 10, "direct", clock.now())
            await asyncio.sleep(0.01)
            raise RuntimeError("rollback")

    async def other_writer() -> None:
        await asyncio.sleep(0)
        await repo.insert_user(db, 11, "direct", clock.now())

    results = await asyncio.gather(slow_writer(), other_writer(), return_exceptions=True)
    assert isinstance(results[0], RuntimeError)
    assert await repo.get_user(db, 10) is None
    assert await repo.get_user(db, 11) is not None, "a concurrent statement is not rolled back with the other task"


async def test_user_state_expires(db: Database, clock: FakeClock) -> None:
    await repo.insert_user(db, 5, "direct", clock.now())
    await repo.set_state(db, 5, StateKind.RESUME, clock.now(), data={"payload": "j_ABC234"})
    state = await repo.get_state(db, 5, clock.now())
    assert state is not None and state.kind == StateKind.RESUME and state.data == {"payload": "j_ABC234"}
    clock.advance(31 * 60)
    assert await repo.get_state(db, 5, clock.now()) is None
    assert await db.fetchval("SELECT COUNT(*) FROM user_state") == 0


async def test_processed_updates_dedupe_and_prune(db: Database, clock: FakeClock) -> None:
    assert await repo.mark_update_processed(db, "cb.1", clock.now())
    assert not await repo.mark_update_processed(db, "cb.1", clock.now())
    clock.advance(4 * 86400)
    assert await repo.prune_processed_updates(db, clock.now()) == 1


async def test_backup(db: Database, tmp_path: Path, clock: FakeClock) -> None:
    await repo.insert_user(db, 1, "direct", clock.now())
    target = tmp_path / "backup.db"
    await db.backup_to(target)
    copy = await Database.open(target)
    assert await repo.get_user(copy, 1) is not None
    await copy.close()
