"""Record a purchase of singles by hand: a form, a preview, then the write.
A purchase only adds cards to your collection; it never changes a deck."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from mtgtracker import purchases
from mtgtracker.money import parse_cents
from mtgtracker.services import ServiceError
from mtgtracker.services import purchases as service
from mtgtracker.web.deps import get_conn, me, redirect, render

router = APIRouter()


def _dollars(value: str, label: str, *, required: bool = False) -> int:
    """A dollar amount typed in the form, as cents."""
    text = (value or "").strip().lstrip("$").replace(",", "")
    if not text:
        if required:
            raise ServiceError(f"{label} is required")
        return 0
    try:
        cents = parse_cents(text)
    except (ArithmeticError, ValueError):
        raise ServiceError(f"{label} {value.strip()!r} isn't an amount in dollars and cents") from None
    if cents < 0:
        raise ServiceError(f"{label} can't be negative")
    return cents


def _page(request: Request, form: dict, plan=None, error: str | None = None, status_code: int = 200):
    return render(
        request, "purchase_new.html", status_code=status_code, form=form, plan=plan, error=error,
        summary=purchases.summarize(plan) if plan else None,
    )


async def _read(request: Request) -> dict:
    data = await request.form()
    return {k: str(data.get(k) or "") for k in ("cart", "vendor", "date", "tax", "shipping", "total")}


@router.get("/purchases/new", response_class=HTMLResponse)
def new_purchase(request: Request):
    return _page(request, {"cart": "", "vendor": "", "date": "", "tax": "", "shipping": "", "total": ""})


def _plan(request: Request, conn: sqlite3.Connection, form: dict):
    return service.preview_purchase(
        conn, me(request), form["cart"], vendor=form["vendor"], date=form["date"].strip() or None,
        tax_cents=_dollars(form["tax"], "Tax"), shipping_cents=_dollars(form["shipping"], "Shipping"),
        total_cents=_dollars(form["total"], "Total", required=True),
    )


@router.post("/purchases/preview", response_class=HTMLResponse)
async def preview_purchase(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """What recording this would do. It writes nothing, so it leaves no audit
    row or restore point either."""
    request.state.no_audit = True
    form = await _read(request)
    try:
        plan = _plan(request, conn, form)
    except ServiceError as exc:
        return _page(request, form, error=str(exc), status_code=422)
    return _page(request, form, plan=plan)


@router.post("/purchases")
async def record_purchase(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await _read(request)
    try:
        _, summary = service.record_purchase(
            conn, me(request), form["cart"], vendor=form["vendor"], date=form["date"].strip() or None,
            tax_cents=_dollars(form["tax"], "Tax"), shipping_cents=_dollars(form["shipping"], "Shipping"),
            total_cents=_dollars(form["total"], "Total", required=True),
        )
    except ServiceError as exc:
        conn.rollback()
        return _page(request, form, error=str(exc), status_code=422)
    return redirect(f"/transactions/{summary.transaction_id}", notice=summary.message())
