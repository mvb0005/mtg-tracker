"""Deck modifications: the Modify page and the POSTs that edit the change
list or apply it as a new version."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from mtgtracker import cardinfo, deckmods, users
from mtgtracker.services import ServiceError
from mtgtracker.services import modify as modify_service
from mtgtracker.web import queries
from mtgtracker.web.deps import (
    form_int,
    get_conn,
    me,
    redirect,
    render,
    require_own_deck,
    require_own_lot,
)

router = APIRouter()


def modify_page(
    request: Request, conn: sqlite3.Connection, deck_id: int,
    *, q: str = "", notice: str = "", error: str = "", status_code: int = 200,
) -> HTMLResponse:
    user = me(request)
    deck = require_own_deck(conn, deck_id, user)
    # Display rows (same shape as the deck page's) so changes, the
    # replacement picker and search results render as readable card
    # tiles with the shared hover panel / tap sheet.
    groups = queries.get_deck(conn, deck_id, viewer_id=user.id)["groups"]
    by_name: dict[str, dict] = {}
    for group in groups:
        by_name.setdefault(group["card_name"], group)

    pending = deckmods.pending_changes(conn, deck_id)
    for change in pending:
        art = cardinfo.lookup(change["add_scryfall_id"], change["add_card_name"],
                              change["add_finish"] or "nonfoil")
        if not change["ready"]:
            where = "Not owned yet (to acquire)"
        elif change["add_from_deck_name"]:
            where = f"In {change['add_from_deck_name']} (would be borrowed)"
        else:
            where = "In your collection, not in a deck"
        change["insert_row"] = {
            "card_name": change["add_card_name"], "art": art, "qty": 1,
            "finish": change["add_finish"] or "nonfoil", "set_code": change["add_set_code"],
            "collector_number": change["add_collector_number"], "cost_cents": change["add_cost_cents"],
            "where": where, "decks": [], "disposition_type": None,
        }
        if not change["ready"]:
            change["insert_row"]["printing"] = "Not owned yet"
        cut = by_name.get(change["cut_card_name"]) if change["cut_card_name"] else None
        change["cut_row"] = {**cut, "qty": 1} if cut else None
        change["owned_copies"] = (
            [] if change["ready"] else deckmods.owned_copies_of(conn, deck_id, change["add_card_name"])
        )

    identity = queries.deck_identities(conn).get(deck_id, ())
    friend_copies = queries.FriendCopies(conn, {user.id})
    for change in pending:
        if not change["ready"]:
            change["insert_row"]["friends"] = friend_copies(change["add_card_name"])
    # Cards the deck lists but you don't own, each replaceable by one you do.
    wanted = []
    for group in deckmods.wanted_groups(conn, deck_id):
        art = cardinfo.lookup(group["scryfall_id"], group["card_name"], "nonfoil")
        row = {
            "card_name": group["card_name"], "art": art, "qty": group["qty"], "finish": "nonfoil",
            "set_code": None, "collector_number": None, "cost_cents": None, "decks": [],
            "disposition_type": None, "category": art.category(group["card_name"]),
            "wanted": True, "where": "Not owned yet: need to acquire", "printing": "Not owned yet",
            "friends": friend_copies(group["card_name"]),
        }
        wanted.append({**group, "row": row})
    owned_rows = []
    for item in deckmods.owned_fitting(conn, deck_id, identity):
        owned_rows.append({
            **item, "category": item["art"].category(item["card_name"]), "decks": [],
            "disposition_type": None,
            "where": (f"In {item['holder_name']}" if item["holder_name"]
                      else "In your collection, not in a deck"),
        })
    owned_rows.sort(key=lambda r: cardinfo.mana_value_order(r["card_name"], r["art"]))

    cut_choices = []
    for option in deckmods.cut_options(conn, deck_id):
        row = by_name.get(option["card_name"])
        if row:
            cut_choices.append({**row, "cut_lot_id": option["lot_id"], "qty": option["qty"]})
    cut_choices.sort(key=lambda r: cardinfo.mana_value_order(r["card_name"], r["art"]))

    ready = [c for c in pending if c["ready"]]
    size = deckmods.list_size(conn, deck_id)
    candidates = deckmods.owned_candidates(conn, deck_id, q)
    for candidate in candidates:
        candidate.update(
            art=cardinfo.lookup(candidate["scryfall_id"], candidate["card_name"], candidate["finish"]),
            where=(f"{candidate['qty']} in {candidate['holder_name']}" if candidate["holder_name"]
                   else f"{candidate['qty']} not in a deck"),
            decks=[], disposition_type=None, cost_cents=None,
        )
    return render(
        request, "deck_modify.html", status_code=status_code, can_edit=True,
        identity=identity,
        deck=deck, pending=pending, ready_count=len(ready),
        size=size, size_after=size + len(pending) - sum(1 for c in pending if c["cut_lot_id"]),
        cut_options=deckmods.cut_options(conn, deck_id),
        cut_sections=queries.sections_by_type(cut_choices),
        wanted=[w for w in wanted if w["want_id"] is not None],
        wanted_total=sum(w["qty"] for w in wanted),
        own_sections=queries.sections_by_type(owned_rows), own_count=len(owned_rows),
        candidates=candidates, q=q,
        versions=deckmods.versions(conn, deck_id),
        notice=notice, error=error,
    )


@router.get("/decks/{deck_id}/modify", response_class=HTMLResponse)
def deck_modify(
    deck_id: int, request: Request, q: str = "", notice: str = "", error: str = "",
    conn: sqlite3.Connection = Depends(get_conn),
):
    return modify_page(request, conn, deck_id, q=q, notice=notice, error=error)


def change_action(
    deck_id: int, conn: sqlite3.Connection, action: Callable[[], str], q: str = "", *, user: users.User
):
    """Run a deck_changes edit on the user's own deck; redirect back to the
    modify page with the service's notice, or with the error if it was
    rejected."""
    require_own_deck(conn, deck_id, user)
    try:
        notice = action()
    except ServiceError as exc:
        return redirect(f"/decks/{deck_id}/modify", error=str(exc), q=q)
    return redirect(f"/decks/{deck_id}/modify", notice=notice, q=q)


@router.post("/decks/{deck_id}/changes")
async def deck_add_change(deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    user = me(request)
    form = await request.form()
    q = str(form.get("q", ""))
    lot_id = form_int(form.get("lot_id"))
    require_own_lot(conn, lot_id, user)
    return change_action(
        deck_id, conn,
        lambda: modify_service.add_change(
            conn, deck_id, lot_id=lot_id, planned_name=str(form.get("planned_name", ""))
        ),
        q, user=user,
    )


@router.post("/decks/{deck_id}/import")
async def deck_import_list(deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    text = str((await request.form()).get("decklist", ""))
    return change_action(deck_id, conn, lambda: modify_service.import_list(conn, deck_id, text),
                         user=me(request))


@router.post("/decks/{deck_id}/changes/{change_id}/cut")
async def deck_change_cut(
    deck_id: int, change_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
):
    user = me(request)
    form = await request.form()
    cut, cut_want = form_int(form.get("cut_lot_id")), form_int(form.get("cut_want_id"))
    require_own_lot(conn, cut, user)
    return change_action(
        deck_id, conn, lambda: modify_service.set_cut(conn, deck_id, change_id, cut, cut_want), user=user
    )


@router.post("/decks/{deck_id}/swaps")
async def deck_swap(deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Replace a card the deck lists but you don't own with one you do."""
    user = me(request)
    form = await request.form()
    lot_id, want_id = form_int(form.get("lot_id")), form_int(form.get("want_id"))
    require_own_lot(conn, lot_id, user)
    return change_action(
        deck_id, conn, lambda: modify_service.add_swap(conn, deck_id, lot_id, want_id), user=user
    )


@router.post("/decks/{deck_id}/changes/{change_id}/link")
async def deck_change_link(
    deck_id: int, change_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
):
    user = me(request)
    lot_id = form_int((await request.form()).get("lot_id"))
    require_own_lot(conn, lot_id, user)
    return change_action(
        deck_id, conn, lambda: modify_service.link_owned(conn, deck_id, change_id, lot_id), user=user
    )


@router.post("/decks/{deck_id}/changes/{change_id}/delete")
def deck_change_delete(
    deck_id: int, change_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
):
    return change_action(deck_id, conn, lambda: modify_service.remove_change(conn, deck_id, change_id),
                         user=me(request))


@router.post("/decks/{deck_id}/apply")
async def deck_apply(deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    note = str((await request.form()).get("note", ""))
    return change_action(deck_id, conn, lambda: modify_service.apply(conn, deck_id, note),
                         user=me(request))
