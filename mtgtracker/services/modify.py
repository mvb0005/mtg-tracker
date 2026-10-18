"""Edits to a deck's change list, and applying it as a version. Each returns
the notice to show; the caller has already run the ownership guards on the
deck and on any lot id."""

from __future__ import annotations

import sqlite3

from mtgtracker import deckmods
from mtgtracker.services import ServiceError, translate_errors
from mtgtracker.services.decks import MAX_LIST_CHARS


def add_change(
    conn: sqlite3.Connection, deck_id: int, *, lot_id: int | None = None, planned_name: str = "",
    commit: bool = True,
) -> str:
    """Queue an owned copy (lot_id) or, without one, a card to acquire by name."""
    with translate_errors():
        if lot_id is not None:
            deckmods.add_owned(conn, deck_id, lot_id, commit=commit)
            return "Added to the change list."
        deckmods.add_planned(conn, deck_id, planned_name, commit=commit)
        return "Added to the change list as to acquire."


def import_list(conn: sqlite3.Connection, deck_id: int, text: str, *, commit: bool = True) -> str:
    if len(text) > MAX_LIST_CHARS:
        raise ServiceError("That list is too long")
    with translate_errors():
        return deckmods.import_list(conn, deck_id, text, commit=commit).message()


def set_cut(
    conn: sqlite3.Connection, deck_id: int, change_id: int, cut_lot_id: int | None,
    cut_want_id: int | None, *, commit: bool = True,
) -> str:
    with translate_errors():
        deckmods.set_cut(conn, deck_id, change_id, cut_lot_id, cut_want_id=cut_want_id, commit=commit)
    return "Replacement updated."


def add_swap(
    conn: sqlite3.Connection, deck_id: int, lot_id: int | None, want_id: int | None, *, commit: bool = True
) -> str:
    """Replace a card the deck lists but you don't own with one you do."""
    if lot_id is None or want_id is None:
        raise ServiceError("Pick a card to replace and one of yours to use")
    with translate_errors():
        deckmods.add_swap(conn, deck_id, lot_id, want_id, commit=commit)
    return "Queued the swap; apply to make it the next version."


def link_owned(
    conn: sqlite3.Connection, deck_id: int, change_id: int, lot_id: int | None, *, commit: bool = True
) -> str:
    if lot_id is None:
        raise ServiceError("Pick a copy to use")
    with translate_errors():
        deckmods.link_owned(conn, deck_id, change_id, lot_id, commit=commit)
    return "Linked your copy; the change is ready."


def remove_change(conn: sqlite3.Connection, deck_id: int, change_id: int, *, commit: bool = True) -> str:
    with translate_errors():
        deckmods.remove_change(conn, deck_id, change_id, commit=commit)
    return "Removed from the change list."


def apply(conn: sqlite3.Connection, deck_id: int, note: str, *, commit: bool = True) -> str:
    with translate_errors():
        version = deckmods.apply_changes(conn, deck_id, note, commit=commit)
    return f"Applied as version {version}."
