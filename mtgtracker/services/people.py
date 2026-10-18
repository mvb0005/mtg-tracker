"""People and collection imports."""

from __future__ import annotations

import sqlite3

import httpx

from mtgtracker import users
from mtgtracker.importers import collection_csv
from mtgtracker.services import ServiceError

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
TOO_BIG = "That file is too big (5 MB at most)"


def add_person(
    conn: sqlite3.Connection, display_name: str, email: str, *, commit: bool = True
) -> str:
    """Add someone to the sign-in allowlist (the caller checks the actor may).
    Returns the notice."""
    try:
        person = users.create(conn, display_name, email, commit=commit)
    except users.UserError as exc:
        raise ServiceError(str(exc)) from exc
    return f"Added {person.display_name}."


def import_collection(
    conn: sqlite3.Connection, owner: users.User, actor: users.User, data: bytes, filename: str,
    *, commit: bool = True,
) -> str:
    """Import a collection CSV into owner's collection (the caller checked
    the actor may act for them). Returns the notice."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ServiceError(TOO_BIG)
    try:
        summary = collection_csv.import_collection(
            conn, data, owner_id=owner.id, imported_by=actor.id, filename=filename[:200], commit=commit
        )
    except collection_csv.CollectionImportError as exc:
        raise ServiceError(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise ServiceError("Couldn't reach Scryfall; nothing was imported") from exc
    return summary.message()
