"""Connection setup and migration runner.

Migrations are plain numbered .sql files in migrations/, applied in order.
Progress is tracked with PRAGMA user_version: a fresh database is at 0, and
after applying migrations/0001_*.sql the version becomes 1, etc. Re-running
against an up-to-date database is a no-op.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(db_path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply any migrations not yet applied. Returns the resulting version."""
    files = _migration_files()
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]

    for index, path in enumerate(files, start=1):
        if index <= current_version:
            continue
        sql = path.read_text()
        conn.executescript(sql)
        conn.execute(f"PRAGMA user_version = {index}")
        conn.commit()
        current_version = index

    return current_version
