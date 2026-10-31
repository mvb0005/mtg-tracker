"""The play_games history (migration 0013): one row per match, written when the play page starts it and
completed by the game server's result, matched by match_id."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

STATUSES = ("finished", "ended", "abandoned")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def record_start(
    conn: sqlite3.Connection,
    *,
    match_id: str,
    host_id: int,
    format: str,
    deck_id: int | None,
    deck_name: str,
    opponents: list[dict],
    ticket: str | None,
    commit: bool = True,
) -> int:
    cur = conn.execute(
        "INSERT INTO play_games (match_id, host_id, format, deck_id, deck_name, opponents, ticket, status,"
        " started_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'playing', ?)",
        (match_id, host_id, format, deck_id, deck_name, json.dumps(opponents), ticket, _now()),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def record_result(conn: sqlite3.Connection, result: dict, *, commit: bool = True) -> bool:
    """Complete a playing game from the game server's result. Idempotent: a game already completed, or a
    match this tracker never started, changes nothing (False). The ticket is dropped: the seat is gone."""
    status = result.get("status")
    if status not in STATUSES:
        status = "abandoned"
    log = result.get("log")
    cur = conn.execute(
        "UPDATE play_games SET status = ?, winner = ?, turn = ?, summary = ?, ended_at = ?, log = ?,"
        " ticket = NULL WHERE match_id = ? AND status = 'playing'",
        (
            status,
            result.get("winner"),
            result.get("turn"),
            result.get("summary"),
            result.get("endedAt") or _now(),
            json.dumps(log) if isinstance(log, list) else None,
            str(result.get("matchId", "")),
        ),
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def _game(row: sqlite3.Row) -> dict:
    g = dict(row)
    g["opponents"] = json.loads(g["opponents"])
    g.pop("log", None)
    return g


def recent(conn: sqlite3.Connection, host_id: int, limit: int = 12) -> list[dict]:
    """A person's games, newest first, without the log."""
    rows = conn.execute(
        "SELECT * FROM play_games WHERE host_id = ? ORDER BY started_at DESC, id DESC LIMIT ?",
        (host_id, limit),
    ).fetchall()
    return [_game(r) for r in rows]


def playing(conn: sqlite3.Connection, host_id: int | None = None) -> list[dict]:
    """Games the tracker still thinks are running (the game server's result hasn't arrived)."""
    sql = "SELECT * FROM play_games WHERE status = 'playing'"
    rows = conn.execute(
        sql + (" AND host_id = ?" if host_id is not None else "") + " ORDER BY id",
        (host_id,) if host_id is not None else (),
    ).fetchall()
    return [_game(r) for r in rows]


def get(conn: sqlite3.Connection, game_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM play_games WHERE id = ?", (game_id,)).fetchone()
    if row is None:
        return None
    g = _game(row)
    g["log"] = json.loads(row["log"]) if row["log"] else []
    return g


def import_history(
    conn: sqlite3.Connection, data: dict, *, owner_by_email, commit: bool = True
) -> tuple[int, int]:
    """The game server's old play log (play/games.json, `{version: 1, games: [...]}`), from before games
    lived here. Each game goes to the person its email names (`owner_by_email(email) -> id | None`, None
    for a game with no email or an unknown one, which is skipped). Re-importing skips games already here.
    Returns (imported, skipped)."""
    imported = skipped = 0
    for g in data.get("games", []):
        owner = owner_by_email(g.get("email"))
        match_id = f"legacy:{g.get('id')}"
        if (
            owner is None
            or conn.execute("SELECT 1 FROM play_games WHERE match_id = ?", (match_id,)).fetchone()
        ):
            skipped += 1
            continue
        status = g.get("status") if g.get("status") in STATUSES else "abandoned"
        opponents = [
            {"name": n.strip(), "deck": None}
            for n in str(g.get("opponentDeckName") or "").split(",")
            if n.strip()
        ]
        conn.execute(
            "INSERT INTO play_games (match_id, host_id, format, deck_id, deck_name, opponents, status,"
            " winner, turn, summary, started_at, ended_at, log)"
            " VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                match_id,
                owner,
                g.get("format") or "constructed",
                g.get("deckName") or "?",
                json.dumps(opponents),
                status,
                g.get("winner"),
                g.get("turn"),
                g.get("summary"),
                g.get("startedAt") or _now(),
                g.get("endedAt"),
                json.dumps(g["log"]) if isinstance(g.get("log"), list) else None,
            ),
        )
        imported += 1
    if commit:
        conn.commit()
    return imported, skipped
