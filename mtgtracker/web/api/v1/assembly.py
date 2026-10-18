"""Deck assembly over JSON: read the plan that turns a deck's list into a
physical deck from the owner's copies, then apply chosen lines. Owner only
(friends get 403). Pulls take a card out of another deck, so they have their
own @risky endpoint; a system actor confirms those with a token."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from mtgtracker import assembly
from mtgtracker.confirm import risky
from mtgtracker.services import CodedError
from mtgtracker.services import assembly as service
from mtgtracker.web import deps
from mtgtracker.web.api.v1.common import Fields, write
from mtgtracker.web.deps import get_conn, me, require_own_deck

router = APIRouter()


class PullsNeedConfirm(CodedError):
    code = "use_pulls_endpoint"
    status = 422


class NoPulls(CodedError):
    code = "no_pulls"
    status = 422


class AssembleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_ids: list[str]
    note: str | None = None


def _line_json(line: assembly.Line) -> dict:
    out = {
        "line_id": line.line_id, "category": line.category, "card": line.card_name, "lot_id": line.lot_id,
        "old_lot_id": line.old_lot_id, "want_id": line.want_id, "scryfall_id": line.scryfall_id,
        "set_code": line.set_code, "collector_number": line.collector_number, "finish": line.finish,
        "needs_printing": line.needs_printing, "source": line.source, "risky": line.risky,
        "default_selected": line.default_selected,
    }
    if line.from_deck_id is not None:
        out["from"] = {"deck_id": line.from_deck_id, "name": line.from_deck_name}
    if line.friends:
        out["friends"] = line.friends
    return out


def plan_json(plan: assembly.AssemblyPlan) -> dict:
    return {
        "deck_id": plan.deck_id, "counts": plan.counts,
        "summary": {c: len(plan.of(c)) for c in assembly.CATEGORIES},
        "lines": [_line_json(line) for line in plan.lines],
        "pull_list": plan.pull_list(list(plan.actionable())),
        "acquire": [{"name": n, "qty": q} for n, q in plan.acquire],
        "borrow": [{"name": n, "qty": q} for n, q in plan.borrow],
    }


def result_json(result: assembly.AssemblyResult) -> dict:
    return {
        "deck_id": result.deck_id, "version": result.version, "filled": result.filled,
        "swapped": result.swapped, "pulled": result.pulled, "equipped": result.equipped,
        "counts_before": result.counts_before, "counts_after": result.counts_after,
        "pull_list": result.pull_list, "line_ids": result.line_ids,
    }


@router.get("/decks/{deck_id}/assembly")
def get_assembly(deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """The assembly plan: every entry on the list in one category (here, fill,
    swap, equip, pull, borrow, acquire), each actionable line with a stable
    `line_id`, plus the pull list by physical place. Reads only."""
    require_own_deck(conn, deck_id, me(request))
    return JSONResponse(jsonable_encoder(plan_json(service.plan(conn, deck_id))))


@router.post("/decks/{deck_id}/assembly")
def apply_assembly(
    deck_id: int, body: AssembleIn, request: Request, conn: sqlite3.Connection = Depends(get_conn)
):
    """Apply the chosen lines as one transaction (fills, swaps and equips).
    422 use_pulls_endpoint if any is a pull; 409 stale_plan (with `stale`) if
    a line is no longer in the plan. Records one deck version when the list's
    contents changed. `?dry_run=1` previews."""
    require_own_deck(conn, deck_id, me(request))
    if any(i.startswith("pull:") for i in body.line_ids):
        raise PullsNeedConfirm("Pulls take a card out of another deck: use POST .../assembly/pulls")

    def run() -> dict:
        result = service.assemble(conn, deck_id, body.line_ids, note=body.note, commit=False)
        return {**result_json(result), "notice": result.message()}

    return write(request, conn, run)


@router.post("/decks/{deck_id}/assembly/pulls")
@risky
def apply_assembly_with_pulls(
    deck_id: int, body: AssembleIn, request: Request, confirm: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """RISKY: like POST .../assembly but with pulls, which equip a copy here
    and take it out of the deck that holds it. A person just does it; a
    system actor first gets the preview and a token (409 confirm_required)
    and repeats with ?confirm=TOKEN and the same body. `?dry_run=1` returns
    the preview to anyone, changing nothing."""
    require_own_deck(conn, deck_id, me(request))
    if not any(i.startswith("pull:") for i in body.line_ids):
        raise NoPulls("None of these lines is a pull: use POST .../assembly")

    def operation(c: sqlite3.Connection):
        result = service.assemble(c, deck_id, body.line_ids, note=body.note, commit=False)
        return result.message(), result_json(result)

    notice = deps.run_risky(
        request, conn, operation,
        Fields(confirm, line_ids=",".join(body.line_ids), note=body.note or ""),
    )
    return JSONResponse(jsonable_encoder({"notice": notice, **request.state.audit_preview}))
