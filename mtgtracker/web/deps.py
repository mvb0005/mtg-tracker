"""Request-level pieces shared by every router (and, later, the JSON API):
the per-request connection, who's asking, the ownership guards, and the
render/redirect helpers.

Nothing here is captured from app setup. create_app puts db_path, auth,
verifier and templates on `app.state`, and these read them from the request.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from mtgtracker import db, users


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """One short-lived connection per request, never shared. FastAPI runs
    sync dependencies and endpoints in a threadpool, so it may be created on
    one thread and used on another -- hence check_same_thread=False."""
    conn = db.connect(request.app.state.db_path, check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


def render(request: Request, template: str, status_code: int = 200, **context) -> HTMLResponse:
    context.setdefault("user", getattr(request.state, "user", None))
    context.setdefault("can_edit", False)  # owner-only controls are opt-in per page
    context.setdefault("can_add_people", False)
    context.setdefault("auth_mode", request.app.state.auth.mode)
    return request.app.state.templates.TemplateResponse(request, template, context, status_code=status_code)


def redirect(path: str, **params: str) -> RedirectResponse:
    query = urlencode({k: v for k, v in params.items() if v})
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)


def form_int(value) -> int | None:
    value = str(value or "").strip()
    return int(value) if value.isdigit() else None


# --- who may do what -----------------------------------------------------
# Friends can view each other's decks and cards; only an owner changes
# their own. Every write path runs these guards on every id it's given,
# so a posted deck_id or lot_id that isn't yours is a 403, never a quiet
# way to take someone's card.

def me(request: Request) -> users.User:
    return request.state.user


def load_deck(conn: sqlite3.Connection, deck_id: int) -> sqlite3.Row:
    deck = conn.execute("SELECT * FROM decks WHERE id = ?", (deck_id,)).fetchone()
    if deck is None:
        raise HTTPException(status_code=404, detail="No such deck")
    return deck


def require_own_deck(conn: sqlite3.Connection, deck_id: int, user: users.User) -> sqlite3.Row:
    deck = load_deck(conn, deck_id)
    if deck["owner_id"] != user.id:
        raise HTTPException(status_code=403, detail="That isn't your deck")
    return deck


def require_own_lot(conn: sqlite3.Connection, lot_id: int | None, user: users.User) -> None:
    if lot_id is None:
        return
    lot = conn.execute("SELECT owner_id FROM lots WHERE id = ?", (lot_id,)).fetchone()
    if lot is None:
        raise HTTPException(status_code=404, detail="No such card")
    if lot["owner_id"] != user.id:
        raise HTTPException(status_code=403, detail="That card isn't yours")


def load_person(conn: sqlite3.Connection, person_id: int) -> users.User:
    person = users.get(conn, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="No such person")
    return person


def may_act_for(request: Request, person: users.User) -> bool:
    """Acting on someone else's collection (importing, later loans) needs
    a real sign-in mode: in single mode every visitor is the admin."""
    actor = me(request)
    if person.id == actor.id:
        return True
    return request.app.state.auth.mode in ("dev", "cloudflare") and users.can_act_for(actor, person)


def may_add_people(request: Request) -> bool:
    """Adding a person adds an email to the sign-in allowlist. In single
    mode every visitor is the admin, so the web form stays off there
    (use `mtg add-user`) -- otherwise a stranger on the public site could
    pre-authorize themselves for when Cloudflare sign-in is turned on."""
    return request.app.state.auth.mode in ("dev", "cloudflare") and me(request).is_admin
