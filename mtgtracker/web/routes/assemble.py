"""Assemble a deck: put the cards you have onto its list, physically. A
checklist of what can happen, a preview of the result, then the write."""

from __future__ import annotations

import sqlite3
from collections import Counter

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from mtgtracker import assembly, cardinfo
from mtgtracker.services import ServiceError
from mtgtracker.services import assembly as service
from mtgtracker.web import queries
from mtgtracker.web.deps import get_conn, me, redirect, render, require_own_deck

router = APIRouter()

SECTIONS = [
    (assembly.FILL, "Fill from your collection",
     "Cards the list wants that you already own and that are in no deck."),
    (assembly.SWAP, "Swap in a free copy",
     "The list names a copy that is in another deck, but you have another copy of the same card in no deck. "
     "Use that one here; the other deck keeps its card."),
    (assembly.EQUIP, "Put in the deck",
     "On this list but in no deck. Nothing leaves another deck."),
    (assembly.PULL, "Pull from another deck",
     "Taking one of these takes the card out of the deck that holds it, which will then show it as away. "
     "Nothing here is checked until you choose it."),
]
WHERE = {
    assembly.FILL: lambda ln: ln.source or "from your collection",
    assembly.SWAP: lambda ln: (
        f"{ln.source or 'from your collection'}; the copy in {ln.from_deck_name} stays there"),
    assembly.EQUIP: lambda ln: "on the list, in no deck",
    assembly.PULL: lambda ln: f"now in {ln.from_deck_name}",
}


def _row(line: assembly.Line, qty: int = 1) -> dict:
    """A plan line as the display row card_face expects."""
    art = cardinfo.lookup(line.scryfall_id, line.card_name, line.finish)
    wanted = line.category in (assembly.BORROW, assembly.ACQUIRE)
    where = line.reason or WHERE.get(line.category, lambda ln: "")(line)
    if line.category == assembly.BORROW:
        where = "Not owned yet: a friend has it"
    elif line.category == assembly.ACQUIRE:
        where = "Not owned yet: need to acquire"
    return {
        "card_name": line.card_name, "art": art, "qty": qty, "finish": line.finish,
        "set_code": line.set_code, "collector_number": line.collector_number, "cost_cents": None,
        "decks": [], "disposition_type": None, "wanted": wanted, "where": where,
        "friends": line.friends, **({"printing": "Not owned yet"} if wanted else {}),
        "category": art.category(line.card_name),
    }


def _items(plan: assembly.AssemblyPlan) -> dict[str, list[dict]]:
    items: dict[str, list[dict]] = {}
    for category, _, _ in SECTIONS:
        items[category] = [
            {"line": ln, "row": _row(ln), "where": WHERE[category](ln)} for ln in plan.of(category)
        ]
    for category in (assembly.BORROW, assembly.ACQUIRE):
        by_name: dict[str, list[assembly.Line]] = {}
        for ln in plan.of(category):
            by_name.setdefault(ln.card_name, []).append(ln)
        items[category] = [{"row": _row(group[0], len(group))} for group in by_name.values()]
    items["unavailable"] = [
        {"row": _row(ln), "reason": ln.reason or "can't be acted on"} for ln in plan.unavailable
        if ln.reason
    ]
    return items


def _page(
    request, conn, deck_id, *, error="", notice="", preview=None, chosen=None, note="", status_code=200
):
    user = me(request)
    deck = require_own_deck(conn, deck_id, user)
    plan = assembly.plan(conn, deck_id)
    identity = queries.deck_identities(conn).get(deck_id, ())
    return render(
        request, "deck_assemble.html", status_code=status_code, can_edit=True, deck=deck, identity=identity,
        plan=plan, sections=SECTIONS, items=_items(plan), error=error, notice=notice,
        preview=preview, chosen=chosen, note=note,
        counts_now=plan.counts, borrow_count=len(plan.of(assembly.BORROW)),
        acquire_count=len(plan.of(assembly.ACQUIRE)),
    )


@router.get("/decks/{deck_id}/assemble", response_class=HTMLResponse)
def deck_assemble(
    deck_id: int, request: Request, notice: str = "", error: str = "",
    conn: sqlite3.Connection = Depends(get_conn),
):
    return _page(request, conn, deck_id, notice=notice, error=error)


async def _read(request: Request) -> tuple[list[str], str]:
    form = await request.form()
    return [str(v) for v in form.getlist("line")], str(form.get("note", ""))


@router.post("/decks/{deck_id}/assemble/preview", response_class=HTMLResponse)
async def deck_assemble_preview(
    deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
):
    """What assembling the chosen lines would leave. Writes nothing, so it
    leaves no audit row or restore point."""
    request.state.no_audit = True
    require_own_deck(conn, deck_id, me(request))
    ids, note = await _read(request)
    plan = assembly.plan(conn, deck_id)
    ids = list(dict.fromkeys(ids))
    stale = [i for i in ids if i not in plan.actionable()]
    if not ids or stale:
        error = "Choose at least one step." if not ids else (
            f"The plan has changed; these steps no longer apply: {', '.join(stale)}.")
        return _page(request, conn, deck_id, error=error, status_code=422)
    lines = plan.selected(ids)
    tally = Counter(ln.category for ln in lines)
    preview = {
        "lines": lines, "counts_after": plan.counts_after(ids), "pull_list": plan.pull_list(ids),
        "tally": tally, "ids": ids,
        "pulls": tally[assembly.PULL],
    }
    return _page(request, conn, deck_id, preview=preview, chosen=set(ids), note=note)


@router.post("/decks/{deck_id}/assemble")
async def deck_assemble_apply(deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    require_own_deck(conn, deck_id, me(request))
    ids, note = await _read(request)
    try:
        result = service.assemble(conn, deck_id, ids, note=note)
    except ServiceError as exc:
        conn.rollback()
        return redirect(f"/decks/{deck_id}/assemble", error=str(exc))
    return redirect(
        f"/decks/{deck_id}", notice=result.message(), pl=assembly.encode_pull_list(result.pull_list)
    )
