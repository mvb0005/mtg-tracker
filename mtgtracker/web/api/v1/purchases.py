"""Purchases over JSON: record singles bought somewhere, list them, undo one.
A purchase only adds cards to the principal's collection; it never touches a
deck (`wanted_by` merely says which decks want the new cards)."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from mtgtracker import purchases
from mtgtracker.confirm import risky
from mtgtracker.services import purchases as service
from mtgtracker.web import deps
from mtgtracker.web.api.v1.common import Fields, write
from mtgtracker.web.deps import get_conn, me

router = APIRouter()


class PurchaseIn(BaseModel):
    vendor: str
    date: str | None = Field(None, description="YYYY-MM-DD; defaults to today")
    tax_cents: int
    shipping_cents: int
    total_cents: int
    cart: str = Field(description="the cart text: a store CSV, a checkout paste, or a plain list")


def _plan_json(plan: purchases.PurchasePlan, summary: purchases.PurchaseSummary) -> dict:
    return {
        "transaction_id": summary.transaction_id, "vendor": plan.vendor, "date": plan.date,
        "format": plan.format, "lines": summary.lines, "copies": summary.copies,
        "subtotal_cents": plan.subtotal_cents, "tax_cents": plan.tax_cents,
        "shipping_cents": plan.shipping_cents, "total_cents": summary.total_cents,
        "unpinned": summary.unpinned, "wanted_by": summary.wanted_by, "line_detail": summary.detail,
    }


def _owned(conn: sqlite3.Connection, request: Request, purchase_id: int) -> sqlite3.Row:
    """The principal's purchase; someone else's is simply not found."""
    try:
        row = service.get_purchase(conn, purchase_id)
    except service.NotFoundError:
        raise HTTPException(status_code=404, detail="No such purchase") from None
    if row["owner_id"] != me(request).id:
        raise HTTPException(status_code=404, detail="No such purchase")
    return row


@router.post("/purchases", status_code=201)
def add_purchase(body: PurchaseIn, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Record a purchase for the principal. Every card is resolved and the
    money reconciled before anything is written (422 `rejected` with the
    exact difference, 422 `cards_not_found` with `names`). `?dry_run=1`
    returns the same preview and writes nothing."""
    owner = me(request)

    def run() -> dict:
        plan, summary = service.record_purchase(
            conn, owner, body.cart, vendor=body.vendor, date=body.date, tax_cents=body.tax_cents,
            shipping_cents=body.shipping_cents, total_cents=body.total_cents, commit=False,
        )
        return {**_plan_json(plan, summary), "notice": summary.message(dry_run=deps.is_dry_run(request))}

    return write(request, conn, run, status=201)


@router.get("/purchases")
def list_purchases(request: Request, source: str | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    """The principal's purchases, newest first (`?source=manual` for the ones recorded by hand)."""
    rows = conn.execute(
        """
        SELECT transactions.*,
               (SELECT COUNT(*) FROM lots WHERE lots.transaction_id = transactions.id) AS copies
        FROM transactions WHERE owner_id = ? AND (? IS NULL OR source = ?)
        ORDER BY created_at DESC, id DESC
        """,
        (me(request).id, source, source),
    ).fetchall()
    return {"purchases": [
        {"id": r["id"], "vendor": r["vendor"], "date": r["created_at"][:10], "source": r["source"],
         "copies": r["copies"], "subtotal_cents": r["subtotal_cents"], "tax_cents": r["tax_cents"],
         "shipping_cents": r["shipping_cents"], "total_cents": r["total_cents"]} for r in rows
    ]}


@router.get("/purchases/{purchase_id}")
def get_purchase(purchase_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """One purchase: its lines, and each line's lots with their cost."""
    txn = _owned(conn, request, purchase_id)
    lines = []
    for p in conn.execute("SELECT * FROM products WHERE transaction_id = ? ORDER BY id", (purchase_id,)):
        lots = conn.execute(
            """
            SELECT lots.id, lots.card_name, lots.scryfall_id, lots.set_code, lots.collector_number,
                   lots.finish, lots.allocated_cost_cents, lots.notes,
                   EXISTS (SELECT 1 FROM deck_cards WHERE deck_cards.lot_id = lots.id) AS on_a_deck_list
            FROM lots WHERE product_id = ? ORDER BY lots.id
            """,
            (p["id"],),
        ).fetchall()
        lines.append({
            "product_id": p["id"], "text": p["title"], "card": p["display_name"], "qty": p["qty"],
            "unit_cents": p["unit_price_cents"], "subtotal_cents": p["line_subtotal_cents"],
            "tax_shipping_cents": p["tax_allocated_cents"], "cost_basis_cents": p["cost_basis_cents"],
            "lots": [
                {"lot_id": lot["id"], "card": lot["card_name"], "scryfall_id": lot["scryfall_id"],
                 "set_code": lot["set_code"], "collector_number": lot["collector_number"],
                 "finish": lot["finish"], "cost_cents": lot["allocated_cost_cents"],
                 "needs_printing": lot["scryfall_id"] is None, "notes": lot["notes"],
                 "on_a_deck_list": bool(lot["on_a_deck_list"])}
                for lot in lots
            ],
        })
    return {
        "id": txn["id"], "vendor": txn["vendor"], "date": txn["created_at"][:10], "source": txn["source"],
        "subtotal_cents": txn["subtotal_cents"], "tax_cents": txn["tax_cents"],
        "shipping_cents": txn["shipping_cents"], "total_cents": txn["total_cents"], "lines": lines,
    }


@router.delete("/purchases/{purchase_id}")
@risky
def delete_purchase(
    purchase_id: int, request: Request, confirm: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Undo a purchase recorded by hand (RISKY): deletes its lots, lines and
    record. 409 purchase_in_use (with `blockers`) if any lot is on a deck
    list, disposed, in a version snapshot or in a change. A system actor
    first gets the preview and a token (409 confirm_required); repeat with
    ?confirm=TOKEN. ?dry_run=1 returns the preview for anyone, changing nothing."""
    _owned(conn, request, purchase_id)  # 404 unless it is the principal's
    notice = deps.run_risky(
        request, conn, lambda c: service.delete_purchase(c, purchase_id, commit=False), Fields(confirm)
    )
    return JSONResponse({"notice": notice, "preview": request.state.audit_preview})
