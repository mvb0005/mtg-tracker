"""Pinning unresolved lots to printings."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

import httpx

from mtgtracker import printings, scryfall
from mtgtracker.services import NotFoundError, ServiceError

SET_CODE_RE = re.compile(r"^[a-z0-9]{2,8}$")


@dataclass
class Pinned:
    notice: str
    next_lot_id: int | None  # the rest of the group still unresolved, if any


def assign(
    conn: sqlite3.Connection, lot_id: int, choice: str | None, quantities: Iterable[tuple[str, str]],
    *, commit: bool = True,
) -> Pinned:
    """Pin lots in lot_id's group. `choice` is one "scryfall_id|finish" (one
    copy); `quantities` are the form's ("qty|scryfall_id|finish", "n") pairs."""
    group = printings.group_for_lot(conn, lot_id)
    if group is None:
        raise NotFoundError("No unresolved card with that id")
    assignments: list[tuple[str, str, int]] = []
    try:
        if choice:
            scryfall_id, finish = choice.split("|", 1)
            assignments.append((scryfall_id, finish, 1))
        for key, value in quantities:
            if key.startswith("qty|") and value.strip():
                _, scryfall_id, finish = key.split("|", 2)
                assignments.append((scryfall_id, finish, int(value)))
        assigned = printings.assign(conn, lot_id, assignments, commit=commit)
    except (ValueError, printings.PrintingError) as exc:
        message = str(exc) if isinstance(exc, printings.PrintingError) else "Invalid form input"
        raise ServiceError(message) from exc
    notice = f"Pinned {assigned} {group.card_name}."
    return Pinned(notice, group.lot_ids[assigned] if assigned < group.qty else None)


def fetch_set(set_code: str) -> str:
    """Explicit, user-initiated Scryfall lookup of a whole set (cached
    afterwards). Page loads never hit the network. Returns the notice."""
    set_code = set_code.strip().lower()
    if not SET_CODE_RE.match(set_code):
        raise ServiceError(f"'{set_code}' doesn't look like a set code")
    try:
        cards = scryfall.fetch_set_cards(set_code)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise ServiceError(f"Scryfall has no set '{set_code}'") from exc
        raise ServiceError(f"Scryfall error {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise ServiceError("Couldn't reach Scryfall") from exc
    return f"Loaded {len(cards)} printings from {set_code.upper()}."
