"""Recovery from the write log: rebuild the database as of a moment by
replaying `changelog` rows on top of a restore point (mtgtracker/restore.py),
and undo one web request's changes by its audit id.

recover() never touches the live database: it reads the log from it
read-only and writes only the new copy at `out_path`.
"""

from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from mtgtracker import db

_q = db._q


class RecoverError(Exception):
    pass


@dataclass
class Summary:
    applied: int = 0
    final_seq: int = 0
    point_seq: int = 0
    fk_problems: list[tuple] = field(default_factory=list)
    counts: dict[str, tuple[int, int | None]] = field(default_factory=dict)  # table -> (copy, live)


def _rowid_alias(conn: sqlite3.Connection, table: str) -> str | None:
    """The column that IS the rowid (a lone INTEGER PRIMARY KEY), if any."""
    pk = [r for r in conn.execute(f"PRAGMA table_info({_q(table)})") if r["pk"]]
    return pk[0]["name"] if len(pk) == 1 and pk[0]["type"].upper() == "INTEGER" else None


def _insert(conn: sqlite3.Connection, table: str, row_id: int, values: dict) -> None:
    cols = dict(values)
    if _rowid_alias(conn, table) not in cols:
        cols = {"rowid": row_id, **cols}
    names = ", ".join(_q(c) for c in cols)
    conn.execute(
        f"INSERT OR REPLACE INTO {_q(table)} ({names}) VALUES ({', '.join('?' * len(cols))})",
        list(cols.values()),
    )


def _update(conn: sqlite3.Connection, table: str, row_id: int, values: dict) -> None:
    sets = ", ".join(f"{_q(c)} = ?" for c in values)
    conn.execute(f"UPDATE {_q(table)} SET {sets} WHERE rowid = ?", [*values.values(), row_id])


def _delete(conn: sqlite3.Connection, table: str, row_id: int) -> None:
    conn.execute(f"DELETE FROM {_q(table)} WHERE rowid = ?", (row_id,))


