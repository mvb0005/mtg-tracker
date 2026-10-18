"""Users. Every card (lot), deck and purchase belongs to one (owner_id).

User 1 is the original owner and an admin. Users exist only if an admin
added them (an allowlist): signing in through Cloudflare Access maps the
verified email to an existing user and never creates one, except that
MTG_OWNER_EMAIL claims user 1 the first time it signs in.
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


def _user(row: sqlite3.Row | None) -> User | None:
    if row is None:
        return None
    return User(id=row["id"], email=row["email"], display_name=row["display_name"],
                is_admin=bool(row["is_admin"]), signed_in=row["last_seen_at"] is not None)


def get(conn: sqlite3.Connection, user_id: int) -> User | None:
    return _user(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def by_email(conn: sqlite3.Connection, email: str) -> User | None:
    return _user(conn.execute("SELECT * FROM users WHERE email = ?", (email.strip(),)).fetchone())


def list_users(conn: sqlite3.Connection) -> list[User]:
    return [_user(r) for r in conn.execute("SELECT * FROM users ORDER BY id")]


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
    if owner is None or owner.email is not None or by_email(conn, email):
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
    return actor.is_admin and not owner.signed_in
