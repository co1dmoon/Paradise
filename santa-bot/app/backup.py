"""Nightly SQLite backups (§10): an online copy to /data/backups/santa-YYYYMMDD.db, 14 kept.

The copy is written under a temporary name and renamed when complete, so a crash
never leaves a half-written file that looks like a backup. Backups hold personal
data: the files are readable by the app user only.
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

from app.db import Database

KEEP = 14
_NAME = re.compile(r"^santa-\d{8}\.db$")


def backup_name(day: date) -> str:
    return f"santa-{day:%Y%m%d}.db"


async def make_backup(db: Database, directory: Path, day: date) -> Path:
    """Copy the live database into ``directory`` (one file per day, replaced if it exists)."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / backup_name(day)
    partial = target.with_name(target.name + ".partial")
    partial.unlink(missing_ok=True)
    await db.backup_to(partial)
    partial.chmod(0o600)
    os.replace(partial, target)
    return target


def rotate(directory: Path, keep: int = KEEP) -> list[Path]:
    """Delete all but the ``keep`` newest backups; returns the deleted files."""
    backups = sorted(path for path in directory.iterdir() if _NAME.match(path.name))
    stale = backups[:-keep] if len(backups) > keep else []
    for path in stale:
        path.unlink()
    return stale
