"""Deck-level writes: create from a list, notes, commander."""

from __future__ import annotations

import sqlite3

from mtgtracker import commanders, deckmods, users
from mtgtracker.services import ServiceError, translate_errors

MAX_LIST_CHARS = 50_000
# Deck notes: the owner's free-form context for a deck (plan, budget rules,
# decisions and why).
MAX_NOTES = 20_000


def create_from_list(
    conn: sqlite3.Connection, user: users.User, name: str, text: str, *, commit: bool = True
) -> deckmods.NewDeckSummary:
    if len(text) > MAX_LIST_CHARS:
        raise ServiceError("That list is too long")
    with translate_errors():
        return deckmods.create_deck_from_list(conn, name, text, owner_id=user.id, commit=commit)


def save_notes(conn: sqlite3.Connection, deck_id: int, text: str, *, commit: bool = True) -> str:
    """Set (or, if empty, clear) the deck's notes. Returns the notice."""
    text = text.replace("\r\n", "\n").strip()
    if len(text) > MAX_NOTES:
        raise ServiceError(f"Notes are limited to {MAX_NOTES:,} characters")
    conn.execute("UPDATE decks SET notes = ? WHERE id = ?", (text or None, deck_id))
    if commit:
        conn.commit()
    return "Notes saved." if text else "Notes cleared."


def set_commander(
    conn: sqlite3.Connection, deck_id: int, card_name: str | None, *, commit: bool = True
) -> str:
    """Make card_name the deck's commander, or clear it (empty/None).
    Returns the notice."""
    card_name = (card_name or "").strip() or None
    try:
        commanders.set_commander(conn, deck_id, card_name, commit=commit)
    except commanders.CommanderError as exc:
        raise ServiceError(str(exc)) from exc
    return f"{card_name} is now the commander." if card_name else "Commander cleared."
