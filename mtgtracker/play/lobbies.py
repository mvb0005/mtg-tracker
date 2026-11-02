"""Web lobbies: a shareable /play/lobby/<code> where signed-in friends sit down together.

Same shape as the Discord Activity rooms (mtgtracker/discord/rooms.py): who is here, the deck each
picked, the AIs, and the tickets once someone starts. In memory, one tracker process.
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from dataclasses import dataclass, field

STALE = 20.0
CODE = re.compile(r"[a-f0-9]{8}")


@dataclass
class Member:
    id: str  # tracker person id, as a string
    name: str
    joined: float
    deck_id: int | str | None = None
    in_game: bool = False
    left_table: bool = False
    seen: float = 0.0


@dataclass
class Lobby:
    code: str
    members: dict[str, Member] = field(default_factory=dict)
    opponents: list[str] = field(default_factory=list)
    started: dict | None = None  # {"matchId", "tickets": {person id: ticket}}
    starting: bool = False
    error: str | None = None


_LOBBIES: dict[str, Lobby] = {}
LOCK = threading.Lock()


def valid_code(code: str) -> bool:
    return bool(CODE.fullmatch(code or ""))


def new_code() -> str:
    return secrets.token_hex(4)


def touch(code: str, person_id: str, name: str, now: float | None = None) -> Lobby:
    """The lobby, with this person marked present. Call with LOCK held."""
    now = now or time.time()
    lobby = _LOBBIES.setdefault(code, Lobby(code))
    member = lobby.members.get(person_id)
    if member is None:
        member = lobby.members[person_id] = Member(person_id, name, now)
    member.name, member.seen = name, now
    for gone in [m for m in lobby.members.values() if now - m.seen > STALE]:
        del lobby.members[gone.id]
    for empty in [k for k, r in _LOBBIES.items() if not r.members and not r.starting]:
        del _LOBBIES[empty]
    _LOBBIES[code] = lobby
    return lobby


def clear_match(match_id: str) -> None:
    with LOCK:
        for lobby in _LOBBIES.values():
            if lobby.started and lobby.started.get("matchId") == match_id:
                lobby.started = None
                for m in lobby.members.values():
                    m.in_game = False
                    m.left_table = False


def clear() -> None:
    """Forget every lobby (tests)."""
    with LOCK:
        _LOBBIES.clear()
