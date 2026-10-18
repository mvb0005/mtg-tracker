"""Users. Every card (lot), deck and purchase belongs to one (owner_id).

User 1 is the original owner and an admin. Users exist only if an admin
added them (an allowlist): signing in through Cloudflare Access maps the
verified email to an existing user and never creates one, except that
MTG_OWNER_EMAIL claims user 1 the first time it signs in.

A system user (kind 'system', e.g. Claude) is an automated actor that acts
for people. It has no email and is never an admin, so the email path can't
reach it; it authenticates by a service credential (a Cloudflare Access
service token's Client ID, never its secret). Users can be disabled
(disabled_at), which refuses them at sign-in.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

OWNER_ID = 1
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UserError(ValueError):
    """A user that can't be created or changed as asked."""


@dataclass(frozen=True)
class User:
    id: int
    email: str | None
    display_name: str
    is_admin: bool
    signed_in: bool = False  # has ever signed in (last_seen_at set)
    kind: str = "person"  # 'person' or 'system'
    disabled: bool = False


@dataclass(frozen=True)
class Credential:
    id: int
    user_id: int
    cf_client_id: str
    label: str
    created_at: str
    last_used_at: str | None
    revoked_at: str | None


def _user(row: sqlite3.Row | None) -> User | None:
    if row is None:
        return None
    return User(id=row["id"], email=row["email"], display_name=row["display_name"],
                is_admin=bool(row["is_admin"]), signed_in=row["last_seen_at"] is not None,
                kind=row["kind"], disabled=row["disabled_at"] is not None)


def get(conn: sqlite3.Connection, user_id: int) -> User | None:
    return _user(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def by_email(conn: sqlite3.Connection, email: str) -> User | None:
    """The person with this email. Never a system user, whatever the row says."""
    return _user(conn.execute(
        "SELECT * FROM users WHERE email = ? AND kind = 'person'", (email.strip(),)
    ).fetchone())


def list_users(conn: sqlite3.Connection, kind: str = "person") -> list[User]:
    """People by default (the friends list); kind='system' for system users."""
    return [_user(r) for r in conn.execute("SELECT * FROM users WHERE kind = ? ORDER BY id", (kind,))]


def create(
    conn: sqlite3.Connection, display_name: str, email: str | None = None, *, is_admin: bool = False,
    commit: bool = True,
) -> User:
    display_name = display_name.strip()
    email = (email or "").strip() or None
    if not display_name:
        raise UserError("give them a name")
    if email is not None:
        if not _EMAIL_RE.match(email):
            raise UserError(f"'{email}' doesn't look like an email address")
        if by_email(conn, email):
            raise UserError(f"{email} is already a user")
    cur = conn.execute(
        "INSERT INTO users (email, display_name, is_admin, created_at) VALUES (?, ?, ?, ?)",
        (email, display_name, int(is_admin), datetime.now(UTC).isoformat()),
    )
    if commit:
        conn.commit()
    return get(conn, cur.lastrowid)


def rename(conn: sqlite3.Connection, user_id: int, display_name: str) -> User:
    display_name = display_name.strip()
    if not display_name:
        raise UserError("give them a name")
    if get(conn, user_id) is None:
        raise UserError(f"no user {user_id}")
    conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (display_name, user_id))
    conn.commit()
    return get(conn, user_id)


def claim_owner(conn: sqlite3.Connection, email: str) -> User | None:
    """Bind `email` to user 1 if user 1 has no email yet and nobody else
    uses it (MTG_OWNER_EMAIL's first sign-in). Returns user 1, or None."""
    owner = get(conn, OWNER_ID)
    if owner is None or owner.kind != "person" or owner.email is not None or by_email(conn, email):
        return None
    conn.execute("UPDATE users SET email = ? WHERE id = ?", (email.strip(), OWNER_ID))
    conn.commit()
    return get(conn, OWNER_ID)


def touch(conn: sqlite3.Connection, user_id: int) -> None:
    """Record a sign-in (at most once a minute, to keep writes rare)."""
    now = datetime.now(UTC)
    conn.execute(
        "UPDATE users SET last_seen_at = ? WHERE id = ?"
        " AND (last_seen_at IS NULL OR last_seen_at < ?)",
        (now.isoformat(), user_id, now.replace(second=0, microsecond=0).isoformat()),
    )
    conn.commit()