def _iso(value: str) -> str:
    """Any ISO-8601 time -> the log's format (UTC, milliseconds, Z)."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond // 1000:03d}Z"


def _log_rows(
    log: sqlite3.Connection, after_seq: int, until_seq, until_time, until_audit
) -> list[sqlite3.Row]:
    sql, args = "SELECT * FROM changelog WHERE seq > ?", [after_seq]
    if until_seq is not None:
        sql += " AND seq <= ?"
        args.append(until_seq)
    if until_time is not None:
        sql += " AND at <= ?"
        args.append(_iso(until_time))
    if until_audit is not None:
        # Just before the request's first row: its changes, and its audit row.
        first = log.execute(
            "SELECT MIN(seq) FROM changelog WHERE batch_id = ?"
            " OR (tbl = 'audit_log' AND op = 'I' AND row_id = ?)", (until_audit, until_audit),
        ).fetchone()[0]
        if first is None:
            raise RecoverError(f"the log has nothing for audit id {until_audit}")
        sql += " AND seq < ?"
        args.append(first)
    return log.execute(sql + " ORDER BY seq", args).fetchall()


def recover(
    point_path: str | Path, log_db_path: str | Path, out_path: str | Path, *,
    until_seq: int | None = None, until_time: str | None = None, until_audit: int | None = None,
) -> Summary:
    point_path, log_db_path, out_path = Path(point_path), Path(log_db_path), Path(out_path)
    if out_path.exists():
        raise RecoverError(f"{out_path} already exists; recovery writes a new file")
    if out_path.resolve() == log_db_path.resolve():
        raise RecoverError("refusing to write over the live database")
    if sum(x is not None for x in (until_seq, until_time, until_audit)) > 1:
        raise RecoverError("give at most one of until_seq, until_time, until_audit")
    if not log_db_path.exists():
        raise RecoverError(f"no database at {log_db_path}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with gzip.open(point_path, "rb") as f_in, open(out_path, "xb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        return _replay(out_path, log_db_path, until_seq, until_time, until_audit)
    except BaseException:
        out_path.unlink(missing_ok=True)
        raise


def _replay(out_path: Path, log_db_path: Path, until_seq, until_time, until_audit) -> Summary:
    copy = db.connect(out_path)
    copy.execute("PRAGMA foreign_keys = OFF")
    log = sqlite3.connect(f"{log_db_path.resolve().as_uri()}?mode=ro", uri=True)
    log.row_factory = sqlite3.Row
    summary = Summary()
    try:
        has_log = copy.execute("SELECT 1 FROM sqlite_master WHERE name = 'changelog'").fetchone()
        if has_log:
            summary.point_seq = copy.execute("SELECT COALESCE(MAX(seq), 0) FROM changelog").fetchone()[0]
        if summary.point_seq:
            # The log must be the point's own timeline (a restore elsewhere can reuse seq numbers).
            mine = copy.execute("SELECT * FROM changelog WHERE seq = ?", (summary.point_seq,)).fetchone()
            theirs = log.execute("SELECT * FROM changelog WHERE seq = ?", (summary.point_seq,)).fetchone()
            if theirs is None or tuple(mine) != tuple(theirs):
                raise RecoverError(
                    f"{log_db_path} does not continue this restore point (seq {summary.point_seq} differs)"
                )
        rows = _log_rows(log, summary.point_seq, until_seq, until_time, until_audit)
        db.drop_changelog_triggers(copy)
        copy.commit()
        for row in rows:
            op, table = row["op"], row["tbl"]
            if op == "S":
                db.migrate(copy, json.loads(row["after_json"])["user_version"], log=False)
                if copy.in_transaction:
                    copy.commit()
                continue
            if op == "I":
                _insert(copy, table, row["row_id"], json.loads(row["after_json"]))
            elif op == "U":
                _update(copy, table, row["row_id"], json.loads(row["after_json"]))
            else:
                _delete(copy, table, row["row_id"])
            summary.applied += 1
        # The copy's own log now matches the live one up to this point.
        for row in rows:
            copy.execute(
                "INSERT INTO changelog (seq, at, batch_id, tbl, op, row_id, before_json, after_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", tuple(row),
            )
        copy.execute("UPDATE changelog_context SET batch_id = NULL WHERE id = 1")
        summary.final_seq = copy.execute("SELECT COALESCE(MAX(seq), 0) FROM changelog").fetchone()[0]
        copy.commit()
        db.ensure_changelog_triggers(copy)
        summary.fk_problems = [tuple(r) for r in copy.execute("PRAGMA foreign_key_check")]
        live_tables = {r[0] for r in log.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table in [r[0] for r in copy.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
            " ORDER BY name"
        )]:
            count = f"SELECT COUNT(*) FROM {_q(table)}"
            live = log.execute(count).fetchone()[0] if table in live_tables else None
            summary.counts[table] = (copy.execute(count).fetchone()[0], live)
    finally:
        log.close()
        copy.close()
    return summary


# --- undo one request -------------------------------------------------------

def undo_batch(conn: sqlite3.Connection, audit_id: int, *, commit: bool = True) -> list[dict]:
    """Reverse exactly the changes logged under `audit_id`, newest first
    (insert -> delete, update -> restore the old values, delete -> re-insert).
    Refuses if a later change touched any row the batch touched, since
    undoing would silently overwrite it. Returns the preview of what it
    did. With commit=False the caller owns the transaction (a dry run rolls
    back); the undo is itself logged like any write."""
    rows = conn.execute(
        "SELECT * FROM changelog WHERE batch_id = ? AND op != 'S' ORDER BY seq DESC", (audit_id,)
    ).fetchall()
    if not rows:
        raise RecoverError(f"no logged changes for audit id {audit_id}")
    last: dict[tuple, int] = {}
    for row in rows:
        last.setdefault((row["tbl"], row["row_id"]), row["seq"])
    for (table, row_id), seq in last.items():
        later = conn.execute(
            "SELECT seq, batch_id FROM changelog WHERE tbl = ? AND row_id = ? AND seq > ?"
            " AND batch_id IS NOT ? ORDER BY seq LIMIT 1", (table, row_id, seq, audit_id),
        ).fetchone()
        if later:
            raise RecoverError(
                f"cannot undo audit {audit_id}: {table} row {row_id} was changed again later"
                f" (log seq {later['seq']}, audit {later['batch_id'] or 'none'})"
            )
    preview = []
    for row in rows:
        table, row_id, op = row["tbl"], row["row_id"], row["op"]
        if op == "I":
            _delete(conn, table, row_id)
            inverse = "delete"
        elif op == "U":
            _update(conn, table, row_id, json.loads(row["before_json"]))
            inverse = "restore"
        else:
            _insert(conn, table, row_id, json.loads(row["before_json"]))
            inverse = "re-insert"
        preview.append({"seq": row["seq"], "table": table, "row_id": row_id, "undo": inverse})
    if commit:
        conn.commit()
    return preview
