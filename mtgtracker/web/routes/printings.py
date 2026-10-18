"""/printings: pin unresolved lots to the printing in your hand."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from mtgtracker import printings
from mtgtracker.services import NotFoundError, ServiceError
from mtgtracker.services import printings as printing_service
from mtgtracker.web.deps import get_conn, me, redirect, render, require_own_lot

router = APIRouter()


@router.get("/printings", response_class=HTMLResponse)
def printings_queue(
    request: Request, notice: str = "", conn: sqlite3.Connection = Depends(get_conn)
):
    return render(
        request, "printings.html",
        groups=printings.unresolved_groups(conn, me(request).id),
        cached_sets=printings.cached_set_codes(),
        notice=notice,
    )


def resolve_page(
    request: Request, conn: sqlite3.Connection, lot_id: int,
    *, notice: str = "", error: str = "", status_code: int = 200,
) -> HTMLResponse:
    require_own_lot(conn, lot_id, me(request))
    group = printings.group_for_lot(conn, lot_id)
    if group is None:
        raise HTTPException(status_code=404, detail="No unresolved card with that id")
    return render(
        request, "printing_resolve.html", status_code=status_code,
        group=group,
        candidates=printings.find_printings(group.card_name),
        cached_sets=printings.cached_set_codes(),
        notice=notice,
        error=error,
    )


@router.get("/printings/{lot_id}", response_class=HTMLResponse)
def printing_resolve(
    lot_id: int, request: Request, notice: str = "", error: str = "",
    conn: sqlite3.Connection = Depends(get_conn),
):
    return resolve_page(request, conn, lot_id, notice=notice, error=error)


@router.post("/printings/{lot_id}")
async def printing_assign(
    lot_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
):
    require_own_lot(conn, lot_id, me(request))
    form = await request.form()
    choice = form.get("choice")
    quantities = [(key, str(value)) for key, value in form.multi_items()]
    try:
        pinned = printing_service.assign(conn, lot_id, str(choice) if choice else None, quantities)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ServiceError as exc:
        return resolve_page(request, conn, lot_id, error=str(exc), status_code=400)
    if pinned.next_lot_id is not None:
        return redirect(f"/printings/{pinned.next_lot_id}", notice=pinned.notice)
    return redirect("/printings", notice=pinned.notice)


@router.post("/printings/{lot_id}/fetch-set")
async def printing_fetch_set(lot_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    require_own_lot(conn, lot_id, me(request))
    form = await request.form()
    try:
        notice = printing_service.fetch_set(str(form.get("set_code", "")))
    except ServiceError as exc:
        return redirect(f"/printings/{lot_id}", error=str(exc))
    return redirect(f"/printings/{lot_id}", notice=notice)
