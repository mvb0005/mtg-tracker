"""Collection imports over JSON: add one, list a person's, undo one."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from mtgtracker.confirm import risky
from mtgtracker.services import collection as collection_service
from mtgtracker.services import people as people_service
from mtgtracker.web import deps
from mtgtracker.web.api.v1.common import ApiError, Fields, write
from mtgtracker.web.deps import get_conn, load_person, may_act_for, me

router = APIRouter()


def _import_json(row: sqlite3.Row) -> dict:
    return {
        "import_id": row["id"], "owner_id": row["owner_id"], "filename": row["original_filename"],
        "format": row["format"], "card_count": row["card_count"], "imported_at": row["imported_at"],
    }


@router.get("/people/{person_id}/imports")
def list_imports(person_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """The collection imports of a collection you may act on (as the web page shows them)."""
    person = load_person(conn, person_id)
    if not may_act_for(request, person):
        raise HTTPException(status_code=403, detail="You can't see that collection's imports")
    rows = conn.execute(
        "SELECT * FROM collection_imports WHERE owner_id = ? ORDER BY id DESC", (person.id,)
    ).fetchall()
    return {"owner_id": person.id, "imports": [_import_json(r) for r in rows]}


async def _upload(request: Request) -> tuple[bytes, str]:
    """(file bytes, filename) from a multipart `file` or a JSON {csv, filename}."""
    limit = people_service.MAX_UPLOAD_BYTES
    if int(request.headers.get("content-length") or 0) > limit + 64 * 1024:
        raise ApiError(413, "too_large", people_service.TOO_BIG)
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            payload = await request.json()
        except ValueError:
            raise ApiError(422, "validation_error", "the body isn't valid JSON") from None
        payload = payload if isinstance(payload, dict) else {}
        csv_text, filename = payload.get("csv"), payload.get("filename")
        if not isinstance(csv_text, str) or not csv_text.strip():
            raise ApiError(422, "validation_error", 'send {"csv": text, "filename": name}')
        return csv_text.encode(), str(filename or "import.csv")
    form = await request.form()
    upload = form.get("file")
    if upload is None or isinstance(upload, str) or not upload.filename:
        raise ApiError(422, "validation_error", "send a multipart `file`, or JSON {csv, filename}")
    return await upload.read(limit + 1), upload.filename


@router.post("/people/{person_id}/imports", status_code=201)
async def add_import(person_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Import a collection CSV into a collection: your own, or another
    person's when may_act_for allows (always for a system actor, which could
    act for them anyway). Same limits and semantics as the web upload."""
    person = load_person(conn, person_id)
    if not may_act_for(request, person):
        raise HTTPException(status_code=403, detail="You can't import into that collection")
    data, filename = await _upload(request)
    principal, actor = me(request), deps.actor(request)

    def run() -> dict:
        s = people_service.import_collection_summary(conn, person, actor, data, filename, commit=False)
        body = {
            "import_id": s.import_id, "owner_id": person.id, "format": s.format, "copies": s.copies,
            "distinct": s.distinct, "priced": s.priced, "unresolved": s.unresolved,
            "unpinned": s.unpinned, "notice": s.message(),
        }
        if person.id == principal.id:  # the file's own prices, echoed only to its owner
            body["cost_cents"] = s.cost_cents
        return body

    return write(request, conn, run, status=201)


@router.delete("/imports/{import_id}")
@risky
def delete_import(
    import_id: int, request: Request, confirm: str | None = None, conn: sqlite3.Connection = Depends(get_conn)
):
    """Undo a collection import (RISKY): deletes its lots and its record.
    409 import_in_use if any lot is on a deck list, equipped, disposed, in a
    version snapshot or in a change. A system actor first gets the preview and
    a token (409 confirm_required); repeat with ?confirm=TOKEN. ?dry_run=1
    returns the preview for anyone, changing nothing."""
    imp = collection_service.get_import(conn, import_id)  # 404
    if imp["owner_id"] != me(request).id:
        raise HTTPException(status_code=403, detail="That isn't your import")
    notice = deps.run_risky(
        request, conn, lambda c: collection_service.delete_import(c, import_id, commit=False), Fields(confirm)
    )
    return JSONResponse({"notice": notice, "preview": request.state.audit_preview})
