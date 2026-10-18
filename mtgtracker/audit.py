"""The audit log: one row per successful write request, saying who did it
(actor) and whose things it changed (principal; the same user for a person,
the person a system user acted for otherwise). Written by the identify
middleware (web/middleware.py), read on a person's page."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime


def record(
    conn: sqlite3.Connection, *, actor_id: int, principal_id: int, method: str, path: str,
    action: str, target: dict | None = None, summary: dict | None = None,
    restore_point: str | None = None, commit: bool = True,
) -> int:
    cur = conn.execute(
        "INSERT INTO audit_log (at, actor_id, principal_id, method, path, action, target,"
        " summary_json, restore_point) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.now(UTC).isoformat(), actor_id, principal_id, method, path, action,
            json.dumps(target, sort_keys=True) if target else None,
            json.dumps(summary, sort_keys=True) if summary else None,
            restore_point,
        ),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def recent(conn: sqlite3.Connection, principal_id: int, limit: int = 20) -> list[dict]:
    """Newest first, with names and the parsed summary (`notice` too)."""
    rows = conn.execute(
        "SELECT a.*, actor.display_name AS actor_name, actor.kind AS actor_kind,"
        " principal.display_name AS principal_name"
        " FROM audit_log a JOIN users actor ON actor.id = a.actor_id"
        " JOIN users principal ON principal.id = a.principal_id"
        " WHERE a.principal_id = ? ORDER BY a.at DESC, a.id DESC LIMIT ?",
        (principal_id, limit),
    ).fetchall()
    out = []
    for row in rows:
        entry = dict(row)
        summary = json.loads(row["summary_json"]) if row["summary_json"] else {}
        entry["summary"] = summary
        entry["notice"] = summary.get("notice", "")
        out.append(entry)
    return out
