"""Activity lobbies: one room per Discord Activity instance (everyone in the same Activity shares it).

A room holds who is here (anyone polling in the last STALE seconds), the deck each person picked, the AI
opponents the room picked (anyone may change them; everyone sees it on their next poll), and, once someone
starts, the game's id and each seat's ticket. In memory: one tracker process, and a room is only the few
minutes before a game. A started room goes back to picking once its game is no longer playing.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

STALE = 20.0
INSTANCE = re.compile(r"[A-Za-z0-9_\-]{1,200}")


@dataclass
class Member:
    id: str  # Discord user id
    name: str
    joined: float
    avatar: str | None = None
    guest: bool = False
    deck_id: int | str | None = None
    in_game: bool = False
    left_table: bool = False
    seen: float = 0.0


@dataclass
class Room:
    instance: str
    members: dict[str, Member] = field(default_factory=dict)
    opponents: list[str] = field(default_factory=list)
    started: dict | None = None  # {"matchId", "tickets": {discord id: ticket}}
    starting: bool = False
    error: str | None = None


_ROOMS: dict[str, Room] = {}
LOCK = threading.Lock()


def valid_instance(instance: str) -> bool:
    return bool(INSTANCE.fullmatch(instance or ""))


def touch(
    instance: str,
    discord_id: str,
    name: str,
    avatar: str | None = None,
    now: float | None = None,
    guest: bool = False,
) -> Room:
    """The room, with this person marked present; people gone quiet for STALE seconds have left. Call with
    LOCK held."""
    now = now or time.time()
    room = _ROOMS.setdefault(instance, Room(instance))
    member = room.members.get(discord_id)
    if member is None:
        member = room.members[discord_id] = Member(discord_id, name, now)
    member.name, member.avatar, member.guest, member.seen = name, avatar, guest, now
    for gone in [m for m in room.members.values() if now - m.seen > STALE]:
        del room.members[gone.id]
    for empty in [k for k, r in _ROOMS.items() if not r.members and not r.starting]:
        del _ROOMS[empty]
    _ROOMS[instance] = room
    return room


def ticket_for(match_id: str, discord_id: str) -> str | None:
    """This Discord user's seat ticket in a running match started from an Activity room, if any."""
    with LOCK:
        for room in _ROOMS.values():
            if room.started and room.started.get("matchId") == match_id:
                return room.started.get("tickets", {}).get(discord_id)
    return None


def seated_ids(match_id: str) -> set[str]:
    """Discord user ids seated in a match started from an Activity room."""
    with LOCK:
        for room in _ROOMS.values():
            if room.started and room.started.get("matchId") == match_id:
                return set(room.started.get("tickets", {}).keys())
    return set()


def clear_match(match_id: str) -> None:
    """Clear `started` on any room whose match was ended."""
    with LOCK:
        for room in _ROOMS.values():
            if room.started and room.started.get("matchId") == match_id:
                room.started = None
                for m in room.members.values():
                    m.in_game = False
                    m.left_table = False


def clear() -> None:
    """Forget every room (tests)."""
    with LOCK:
        _ROOMS.clear()
