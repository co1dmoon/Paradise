"""SQLite access: one aiosqlite connection, WAL, foreign keys ON, migrations at startup.

Concurrency model (one process, §11): all statements go through one connection
guarded by an asyncio lock, so a transaction opened by one task never mixes with
statements from another task. Inside ``async with db.transaction() as tx`` use
``tx`` (or keep calling ``db`` from the same task: calls are routed into the open
transaction). Nested ``transaction()`` calls join the outer transaction.

Every data-access function accepts a ``Db`` — either the ``Database`` or a ``Tx``.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import aiosqlite

from app.core.clock import to_iso

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
Params = Sequence[Any] | dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExecResult:
    lastrowid: int | None
    rowcount: int


class Db(Protocol):
    async def execute(self, sql: str, params: Params = ()) -> ExecResult: ...

    async def executemany(self, sql: str, rows: Iterable[Params]) -> None: ...

    async def fetchone(self, sql: str, params: Params = ()) -> sqlite3.Row | None: ...

    async def fetchall(self, sql: str, params: Params = ()) -> list[sqlite3.Row]: ...

    async def fetchval(self, sql: str, params: Params = ()) -> Any: ...

    def transaction(self) -> Any:
        """Async context manager yielding a ``Db`` bound to one transaction."""


class Tx:
    """Statements on the raw connection; the caller holds the database lock."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def execute(self, sql: str, params: Params = ()) -> ExecResult:
        cursor = await self._conn.execute(sql, params)
        try:
            return ExecResult(cursor.lastrowid, cursor.rowcount)
        finally:
            await cursor.close()

    async def executemany(self, sql: str, rows: Iterable[Params]) -> None:
        cursor = await self._conn.executemany(sql, rows)
        await cursor.close()

    async def fetchone(self, sql: str, params: Params = ()) -> sqlite3.Row | None:
        async with self._conn.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def fetchall(self, sql: str, params: Params = ()) -> list[sqlite3.Row]:
        async with self._conn.execute(sql, params) as cursor:
            return list(await cursor.fetchall())

    async def fetchval(self, sql: str, params: Params = ()) -> Any:
        row = await self.fetchone(sql, params)
        return None if row is None else row[0]

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Tx]:
        yield self


class Database:
    def __init__(self, conn: aiosqlite.Connection, path: str) -> None:
        self._conn = conn
        self._tx = Tx(conn)
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None
        self.path = path

    @classmethod
    async def open(cls, path: str | Path) -> Database:
        """Open (creating parent directories) with WAL, foreign keys ON and autocommit."""
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        for pragma in (
            "PRAGMA journal_mode=WAL",
            "PRAGMA foreign_keys=ON",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA busy_timeout=5000",
        ):
            await conn.execute(pragma)
        return cls(conn, str(path))

    async def close(self) -> None:
        await self._conn.close()

    def _in_own_transaction(self) -> bool:
        return self._owner is not None and self._owner is asyncio.current_task()

    @asynccontextmanager
    async def _locked(self) -> AsyncIterator[Tx]:
        if self._in_own_transaction():
            yield self._tx
            return
        async with self._lock:
            yield self._tx

    async def execute(self, sql: str, params: Params = ()) -> ExecResult:
        async with self._locked() as tx:
            return await tx.execute(sql, params)

    async def executemany(self, sql: str, rows: Iterable[Params]) -> None:
        async with self._locked() as tx:
            await tx.executemany(sql, rows)

    async def fetchone(self, sql: str, params: Params = ()) -> sqlite3.Row | None:
        async with self._locked() as tx:
            return await tx.fetchone(sql, params)

    async def fetchall(self, sql: str, params: Params = ()) -> list[sqlite3.Row]:
        async with self._locked() as tx:
            return await tx.fetchall(sql, params)

    async def fetchval(self, sql: str, params: Params = ()) -> Any:
        async with self._locked() as tx:
            return await tx.fetchval(sql, params)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Tx]:
        """BEGIN IMMEDIATE … COMMIT, rolled back on any exception."""
        if self._in_own_transaction():
            yield self._tx
            return
        async with self._lock:
            self._owner = asyncio.current_task()
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._tx
            except BaseException:
                await self._conn.execute("ROLLBACK")
                raise
            else:
                await self._conn.execute("COMMIT")
            finally:
                self._owner = None

    async def ping(self) -> bool:
        try:
            return await self.fetchval("SELECT 1") == 1
        except sqlite3.Error:
            log.exception("database ping failed")
            return False

    async def backup_to(self, target: str | Path) -> None:
        """Online backup (consistent snapshot) into ``target``, as one self-contained file (no WAL)."""
        async with self._lock:
            target_conn = await aiosqlite.connect(str(target))
            try:
                await self._conn.backup(target_conn)
                await target_conn.execute("PRAGMA journal_mode=DELETE")
            finally:
                await target_conn.close()

    async def migrate(self, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
        """Apply pending ``NNN_name.sql`` files in order, each atomically. Returns applied names."""
        async with self._lock:
            await self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            async with self._conn.execute("SELECT version FROM schema_migrations") as cursor:
                applied_versions = {row[0] for row in await cursor.fetchall()}
            applied: list[str] = []
            for path in sorted(migrations_dir.glob("[0-9][0-9][0-9]_*.sql")):
                version = int(path.name[:3])
                if version in applied_versions:
                    continue
                await self._apply(version, path)
                applied.append(path.name)
                log.info("migration applied", extra={"migration": path.name})
            return applied

    async def _apply(self, version: int, path: Path) -> None:
        name = path.name.replace("'", "")
        now = to_iso(datetime.now(timezone.utc))
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{path.read_text(encoding='utf-8')}\n;\n"
            "INSERT INTO schema_migrations (version, name, applied_at)"
            f" VALUES ({version}, '{name}', '{now}');\n"
            "COMMIT;"
        )
        try:
            await self._conn.executescript(script)
        except sqlite3.Error:
            if self._conn.in_transaction:
                await self._conn.execute("ROLLBACK")
            raise
