"""Deck exports: decklist, shopping list, per-friend borrow lists."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from mtgtracker.services import NotFoundError
from mtgtracker.services import exports as export_service
from mtgtracker.web import queries
from mtgtracker.web.deps import get_conn, me, render

router = APIRouter()


def export_detail(request: Request, conn: sqlite3.Connection, deck_id: int) -> dict:
    detail = queries.get_deck(conn, deck_id, viewer_id=me(request).id)
    if detail is None:
        raise HTTPException(status_code=404, detail="No such deck")
    return detail


@router.get("/decks/{deck_id}/export", response_class=HTMLResponse)
def deck_export_page(deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """The deck as text to copy or download: decklist, shopping list, and
    what to borrow from each friend. Read-only, so friends can use it too."""
    detail = export_detail(request, conn, deck_id)
    return render(
        request, "deck_export.html", deck=detail["deck"], identity=detail["identity"],
        exports=queries.deck_exports(detail),
    )


@router.get("/decks/{deck_id}/export/{kind}.txt", response_class=PlainTextResponse)
def deck_export_text(
    deck_id: int, kind: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)
):
    detail = export_detail(request, conn, deck_id)
    try:
        text, filename = export_service.export_text(detail, kind)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
