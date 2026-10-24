"""Deck suggestions over JSON: list them, add a batch (the system user's way
of proposing cards to compare on /decks/{id}/suggestions), accept, dismiss,
reopen. Owner only (friends get 403): reasons are deck strategy, like notes."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from mtgtracker import suggestions
from mtgtracker.services import suggestions as service
from mtgtracker.web.api.v1.common import write
from mtgtracker.web.deps import actor, get_conn, me, require_own_deck

router = APIRouter()


class SuggestionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    replaces: str | None = None
    reason: str | None = None
    theme: str | None = None
    price_cents: int | None = Field(default=None, ge=0)


class SuggestionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suggestions: list[SuggestionIn]


def _listing(conn: sqlite3.Connection, deck_id: int) -> dict:
    return {"deck_id": deck_id, "suggestions": suggestions.list_for(conn, deck_id)}


@router.get("/decks/{deck_id}/suggestions")
def list_suggestions(deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Every suggestion with its state: open, queued, applied or dismissed.
    Read the dismissed ones before suggesting more."""
    require_own_deck(conn, deck_id, me(request))
    return _listing(conn, deck_id)


@router.post("/decks/{deck_id}/suggestions", status_code=201)
def create_suggestions(
    deck_id: int, body: SuggestionsIn, request: Request, conn: sqlite3.Connection = Depends(get_conn)
):
    """Add suggestions. 422 cards_not_found (with names) if Scryfall doesn't
    know a card; cards already on the list, already suggested for that slot
    or dismissed before come back in `skipped`."""
    require_own_deck(conn, deck_id, me(request))

    def create() -> dict:
        result = service.create(
            conn, deck_id, [s.model_dump() for s in body.suggestions], suggested_by=actor(request).id,
            commit=False,
        )
        return {**result, **_listing(conn, deck_id)}

    return write(request, conn, create, status=201)


def _decide(request: Request, conn: sqlite3.Connection, deck_id: int, suggestion_id: int, fn):
    require_own_deck(conn, deck_id, me(request))

    def run() -> dict:
        notice = fn(conn, deck_id, suggestion_id, commit=False)
        return {"notice": notice, "suggestion": suggestions.get(conn, deck_id, suggestion_id)}

    return write(request, conn, run)


@router.post("/decks/{deck_id}/suggestions/{suggestion_id}/accept")
def accept(deck_id: int, suggestion_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Queue it on the Modify page (a planned add replacing its cut card).
    That pending change makes POST /decks/{id}/changes answer 409
    pending_changes until it is applied or removed there."""
    return _decide(request, conn, deck_id, suggestion_id, service.accept)


@router.post("/decks/{deck_id}/suggestions/{suggestion_id}/dismiss")
def dismiss(deck_id: int, suggestion_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _decide(request, conn, deck_id, suggestion_id, service.dismiss)


@router.post("/decks/{deck_id}/suggestions/{suggestion_id}/reopen")
def reopen(deck_id: int, suggestion_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _decide(request, conn, deck_id, suggestion_id, service.reopen)