def can_act_for(actor: User, owner: User) -> bool:
    """Whether `actor` may act on `owner`'s things: themselves, or an admin
    acting for someone who has never signed in -- so a friend's requests
    and returns don't stall waiting on them. Once they sign in, only they
    can."""
    if actor.id == owner.id:
        return True
    return actor.kind == "person" and owner.kind == "person" and actor.is_admin and not owner.signed_in


# --- system users and their service credentials -----------------------------

def create_system(conn: sqlite3.Connection, display_name: str, *, commit: bool = True) -> User:
    """An automated actor: no email, never an admin."""
    display_name = display_name.strip()
    if not display_name:
        raise UserError("give it a name")
    cur = conn.execute(
        "INSERT INTO users (email, display_name, is_admin, created_at, kind)"
        " VALUES (NULL, ?, 0, ?, 'system')",
        (display_name, datetime.now(UTC).isoformat()),
    )
    if commit:
        conn.commit()
    return get(conn, cur.lastrowid)


def _credential(row: sqlite3.Row) -> Credential:
    return Credential(**{k: row[k] for k in Credential.__dataclass_fields__})


def add_credential(
    conn: sqlite3.Connection, user_id: int, cf_client_id: str, label: str, *, commit: bool = True
) -> Credential:
    """Let a Cloudflare Access service token (by its Client ID) act as this
    system user. Only the Client ID is stored; the secret never is."""
    user = get(conn, user_id)
    if user is None or user.kind != "system":
        raise UserError(f"user {user_id} isn't a system user")
    cf_client_id, label = cf_client_id.strip(), label.strip()
    if not cf_client_id:
        raise UserError("give the service token's Client ID")
    if not label:
        raise UserError("give the credential a label")
    if conn.execute("SELECT 1 FROM service_credentials WHERE cf_client_id = ?", (cf_client_id,)).fetchone():
        raise UserError("that Client ID is already registered")
    cur = conn.execute(
        "INSERT INTO service_credentials (user_id, cf_client_id, label, created_at) VALUES (?, ?, ?, ?)",
        (user_id, cf_client_id, label, datetime.now(UTC).isoformat()),
    )
    if commit:
        conn.commit()
    row = conn.execute("SELECT * FROM service_credentials WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _credential(row)


def revoke_credential(conn: sqlite3.Connection, credential_id: int, *, commit: bool = True) -> None:
    row = conn.execute("SELECT revoked_at FROM service_credentials WHERE id = ?", (credential_id,)).fetchone()
    if row is None:
        raise UserError(f"no credential {credential_id}")
    if row["revoked_at"] is None:
        conn.execute(
            "UPDATE service_credentials SET revoked_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), credential_id),
        )
    if commit:
        conn.commit()


def credential_user(conn: sqlite3.Connection, cf_client_id: str) -> User | None:
    """The system user behind a service token's Client ID, or None if it is
    unknown, revoked, or its user is disabled or not a system user. Marks
    the credential used."""
    row = conn.execute(
        "SELECT c.id AS cid, u.* FROM service_credentials c JOIN users u ON u.id = c.user_id"
        " WHERE c.cf_client_id = ? AND c.revoked_at IS NULL AND u.kind = 'system' AND u.disabled_at IS NULL",
        (cf_client_id,),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE service_credentials SET last_used_at = ? WHERE id = ?",
        (datetime.now(UTC).isoformat(), row["cid"]),
    )
    conn.commit()
    return _user(row)


def set_disabled(conn: sqlite3.Connection, user_id: int, disabled: bool, *, commit: bool = True) -> User:
    if user_id == OWNER_ID:
        raise UserError("the owner can't be disabled")
    if get(conn, user_id) is None:
        raise UserError(f"no user {user_id}")
    conn.execute(
        "UPDATE users SET disabled_at = ? WHERE id = ?",
        (datetime.now(UTC).isoformat() if disabled else None, user_id),
    )
    if commit:
        conn.commit()
    return get(conn, user_id)


def list_credentials(conn: sqlite3.Connection) -> list[Credential]:
    return [_credential(r) for r in conn.execute("SELECT * FROM service_credentials ORDER BY id")]
