"""Cards across collections: who owns what, and a person's lots."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query, Request

from mtgtracker.web import queries
from mtgtracker.web.deps import get_conn, load_person, me

router = APIRouter()


@router.get("/cards/owners")
def card_owners(
    request: Request, name: list[str] = Query(default=[]), conn: sqlite3.Connection = Depends(get_conn)
):
    """?name=A&name=B: every owner's non-disposed copies of each card (the
    caller's too) and where each is: in a deck, or not in a deck. Names match
    by front face too. No money."""
    names = [n.strip() for n in name if n.strip()]
    return {"cards": queries.card_owners(conn, names)}


@router.get("/collection")
def collection(
    request: Request, owner: int | None = None, conn: sqlite3.Connection = Depends(get_conn)
):
    """A person's lots (yours by default; anyone's is readable). cost_cents
    appears only on lots the caller owns."""
    viewer = me(request)
    person = load_person(conn, owner) if owner is not None else viewer
    lots = []
    for row in conn.execute(
        """
        SELECT lots.id, lots.card_name, lots.scryfall_id, lots.set_code, lots.collector_number, lots.finish,
               lots.owner_id, lots.collection_import_id, lots.allocated_cost_cents,
               dispositions.id IS NOT NULL AS disposed,
               current_deck.id AS deck_id, current_deck.name AS deck
        FROM lots
        LEFT JOIN dispositions ON dispositions.lot_id = lots.id
        LEFT JOIN deck_cards AS current ON current.lot_id = lots.id AND current.equipped = 1
        LEFT JOIN decks AS current_deck ON current_deck.id = current.deck_id
        WHERE lots.owner_id = ?
        ORDER BY lots.card_name COLLATE NOCASE, lots.id
        """,
        (person.id,),
    ):
        lot = {
            "lot_id": row["id"], "name": row["card_name"], "scryfall_id": row["scryfall_id"],
            "set": row["set_code"], "collector_number": row["collector_number"], "finish": row["finish"],
            "where": {"id": row["deck_id"], "name": row["deck"]} if row["deck_id"] else None,
            "disposed": bool(row["disposed"]), "import_id": row["collection_import_id"],
        }
        if row["owner_id"] == viewer.id:
            lot["cost_cents"] = row["allocated_cost_cents"]
        lots.append(lot)
    return {"owner_id": person.id, "count": len(lots), "lots": lots}
