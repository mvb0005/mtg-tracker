"""Suggested cards for a deck (owner only): compare each against the card it
would replace, then accept it onto the Modify page or dismiss it."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from mtgtracker import cardinfo, deckmods, suggestions
from mtgtracker.services import ServiceError
from mtgtracker.services import suggestions as service
from mtgtracker.web import queries
from mtgtracker.web.deps import get_conn, me, redirect, render, require_own_deck

router = APIRouter()


def _facts(art: cardinfo.CardInfo) -> dict:
    face = art.faces[0] if art.faces else {}
    return {"mana_cost": face.get("mana_cost") or None, "cmc": art.cmc,
            "type_line": (art.type_line or "").split("//")[0].strip() or None}


def _theme_summary(items: list[dict], picks: list[dict]) -> dict:
    """Totals for taking one option per slot (the first): alternatives for
    the same card aren't added together."""
    priced = [s["price_cents"] for s in picks if s["price_cents"] is not None]
    deltas = [s["mv_delta"] for s in picks if s["mv_delta"] is not None]
    return {"count": len(items), "picks": len(picks), "price_cents": sum(priced) if priced else None,
            "unpriced": len(picks) - len(priced), "mv_delta": sum(deltas) if deltas else None}


@router.get("/decks/{deck_id}/suggestions", response_class=HTMLResponse)
def deck_suggestions(
    deck_id: int, request: Request, notice: str = "", error: str = "",
    conn: sqlite3.Connection = Depends(get_conn),
):
    user = me(request)
    deck = require_own_deck(conn, deck_id, user)
    by_name: dict[str, dict] = {}
    for group in queries.get_deck(conn, deck_id, viewer_id=user.id)["groups"]:
        by_name.setdefault(group["card_name"], group)
    friend_copies = queries.FriendCopies(conn, {user.id})

    open_items, decided = [], []
    for s in suggestions.list_for(conn, deck_id):
        art = cardinfo.lookup(s["scryfall_id"], s["card_name"], "nonfoil")
        owned = deckmods.owned_copies_of(conn, deck_id, s["card_name"]) if s["state"] == "open" else []
        holders = sorted({c["holder_name"] or "your collection" for c in owned})
        copies = sum(c["qty"] for c in owned)
        s["add_row"] = {
            "card_name": s["card_name"], "art": art, "qty": 1, "finish": "nonfoil", "set_code": None,
            "collector_number": None, "cost_cents": None, "decks": [], "disposition_type": None,
            "where": (f"Suggested; you have {copies}, in {' and '.join(holders)}" if owned
                      else "Suggested; not owned yet"),
            "printing": "Not owned yet" if not owned else None,
            "friends": friend_copies(s["card_name"]),
        }
        s["owned"], s["owned_in"] = copies, holders
        s["add"] = _facts(art)
        cut = by_name.get(s["cut_card_name"]) if s["cut_card_name"] else None
        s["cut_row"] = {**cut, "qty": 1} if cut else None
        s["cut"] = _facts(cut["art"]) if cut else None
        s["mv_delta"] = (
            s["add"]["cmc"] - s["cut"]["cmc"]
            if s["cut"] and s["add"]["cmc"] is not None and s["cut"]["cmc"] is not None else None
        )
        (open_items if s["state"] == "open" else decided).append(s)

    # Themes, newest batch first; within one, a slot per replaced card so
    # alternatives for the same slot sit together (plain additions last).
    themes: dict[str, list[dict]] = {}
    for s in sorted(open_items, key=lambda s: -s["id"]):
        themes.setdefault(s["theme"] or "Suggestions", []).append(s)
    theme_list = []
    for title, items in themes.items():
        slots: dict[str | None, list[dict]] = {}
        for s in sorted(items, key=lambda s: s["id"]):
            slots.setdefault(s["cut_card_name"], []).append(s)
        ordered = sorted(slots.items(), key=lambda kv: (kv[0] is None, (kv[0] or "").casefold()))
        picks = [group[0] for cut, group in ordered if cut is not None] + (slots.get(None) or [])
        theme_list.append({
            "title": title, "summary": _theme_summary(items, picks),
            "slots": [{"cut_card_name": cut, "cut_row": group[0]["cut_row"], "cut": group[0]["cut"],
                       "items": group} for cut, group in ordered],
        })
    decided.sort(key=lambda s: s["decided_at"] or s["created_at"], reverse=True)
    return render(
        request, "deck_suggestions.html", can_edit=True, deck=deck,
        identity=queries.deck_identities(conn).get(deck_id, ()),
        themes=theme_list, open_count=len(open_items), decided=decided,
        pending=len(deckmods.pending_changes(conn, deck_id)), notice=notice, error=error,
    )


def _act(request: Request, conn: sqlite3.Connection, deck_id: int, action: Callable[[], str]):
    require_own_deck(conn, deck_id, me(request))
    try:
        notice = action()
    except ServiceError as exc:
        return redirect(f"/decks/{deck_id}/suggestions", error=str(exc))
    return redirect(f"/decks/{deck_id}/suggestions", notice=notice)


@router.post("/decks/{deck_id}/suggestions/{suggestion_id}/accept")
def accept(deck_id: int, suggestion_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _act(request, conn, deck_id, lambda: service.accept(conn, deck_id, suggestion_id))


@router.post("/decks/{deck_id}/suggestions/{suggestion_id}/dismiss")
def dismiss(deck_id: int, suggestion_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _act(request, conn, deck_id, lambda: service.dismiss(conn, deck_id, suggestion_id))


@router.post("/decks/{deck_id}/suggestions/{suggestion_id}/reopen")
def reopen(deck_id: int, suggestion_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _act(request, conn, deck_id, lambda: service.reopen(conn, deck_id, suggestion_id))
