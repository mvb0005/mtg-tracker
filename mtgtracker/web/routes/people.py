"""People (friends) and collection imports."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from mtgtracker import audit, users
from mtgtracker.confirm import risky
from mtgtracker.services import ServiceError
from mtgtracker.services import people as people_service
from mtgtracker.web import deps, queries
from mtgtracker.web.deps import (
    get_conn,
    load_person,
    may_act_for,
    may_add_people,
    may_manage_system,
    me,
    redirect,
    render,
    require_person,
)
from mtgtracker.web.routes.pages import collection_page

router = APIRouter()


@router.get("/people", response_class=HTMLResponse)
def people(
    request: Request, notice: str = "", error: str = "", conn: sqlite3.Connection = Depends(get_conn)
):
    rows = []
    for person in users.list_users(conn):
        rows.append({
            "user": person,
            "decks": len(queries.list_decks(conn, person.id)),
            "cards": queries.count_cards(conn, person.id),
        })
    system = []
    if may_manage_system(request):
        credentials = users.list_credentials(conn)
        system = [
            {"user": u, "credentials": [c for c in credentials if c.user_id == u.id]}
            for u in users.list_users(conn, "system")
        ]
    return render(request, "people.html", people=rows, notice=notice, error=error,
                  can_add_people=may_add_people(request), can_manage_system=may_manage_system(request),
                  system_users=system)


@router.post("/people")
@risky
async def people_add(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    if not may_add_people(request):
        raise HTTPException(status_code=403, detail="Only a signed-in admin can add people")
    form = await request.form()
    name, email = str(form.get("display_name", "")), str(form.get("email", ""))
    try:
        notice = deps.run_risky(
            request, conn, lambda c: people_service.add_person_previewed(c, name, email, commit=False), form
        )
    except ServiceError as exc:
        return redirect("/people", error=str(exc))
    return redirect("/people", notice=notice)


def _require_system_admin(request: Request) -> None:
    if not may_manage_system(request):
        raise HTTPException(status_code=403, detail="Only a signed-in admin can manage system users")


def _system_admin(request: Request, conn: sqlite3.Connection, action) -> object:
    """Human-only (require_person is on each route), admin-only: run a
    service call and redirect back to /people with its notice or error."""
    _require_system_admin(request)
    try:
        return redirect("/people", notice=action())
    except ServiceError as exc:
        return redirect("/people", error=str(exc))


@router.post("/system-users/{user_id}/credentials", dependencies=[Depends(require_person)])
async def system_credential_add(
    user_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
):
    _require_system_admin(request)
    form = await request.form()
    return _system_admin(request, conn, lambda: people_service.add_credential(
        conn, user_id, str(form.get("cf_client_id", "")), str(form.get("label", ""))))


@router.post("/system-users/credentials/{credential_id}/revoke", dependencies=[Depends(require_person)])
def system_credential_revoke(
    credential_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
):
    return _system_admin(request, conn, lambda: people_service.revoke_credential(conn, credential_id))


@router.post("/system-users/{user_id}/disable", dependencies=[Depends(require_person)])
def system_user_disable(user_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _system_admin(request, conn, lambda: people_service.set_system_disabled(conn, user_id, True))


@router.post("/system-users/{user_id}/enable", dependencies=[Depends(require_person)])
def system_user_enable(user_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _system_admin(request, conn, lambda: people_service.set_system_disabled(conn, user_id, False))


@router.get("/people/{person_id}", response_class=HTMLResponse)
def person_page(
    person_id: int, request: Request, notice: str = "", error: str = "",
    conn: sqlite3.Connection = Depends(get_conn),
):
    person = load_person(conn, person_id)
    can_import = may_act_for(request, person)
    # Activity is private like money: that person, or an admin.
    activity = audit.recent(conn, person.id) if person.id == me(request).id or me(request).is_admin else []
    imports = conn.execute(
        "SELECT original_filename, format, card_count, imported_at FROM collection_imports"
        " WHERE owner_id = ? ORDER BY id DESC", (person.id,),
    ).fetchall() if can_import else []
    return render(
        request, "person.html", person=person, decks=queries.list_decks(conn, person.id),
        card_count=queries.count_cards(conn, person.id), can_import=can_import, imports=imports,
        notice=notice, error=error, activity=activity,
    )


@router.post("/people/{person_id}/import")
async def person_import(person_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Import a collection CSV into this person's collection: your own,
    or (as an admin) for someone who hasn't signed in yet."""
    person = load_person(conn, person_id)
    if not may_act_for(request, person):
        raise HTTPException(status_code=403, detail="You can't import into that collection")
    back = f"/people/{person.id}"
    limit = people_service.MAX_UPLOAD_BYTES
    # Refuse an oversized body before the form parser spools it.
    if int(request.headers.get("content-length") or 0) > limit + 64 * 1024:
        return redirect(back, error=people_service.TOO_BIG)
    form = await request.form()
    upload = form.get("file")
    if upload is None or isinstance(upload, str) or not upload.filename:
        return redirect(back, error="Choose a CSV file to import")
    data = await upload.read(limit + 1)
    try:
        notice = people_service.import_collection(conn, person, deps.actor(request), data, upload.filename)
    except ServiceError as exc:
        return redirect(back, error=str(exc))
    return redirect(back, notice=notice)


@router.get("/people/{person_id}/cards", response_class=HTMLResponse)
def person_cards(
    person_id: int, request: Request, q: str = "", view: str = "",
    sort: str = "", color: list[str] = Query([]), color_mode: str = "", type: list[str] = Query([]),
    mv: list[str] = Query([]), deck: list[str] = Query([]),
    conn: sqlite3.Connection = Depends(get_conn),
):
    person = load_person(conn, person_id)
    return collection_page(request, conn, person, q=q, view=view, sort=sort, color=color,
                           color_mode=color_mode, type=type, mv=mv, deck=deck,
                           action=f"/people/{person.id}/cards")
