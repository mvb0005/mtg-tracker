"""Connection setup and migration runner.

Migrations are plain numbered .sql files in migrations/, applied in order.
Progress is tracked with PRAGMA user_version: a fresh database is at 0, and
after applying migrations/0001_*.sql the version becomes 1, etc. Re-running
against an up-to-date database is a no-op.

Write log (migration 0009): every table gets generated AFTER INSERT/UPDATE/
DELETE triggers, named cl_<table>_<i|u|d>, that append the row's old and new
values as JSON to `changelog`. They are not hand-written: migrate() drops
them before applying a migration (so a table rebuild isn't logged row by
row) and regenerates them from PRAGMA table_info afterwards, so they always
match the current columns. Never edit or drop cl_% triggers by hand.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

CHANGELOG_VERSION = 9  # the migration that creates changelog
_NOT_LOGGED = frozenset({"changelog", "changelog_context"})
_DML = re.compile(r"\s*(?:/\*.*?\*/\s*)*(insert|update|delete|replace)\b", re.I | re.S)


_ARM = "UPDATE changelog_context SET batch_id = ? WHERE id = 1"
_CLEAR = "UPDATE changelog_context SET batch_id = NULL WHERE id = 1 AND batch_id IS NOT NULL"


class Connection(sqlite3.Connection):
    """A connection that can tag its writes with a batch id.

    The change-log triggers read the batch id from the one-row table
    changelog_context (a TEMP table is invisible to triggers in the main
    schema, and an application function would break every other client of
    this file, e.g. the sqlite3 shell or the previous release during a
    rolling update). Because that row is shared, it is armed only inside the
    transaction that writes (before the first INSERT/UPDATE/DELETE after a
    commit or rollback) and cleared in the same transaction just before it
    commits, so it can never leak into another connection's writes.

    set_batch(conn, id) fixes the id; set_batch_provider(conn, fn) calls
    fn(conn) when the first write of a transaction is about to happen (the
    web layer uses it to create the request's audit row inside that
    transaction, so a rolled-back request leaves no audit row either)."""

    _batch: int | None = None
    _provider = None
    _busy = False
    _armed = False  # the context row holds our batch id in the current transaction

    def _arm(self, sql) -> None:
        if self._busy or (self._armed and self.in_transaction):
            return
        if not isinstance(sql, str) or not _DML.match(sql):
            return
        self._busy = True
        try:
            batch = self._batch
            if self._provider is not None:
                batch = self._provider(self)
            if batch is not None:
                super().execute(_ARM, (batch,))
                self._armed = True
        finally:
            self._busy = False

    def execute(self, sql, *args):
        if self._batch is not None or self._provider is not None:
            self._arm(sql)
        return super().execute(sql, *args)

    def executemany(self, sql, *args):
        if self._batch is not None or self._provider is not None:
            self._arm(sql)
        return super().executemany(sql, *args)

    def _disarm(self) -> None:
        if self._armed and self.in_transaction:
            super().execute(_CLEAR)  # in the transaction about to commit
        self._armed = False

    def commit(self):
        self._disarm()
        return super().commit()

    def rollback(self):
        self._armed = False  # the arming write rolls back with everything else
        return super().rollback()

    def __exit__(self, exc_type, exc, tb):  # `with conn:` doesn't go through commit()
        if exc_type is None:
            self._disarm()
        else:
            self._armed = False
        return super().__exit__(exc_type, exc, tb)


def connect(db_path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread, factory=Connection)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def set_batch(conn: sqlite3.Connection, batch_id: int | None) -> None:
    """Tag this connection's logged writes with an audit_log id (None: untagged).
    Takes effect from the next write (an already open transaction is armed
    by its next write too); it never outlives a commit."""
    conn._batch = batch_id  # type: ignore[attr-defined]


def set_batch_provider(conn: sqlite3.Connection, provider) -> None:
    conn._provider = provider  # type: ignore[attr-defined]


def _migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)


# --- generated change-log triggers -----------------------------------------

def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _logged_tables(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
            " ORDER BY name"
        ) if r[0] not in _NOT_LOGGED
    ]


def _json_obj(prefix: str, cols: list[str]) -> str:
    quoted = [f"'{c.replace(chr(39), chr(39) * 2)}', {prefix}.{_q(c)}" for c in cols]
    return "json_object(" + ", ".join(quoted) + ")"


def _log_insert(table: str, op: str, row_id: str, before: str, after: str) -> str:
    return (
        "INSERT INTO changelog (at, batch_id, tbl, op, row_id, before_json, after_json) VALUES "
        f"(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), (SELECT batch_id FROM changelog_context WHERE id = 1),"
        f" '{table}', '{op}', {row_id}, {before}, {after});"
    )


def _expected_triggers(conn: sqlite3.Connection) -> dict[str, str]:
    out: dict[str, str] = {}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'changelog'").fetchone() is None:
        return out
    for table in _logged_tables(conn):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({_q(table)})")]
        for suffix, event, op, row_id, before, after in (
            ("i", "INSERT", "I", "NEW.rowid", False, True),
            ("u", "UPDATE", "U", "OLD.rowid", True, True),
            ("d", "DELETE", "D", "OLD.rowid", True, False),
        ):
            out[f"cl_{table}_{suffix}"] = (
                f"CREATE TRIGGER {_q(f'cl_{table}_{suffix}')} AFTER {event} ON {_q(table)} BEGIN "
                + _log_insert(
                    table, op, row_id, _json_obj("OLD", cols) if before else "NULL",
                    _json_obj("NEW", cols) if after else "NULL",
                )
                + " END"
            )
    return out


def drop_changelog_triggers(conn: sqlite3.Connection) -> None:
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'cl\\_%' ESCAPE '\\'"
    ).fetchall():
        conn.execute(f"DROP TRIGGER {_q(name)}")


def _current_triggers(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'cl\\_%' ESCAPE '\\'"
    ).fetchall())


def ensure_changelog_triggers(conn: sqlite3.Connection) -> bool:
    """Make the cl_% triggers exactly what the current tables need. A cheap
    comparison of the stored trigger SQL; only when something differs are
    they all dropped and recreated (in one transaction). Returns whether it
    changed anything."""
    if _expected_triggers(conn) == _current_triggers(conn):
        return False
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        want = _expected_triggers(conn)  # re-read under the write lock (another process may have done it)
        if want != _current_triggers(conn):
            drop_changelog_triggers(conn)
            for sql in want.values():
                conn.execute(sql)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return True


def migrate(conn: sqlite3.Connection, target_version: int | None = None, *, log: bool = True) -> int:
    """Apply any migrations not yet applied (up to target_version, if given).
    Returns the resulting version.

    The change-log triggers are dropped before a migration runs and
    regenerated after, with one 'S' row per migration applied. log=False
    (used by recovery replay) skips regenerating them and the 'S' rows."""
    files = _migration_files()
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    pending = [
        (i, p) for i, p in enumerate(files, start=1)
        if i > current_version and (target_version is None or i <= target_version)
    ]
    applied: list[int] = []
    if pending:
        drop_changelog_triggers(conn)
        conn.commit()
    try:
        for index, path in pending:
            conn.executescript(path.read_text())
            conn.execute(f"PRAGMA user_version = {index}")
            conn.commit()
            current_version = index
            applied.append(index)
    finally:
        if log and pending:
            _finish_logging(conn, applied)
    if log and not pending:
        ensure_changelog_triggers(conn)
    return current_version


def _finish_logging(conn: sqlite3.Connection, applied: list[int]) -> None:
    ensure_changelog_triggers(conn)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'changelog'").fetchone() is None:
        return
    for version in applied:
        if version >= CHANGELOG_VERSION:
            conn.execute(
                "INSERT INTO changelog (at, tbl, op, after_json)"
                " VALUES (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), '', 'S', json_object('user_version', ?))",
                (version,),
            )
    conn.commit()
