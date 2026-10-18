"""People and collection imports."""

from __future__ import annotations

import sqlite3

import httpx

from mtgtracker import users
from mtgtracker.importers import collection_csv
from mtgtracker.services import ServiceError

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
TOO_BIG = "That file is too big (5 MB at most)"


def add_person_previewed(
    conn: sqlite3.Connection, display_name: str, email: str, *, commit: bool = True
) -> tuple[str, dict]:
    """Add someone to the sign-in allowlist (the caller checks the actor may).
    Returns (notice, preview): the preview says exactly what was added, and
    with commit=False it is what a confirmed dry run shows."""
    try:
        person = users.create(conn, display_name, email, commit=commit)
    except users.UserError as exc:
        raise ServiceError(str(exc)) from exc
    preview = {
        "action": "add_person", "user_id": person.id, "display_name": person.display_name,
        "email": person.email, "effect": "adds this email to the sign-in allowlist",
    }
    return f"Added {person.display_name}.", preview


def add_person(
    conn: sqlite3.Connection, display_name: str, email: str, *, commit: bool = True
) -> str:
    return add_person_previewed(conn, display_name, email, commit=commit)[0]


def add_credential(conn: sqlite3.Connection, user_id: int, cf_client_id: str, label: str) -> str:
    """Register a service token's Client ID for a system user (an identifier, never a secret)."""
    try:
        cred = users.add_credential(conn, user_id, cf_client_id, label)
    except users.UserError as exc:
        raise ServiceError(str(exc)) from exc
    return f"Added credential {cred.label}."


def revoke_credential(conn: sqlite3.Connection, credential_id: int) -> str:
    try:
        users.revoke_credential(conn, credential_id)
    except users.UserError as exc:
        raise ServiceError(str(exc)) from exc
    return "Credential revoked."


def set_system_disabled(conn: sqlite3.Connection, user_id: int, disabled: bool) -> str:
    user = users.get(conn, user_id)
    if user is None or user.kind != "system":
        raise ServiceError("no such system user")
    users.set_disabled(conn, user_id, disabled)
    return f"{user.display_name} {'disabled' if disabled else 'enabled'}."


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
