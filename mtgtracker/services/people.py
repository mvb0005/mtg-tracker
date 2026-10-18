"""People and collection imports."""

from __future__ import annotations

import sqlite3

import httpx

from mtgtracker import users
from mtgtracker.importers import collection_csv
from mtgtracker.services import NotFoundError, ServiceError, UpstreamError

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


def rename_person(
    conn: sqlite3.Connection, user: users.User, display_name: str, *, commit: bool = True
) -> tuple[str, str]:
    """Change someone's display name. Returns (notice, audit summary): the
    page says "Saved.", the audit log says who became what."""
    try:
        renamed = users.rename(conn, user.id, display_name, commit=commit)
    except users.UserError as exc:
        raise ServiceError(str(exc)) from exc
    if renamed.display_name == user.display_name:
        return "Saved.", "Saved (no change)"
    return "Saved.", f"Renamed {user.display_name} to {renamed.display_name}"


def edit_person(
    conn: sqlite3.Connection, actor: users.User, person_id: int, display_name: str, email: str,
    *, confirm_email: bool = False, commit: bool = True,
) -> tuple[str, str]:
    """An admin edits another person: display name, and sign-in email, which
    decides who can sign in as them. Never your own email (so you can't lock
    yourself out), never a system user's; changing the email of someone who
    has signed in needs confirm_email. Returns (notice, audit summary)."""
    person = users.get(conn, person_id)
    if person is None:
        raise NotFoundError("No such person")
    if person.kind != "person" or actor.kind != "person" or not actor.is_admin:
        raise ServiceError("Only an admin can edit a person")
    email = email.strip()
    changes = []
    try:
        new_name = users.clean_name(display_name)
        email_changed = bool(email) and email.casefold() != (person.email or "").casefold()
        if email_changed:
            if person.id == actor.id:
                raise ServiceError("You can't change your own sign-in email")
            users.clean_email(email)
            other = users.by_email(conn, email)
            if other is not None and other.id != person.id:
                raise ServiceError(f"{email} is already a user")
            if person.signed_in and not confirm_email:
                raise ServiceError("They have signed in before: tick the box to confirm the email change")
        if new_name != person.display_name:
            users.rename(conn, person.id, new_name, commit=False)
            changes.append(f"Renamed {person.display_name} to {new_name}")
        if email_changed:
            users.set_email(conn, person.id, email, commit=False)
            changes.append(f"Changed {new_name}'s sign-in email")
        if commit:
            conn.commit()
    except users.UserError as exc:
        raise ServiceError(str(exc)) from exc
    if not changes:
        return "Nothing to change.", "No changes"
    return "Saved.", "; ".join(changes)


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


def import_collection_summary(
    conn: sqlite3.Connection, owner: users.User, actor: users.User, data: bytes, filename: str,
    *, commit: bool = True,
) -> collection_csv.ImportSummary:
    """Import a collection CSV into owner's collection (the caller checked
    the actor may act for them). Returns the importer's summary."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ServiceError(TOO_BIG)
    try:
        return collection_csv.import_collection(
            conn, data, owner_id=owner.id, imported_by=actor.id, filename=filename[:200], commit=commit
        )
    except collection_csv.CollectionImportError as exc:
        raise ServiceError(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise UpstreamError("Couldn't reach Scryfall; nothing was imported") from exc


def import_collection(
    conn: sqlite3.Connection, owner: users.User, actor: users.User, data: bytes, filename: str,
    *, commit: bool = True,
) -> str:
    """As import_collection_summary, returning the notice."""
    return import_collection_summary(conn, owner, actor, data, filename, commit=commit).message()
