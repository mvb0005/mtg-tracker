"""Decks: list, create, detail, notes, commander, versions."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from mtgtracker import assembly, commanders, deckmods, users
from mtgtracker.services import ServiceError
from mtgtracker.services import decks as deck_service
from mtgtracker.web import filters, queries
from mtgtracker.web.deps import get_conn, me, redirect, render, require_own_deck

router = APIRouter()


@router.get("/decks", response_class=HTMLResponse)
def decks(
    request: Request, notice: str = "", error: str = "", conn: sqlite3.Connection = Depends(get_conn)
):
    return render(
        request, "decks.html", decks=queries.list_decks(conn, me(request).id), notice=notice, error=error
    )


@router.post("/decks/new")
async def deck_new(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    try:
        summary = deck_service.create_from_list(
            conn, me(request), str(form.get("name", "")), str(form.get("decklist", ""))
        )
    except ServiceError as exc:
        return redirect("/decks", error=str(exc))
    return redirect(f"/decks/{summary.deck_id}", notice=summary.message())


@router.get("/decks/{deck_id}", response_class=HTMLResponse)
def deck_detail(
    deck_id: int, request: Request, view: str = "", notice: str = "", error: str = "", pl: str = "",
    sort: str = "", color: list[str] = Query([]), color_mode: str = "", type: list[str] = Query([]),
    mv: list[str] = Query([]),
    conn: sqlite3.Connection = Depends(get_conn),
):
    card_filters = filters.CardFilters.parse(
        sort=sort, color=color, color_mode=color_mode, type=type, mv=mv
    )
    user = me(request)
    detail = queries.get_deck(conn, deck_id, card_filters, viewer_id=user.id)
    if detail is None:
        raise HTTPException(status_code=404, detail="No such deck")
    can_edit = detail["deck"]["owner_id"] == user.id
    commander_options = (
        commanders.eligible(conn, deck_id) if can_edit and detail["deck"]["format"] == "Commander" else []
    )
    pull_list = assembly.decode_pull_list(conn, deck_id, pl) if can_edit and pl else []
    return render(
        request, "deck_detail.html", view=view, notice=notice, error=error, pull_list=pull_list,
        commander_options=commander_options, filters=card_filters, can_edit=can_edit,
        owner=users.get(conn, detail["deck"]["owner_id"]), **detail,
    )


# Deck notes are private to the owner and readable as text, so an assistant
# can pick them up later.

@router.post("/decks/{deck_id}/notes")
async def deck_save_notes(deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    require_own_deck(conn, deck_id, me(request))
    text = str((await request.form()).get("notes", ""))
    try:
        notice = deck_service.save_notes(conn, deck_id, text)
    except ServiceError as exc:
        return redirect(f"/decks/{deck_id}", error=str(exc))
    return redirect(f"/decks/{deck_id}", notice=notice)


@router.get("/decks/{deck_id}/notes.txt", response_class=PlainTextResponse)
def deck_notes_text(deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return require_own_deck(conn, deck_id, me(request))["notes"] or ""


@router.post("/decks/{deck_id}/commander")
async def deck_set_commander(
    deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
):
    require_own_deck(conn, deck_id, me(request))
    form = await request.form()
    try:
        notice = deck_service.set_commander(conn, deck_id, str(form.get("commander", "")))
    except ServiceError as exc:
        return redirect(f"/decks/{deck_id}", error=str(exc))
    return redirect(f"/decks/{deck_id}", notice=notice)


@router.get("/decks/{deck_id}/versions/{version}", response_class=HTMLResponse)
def deck_version(
    deck_id: int, version: int, request: Request, view: str = "",
    conn: sqlite3.Connection = Depends(get_conn),
):
    deck = conn.execute("SELECT * FROM decks WHERE id = ?", (deck_id,)).fetchone()
    lot_ids = deckmods.version_lot_ids(conn, deck_id, version) if deck else None
    if lot_ids is None:
        raise HTTPException(status_code=404, detail="No such deck version")
    rows = queries.lots_as_rows(conn, lot_ids, where=f"On the v{version} list",
                                wants=deckmods.version_wants(conn, deck_id, version),
                                viewer_id=me(request).id)
    info = next(v for v in deckmods.versions(conn, deck_id) if v["version"] == version)
    return render(
        request, "deck_version.html", deck=deck, version=info,
        identity=queries.deck_identities(conn).get(deck_id, ()),
        can_edit=deck["owner_id"] == me(request).id,
        sections=queries.sections_by_type(rows), total=sum(r["qty"] for r in rows),
    )
