"""Lobbies: where players sit down together before a game.

The shareable /play/lobby/<code> (signed-in friends) and the Discord Activity's lobby (everyone in one
Activity instance) are the same thing: who is here (anyone polling in the last STALE seconds), the deck each
picked, the AIs anyone may change, and, once someone starts, the game's id and each seat's ticket. A web
lobby's key is its code; an Activity's is "discord:<instance id>". Members are tracker person ids in web
lobbies and Discord ids in Activity ones. In memory: one tracker process, and a lobby is only the few
minutes before a game. A started lobby goes back to picking once its game is no longer playing.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field

STALE = 20.0
CODE = re.compile(r"[a-f0-9]{8}")
INSTANCE = re.compile(r"[A-Za-z0-9_\-]{1,200}")


@dataclass
class Member:
    id: str
    name: str
    joined: float
    avatar: str | None = None
    guest: bool = False
    deck_id: int | str | None = None
    in_game: bool = False
    left_table: bool = False
    seen: float = 0.0


@dataclass
class Lobby:
    key: str
    members: dict[str, Member] = field(default_factory=dict)
    opponents: list[str] = field(default_factory=list)
    started: dict | None = None  # {"matchId", "tickets": {member id: ticket}}
    starting: bool = False
    error: str | None = None


_LOBBIES: dict[str, Lobby] = {}
LOCK = threading.Lock()


def valid_code(code: str) -> bool:
    return bool(CODE.fullmatch(code or ""))


def new_code() -> str:
    return secrets.token_hex(4)


def valid_instance(instance: str) -> bool:
    return bool(INSTANCE.fullmatch(instance or ""))


def activity_key(instance: str) -> str:
    return f"discord:{instance}"


def touch(
    key: str,
    member_id: str,
    name: str,
    avatar: str | None = None,
    guest: bool = False,
    now: float | None = None,
) -> Lobby:
    """The lobby, with this member marked present; people gone quiet for STALE seconds have left. Call with
    LOCK held."""
    now = now or time.time()
    lobby = _LOBBIES.setdefault(key, Lobby(key))
    member = lobby.members.get(member_id)
    if member is None:
        member = lobby.members[member_id] = Member(member_id, name, now)
    member.name, member.avatar, member.guest, member.seen = name, avatar, guest, now
    for gone in [m for m in lobby.members.values() if now - m.seen > STALE]:
        del lobby.members[gone.id]
    for empty in [k for k, r in _LOBBIES.items() if not r.members and not r.starting]:
        del _LOBBIES[empty]
    _LOBBIES[key] = lobby
    return lobby


def apply(
    conn: sqlite3.Connection,
    lobby: Lobby,
    you: str,
    body: dict,
    decks: list[dict],
    roster: list[dict],
    max_seats: int,
    imported: tuple[int | str | None, str | None] = (None, None),
) -> tuple[list[Member], list[str]] | None:
    """One poll's changes from `you`: a deck pick, the AIs, an imported deck (its id, or the import's error),
    a start. A lobby whose game is over picks again. Returns who sits down and the AIs when this poll starts
    the game, else None. Call with LOCK held."""
    if lobby.started:
        row = conn.execute(
            "SELECT status FROM play_games WHERE match_id = ?", (lobby.started["matchId"],)
        ).fetchone()
        if row is not None and row["status"] != "playing":
            lobby.started = None
            for m in lobby.members.values():
                m.in_game = m.left_table = False
    imported_id, import_err = imported
    if import_err is not None:
        lobby.error = import_err
    elif imported_id is not None:
        lobby.members[you].deck_id = imported_id
        lobby.error = None
    valid_decks = {d["id"] for d in decks} | {o["id"] for o in roster}
    if "deckId" in body and body["deckId"] in valid_decks:
        lobby.members[you].deck_id = body["deckId"]
        lobby.error = None
    if isinstance(body.get("opponents"), list):
        known = {o["id"] for o in roster} | {f"deck:{d['id']}" for d in decks}
        lobby.opponents = [str(o) for o in body["opponents"] if str(o) in known][: max_seats - 1]
        lobby.error = None
    if not body.get("start") or lobby.started or lobby.starting:
        return None
    lobby.starting = True
    seated = sorted(
        (m for m in lobby.members.values() if m.deck_id is not None and not m.in_game), key=lambda m: m.joined
    )
    return seated, list(lobby.opponents)


def finish_start(lobby: Lobby, seated: list[Member], started: dict | None, error: str | None) -> None:
    """Records how starting the game went: {"matchId", "tickets"} or the error."""
    with LOCK:
        lobby.starting = False
        lobby.started = started
        lobby.error = error
        if started:
            for m in seated:
                if m.id in lobby.members:
                    lobby.members[m.id].in_game = True
                    lobby.members[m.id].left_table = False


def ticket_for(match_id: str, member_id: str) -> str | None:
    """This member's seat ticket in a running match started from a lobby, if any."""
    with LOCK:
        for lobby in _LOBBIES.values():
            if lobby.started and lobby.started.get("matchId") == match_id:
                return lobby.started.get("tickets", {}).get(member_id)
    return None


def seated_ids(match_id: str) -> set[str]:
    """The members seated in a match started from a lobby."""
    with LOCK:
        for lobby in _LOBBIES.values():
            if lobby.started and lobby.started.get("matchId") == match_id:
                return set(lobby.started.get("tickets", {}).keys())
    return set()


def clear_match(match_id: str) -> None:
    """A match was ended: its lobby picks again."""
    with LOCK:
        for lobby in _LOBBIES.values():
            if lobby.started and lobby.started.get("matchId") == match_id:
                lobby.started = None
                for m in lobby.members.values():
                    m.in_game = m.left_table = False


def clear() -> None:
    """Forget every lobby (tests)."""
    with LOCK:
        _LOBBIES.clear()
