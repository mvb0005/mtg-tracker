"""Dashboard, purchases, and the collection."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from mtgtracker import users
from mtgtracker.web import filters, queries
from mtgtracker.web.deps import get_conn, me, render

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    user = me(request)
    return render(
        request, "dashboard.html",
        stats=queries.dashboard_stats(conn, user.id),
        transactions=queries.list_transactions(conn, user.id)[:5],
        decks=queries.list_decks(conn, user.id),
        needs_input=queries.needs_input(conn, user.id),
    )


@router.get("/transactions", response_class=HTMLResponse)
def transactions(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return render(
        request, "transactions.html", transactions=queries.list_transactions(conn, me(request).id)
    )


@router.get("/transactions/{transaction_id}", response_class=HTMLResponse)
def transaction_detail(
    transaction_id: int, request: Request, notice: str = "", conn: sqlite3.Connection = Depends(get_conn)
):
    detail = queries.get_transaction(conn, transaction_id)
    # Purchases are private: someone else's is simply not found.
    if detail is None or detail["txn"]["owner_id"] != me(request).id:
        raise HTTPException(status_code=404, detail="No such transaction")
    return render(request, "transaction_detail.html", notice=notice, **detail)


def collection_page(
    request: Request, conn: sqlite3.Connection, owner: users.User, *, q, view, sort, color, color_mode,
    type, mv, deck, action: str,
) -> HTMLResponse:
    """A user's collection (/cards for your own, /people/{id}/cards for a
    friend's, read-only and without cost)."""
    mine = owner.id == me(request).id
    decks = queries.list_decks(conn, owner.id)
    card_filters = filters.CardFilters.parse(
        sort=sort, color=color, color_mode=color_mode, type=type, mv=mv, deck=deck,
        deck_ids={d["id"] for d in decks},
    )
    # Filter individual copies first (so a deck filter counts only that
    # deck's copies), then merge copies of the same card into one row.
    copies = queries.search_cards(conn, owner.id, q)
    if not mine:
        copies = [{**row, "cost_cents": None} for row in copies]
    matching = [row for row in copies if card_filters.matches(row)]
    card_rows = sorted(queries.collapse_by_name(matching), key=card_filters.sort_key)
    return render(
        request, "cards.html", card_rows=card_rows, sections=queries.sections_by_type(card_rows),
        q=q, view=view, filters=card_filters, decks=decks,
        deck_options={**{str(d["id"]): d["name"] for d in decks}, filters.NO_DECK: "Not in a deck"},
        facets=card_filters.facet_counts(copies),
        total=queries.count_cards(conn, owner.id), shown=sum(r["qty"] for r in card_rows),
        owner=owner, can_edit=mine, action=action,
    )


@router.get("/cards", response_class=HTMLResponse)
def cards(
    request: Request, q: str = "", view: str = "",
    sort: str = "", color: list[str] = Query([]), color_mode: str = "", type: list[str] = Query([]),
    mv: list[str] = Query([]), deck: list[str] = Query([]),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return collection_page(request, conn, me(request), q=q, view=view, sort=sort, color=color,
                           color_mode=color_mode, type=type, mv=mv, deck=deck, action="/cards")
