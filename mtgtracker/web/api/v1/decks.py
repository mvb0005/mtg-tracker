"""Decks over JSON: read a deck, create one, edit it as one version, notes,
commander, exports and version history. Same guards and services as the web
pages; friends can read, only the owner writes."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, model_validator

from mtgtracker import deckmods, users
from mtgtracker.services import decks as deck_service
from mtgtracker.services import exports as export_service
from mtgtracker.services import modify as modify_service
from mtgtracker.web import queries
from mtgtracker.web.api.v1.common import ApiError, write
from mtgtracker.web.deps import (
    get_conn,
    load_deck,
    load_person,
    me,
    require_own_deck,
    require_own_lot,
)

router = APIRouter()


def _strict() -> ConfigDict:
    return ConfigDict(extra="forbid")


def _counts(row: dict) -> dict:
    return {"list": row["list_count"], "here": row["equipped_count"], "away": row["elsewhere_count"],
            "wanted": row["wanted_count"]}


def deck_summary(row: dict) -> dict:
    """One deck of queries.list_decks, listing fields explicitly (a row also
    carries the owner's private notes)."""
    return {
        "id": row["id"], "name": row["name"], "owner_id": row["owner_id"], "format": row["format"],
        "commander": row["commander"], "version": row["version"], "counts": _counts(row),
    }


def _pending_json(conn: sqlite3.Connection, deck_id: int) -> list[dict]:
    out = []
    for change in deckmods.pending_changes(conn, deck_id):
        holder = ({"id": change["add_from_deck_id"], "name": change["add_from_deck_name"]}
                  if change["add_from_deck_id"] else None)
        cut = None
        if change["cut_lot_id"] is not None or change["cut_want_id"] is not None:
            cut = {"name": change["cut_card_name"], "lot_id": change["cut_lot_id"],
                   "want_id": change["cut_want_id"]}
        out.append({
            "change_id": change["id"],
            "add": {"name": change["add_card_name"], "lot_id": change["add_lot_id"],
                    "scryfall_id": change["add_scryfall_id"], "owned": change["ready"], "held_by": holder},
            "cut": cut,
        })
    return out


def deck_json(conn: sqlite3.Connection, deck_id: int, viewer: users.User) -> dict:
    """The deck for the API, built from queries.get_deck so it agrees with
    the deck page. Money: a lot's cost_cents appears only when the viewer
    owns the lot. Notes never appear (their own endpoint, owner only), and
    the Modify queue is the owner's."""
    detail = queries.get_deck(conn, deck_id, viewer_id=viewer.id)
    if detail is None:
        raise HTTPException(status_code=404, detail="No such deck")
    deck = detail["deck"]
    owner = users.get(conn, deck["owner_id"])
    mine = {
        row[0]: row[1] for row in conn.execute(
            "SELECT lots.id, lots.allocated_cost_cents FROM deck_cards"
            " JOIN lots ON lots.id = deck_cards.lot_id"
            " WHERE deck_cards.deck_id = ? AND lots.owner_id = ?", (deck_id, viewer.id),
        )
    }
    cards, friends = [], {}
    for g in detail["groups"]:
        friends[g["card_name"]] = g["friends"]
        if g["wanted"]:
            continue
        here = deck_id == g["current_deck_id"]
        holder = None
        if g["current_deck_id"]:
            holder = {"id": g["current_deck_id"], "name": g["current_deck_name"]}
        for lot_id in g["lot_ids"]:
            card = {
                "lot_id": lot_id, "name": g["card_name"], "scryfall_id": g["scryfall_id"],
                "set": g["set_code"], "collector_number": g["collector_number"], "finish": g["finish"],
                "equipped_here": here, "equipped_in": holder, "needs_printing": bool(g["needs_input"]),
                "friends": g["friends"],
            }
            if lot_id in mine:
                card["cost_cents"] = mine[lot_id]
            cards.append(card)
    cards.sort(key=lambda c: (c["name"].casefold(), c["lot_id"]))
    wants = [
        {"want_id": r["id"], "name": r["card_name"], "scryfall_id": r["scryfall_id"],
         "friends": friends.get(r["card_name"], [])}
        for r in conn.execute(
            "SELECT id, card_name, scryfall_id FROM deck_wants WHERE deck_id = ? ORDER BY card_name, id",
            (deck_id,),
        )
    ]
    is_owner = deck["owner_id"] == viewer.id
    return {
        "id": deck["id"], "name": deck["name"], "format": deck["format"], "owner_id": deck["owner_id"],
        "owner": owner.display_name if owner else None, "commander": deck["commander"],
        "version": deck["version"], "created_at": deck["created_at"], "identity": list(detail["identity"]),
        "counts": {"list": detail["list_count"], "here": detail["equipped_count"],
                   "away": detail["elsewhere_count"], "wanted": detail["wanted_count"]},
        "cards": cards, "wants": wants,
        "pending_changes": _pending_json(conn, deck_id) if is_owner else None,
    }


@router.get("/decks")
def list_decks(request: Request, owner: int | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    """The principal's decks, or ?owner=ID's (read-only)."""
    person = load_person(conn, owner) if owner is not None else me(request)
    return {"owner_id": person.id, "decks": [deck_summary(r) for r in queries.list_decks(conn, person.id)]}


class NewDeck(BaseModel):
    model_config = _strict()
    name: str
    decklist: str


@router.post("/decks", status_code=201)
def create_deck(body: NewDeck, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """A deck from list text: owned copies linked (not moved), wants for the
    rest, the commander from the list's Commander section."""
    user = me(request)

    def create() -> dict:
        summary = deck_service.create_from_list(conn, user, body.name, body.decklist, commit=False)
        return {
            **deck_json(conn, summary.deck_id, user), "notice": summary.message(),
            "import": {"not_found": summary.not_found, "unreadable": summary.unreadable,
                       "skipped_lines": summary.skipped_lines, "owned": summary.owned},
        }

    return write(request, conn, create, status=201)


@router.get("/decks/{deck_id}")
def get_deck(deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return deck_json(conn, deck_id, me(request))


# --- the edit: adds and cuts as one version ------------------------------------

def _exactly_one(model, *fields: str) -> None:
    given = [f for f in fields if getattr(model, f) is not None]
    if len(given) != 1:
        raise ValueError(f"give exactly one of {', '.join(fields)}")


class Add(BaseModel):
    model_config = _strict()
    name: str | None = None
    lot_id: int | None = None

    @model_validator(mode="after")
    def _one(self):
        _exactly_one(self, "name", "lot_id")
        return self


class Cut(BaseModel):
    model_config = _strict()
    lot_id: int | None = None
    want_id: int | None = None
    name: str | None = None

    @model_validator(mode="after")
    def _one(self):
        _exactly_one(self, "lot_id", "want_id", "name")
        return self


class Changes(BaseModel):
    model_config = _strict()
    adds: list[Add]
    cuts: list[Cut] = []
    note: str = ""
    expect_version: int


@router.post("/decks/{deck_id}/changes")
def apply_changes(
    deck_id: int, body: Changes, request: Request, conn: sqlite3.Connection = Depends(get_conn)
):
    """Add cards (by name, planned, or an owned lot_id), pair adds[i] with
    cuts[i], and apply the lot as ONE new version. 409 version_conflict if
    the deck isn't at expect_version, 409 pending_changes if the Modify page
    has a queue, 422 pure_cuts_unsupported if cuts outnumber adds."""
    user = me(request)
    require_own_deck(conn, deck_id, user)
    for item in (*body.adds, *body.cuts):
        require_own_lot(conn, item.lot_id, user)

    def apply() -> dict:
        result = modify_service.apply_batch(
            conn, deck_id, adds=[a.model_dump() for a in body.adds],
            cuts=[c.model_dump() for c in body.cuts], note=body.note, expect_version=body.expect_version,
            commit=False,
        )
        return {"deck_id": deck_id, **result, "notice": f"Applied as version {result['version']}."}

    return write(request, conn, apply)


# --- notes, commander -------------------------------------------------------------

class Notes(BaseModel):
    model_config = _strict()
    notes: str


@router.get("/decks/{deck_id}/notes")
def get_notes(deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    deck = require_own_deck(conn, deck_id, me(request))
    return {"deck_id": deck_id, "notes": deck["notes"] or ""}


@router.put("/decks/{deck_id}/notes")
def put_notes(deck_id: int, body: Notes, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    require_own_deck(conn, deck_id, me(request))

    def save() -> dict:
        notice = deck_service.save_notes(conn, deck_id, body.notes, commit=False)
        notes = conn.execute("SELECT notes FROM decks WHERE id = ?", (deck_id,)).fetchone()[0]
        return {"deck_id": deck_id, "notes": notes or "", "notice": notice}

    return write(request, conn, save)


class Commander(BaseModel):
    model_config = _strict()
    name: str | None = None
    lot_id: int | None = None


@router.post("/decks/{deck_id}/commander")
def set_commander(
    deck_id: int, body: Commander, request: Request, conn: sqlite3.Connection = Depends(get_conn)
):
    """Set the commander by card name or by a lot on the list; {"name": null}
    clears it. Same rules as the web picker."""
    user = me(request)
    require_own_deck(conn, deck_id, user)
    if body.lot_id is not None:
        require_own_lot(conn, body.lot_id, user)
        row = conn.execute(
            "SELECT lots.card_name FROM deck_cards JOIN lots ON lots.id = deck_cards.lot_id"
            " WHERE deck_cards.deck_id = ? AND lots.id = ?", (deck_id, body.lot_id),
        ).fetchone()
        if row is None:
            raise ApiError(422, "rejected", "that card isn't on this deck's list")
        name = row[0]
    elif "name" in body.model_fields_set:
        name = body.name
    else:
        raise ApiError(422, "validation_error", 'give "name" (null clears it) or "lot_id"')

    def save() -> dict:
        notice = deck_service.set_commander(conn, deck_id, name, commit=False)
        commander = conn.execute("SELECT commander FROM decks WHERE id = ?", (deck_id,)).fetchone()[0]
        return {"deck_id": deck_id, "commander": commander, "notice": notice}

    return write(request, conn, save)


# --- exports and versions (readable by friends) ---------------------------------------

@router.get("/decks/{deck_id}/export/{kind}")
def get_export(deck_id: int, kind: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """kind: decklist, shopping, or borrow-<friend id>."""
    detail = queries.get_deck(conn, deck_id, viewer_id=me(request).id)
    if detail is None:
        raise HTTPException(status_code=404, detail="No such deck")
    text, filename = export_service.export_text(detail, kind)  # NotFoundError -> 404
    return {"deck_id": deck_id, "kind": kind, "filename": filename, "text": text}


@router.get("/decks/{deck_id}/versions/{version}")
def get_version(deck_id: int, version: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    load_deck(conn, deck_id)
    lot_ids = deckmods.version_lot_ids(conn, deck_id, version)
    if lot_ids is None:
        raise HTTPException(status_code=404, detail="No such deck version")
    info = next(v for v in deckmods.versions(conn, deck_id) if v["version"] == version)
    cards = []
    for lot_id in sorted(lot_ids):
        lot = conn.execute(
            "SELECT id, card_name, scryfall_id, set_code, collector_number, finish FROM lots WHERE id = ?",
            (lot_id,),
        ).fetchone()
        cards.append({"lot_id": lot["id"], "name": lot["card_name"], "scryfall_id": lot["scryfall_id"],
                      "set": lot["set_code"], "collector_number": lot["collector_number"],
                      "finish": lot["finish"]})
    cards.sort(key=lambda c: (c["name"].casefold(), c["lot_id"]))
    return {
        "deck_id": deck_id, "version": version, "created_at": info["created_at"], "note": info["note"],
        "card_count": info["card_count"], "cards": cards,
        "wants": [{"name": n, "scryfall_id": s} for n, s in deckmods.version_wants(conn, deck_id, version)],
        "added": [{"name": n, "qty": q} for n, q in info["added"]],
        "removed": [{"name": n, "qty": q} for n, q in info["removed"]],
    }
