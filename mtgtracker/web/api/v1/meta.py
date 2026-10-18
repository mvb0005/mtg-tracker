"""Who am I, recent activity, restore points."""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from mtgtracker import audit, restore
from mtgtracker.web.api.v1.common import user_json
from mtgtracker.web.deps import actor, get_conn, me

router = APIRouter()


@router.get("/me")
def whoami(request: Request):
    """principal: whose things this request touches; actor: who is asking
    (the same for a person; a system user acts for a person)."""
    return {"principal": user_json(me(request)), "actor": user_json(actor(request))}


@router.get("/audit")
def recent_activity(
    request: Request, limit: int = Query(50, ge=1, le=200), conn: sqlite3.Connection = Depends(get_conn)
):
    """The principal's own recent writes, newest first."""
    rows = audit.recent(conn, me(request).id, limit)
    return {"activity": [
        {
            "id": r["id"], "at": r["at"], "actor_id": r["actor_id"], "actor": r["actor_name"],
            "actor_kind": r["actor_kind"], "method": r["method"], "path": r["path"], "action": r["action"],
            "target": json.loads(r["target"]) if r["target"] else None, "summary": r["summary"],
            "restore_point": r["restore_point"],
        }
        for r in rows
    ]}


@router.get("/restore-points")
def restore_points(request: Request):
    """Metadata of the database's restore points, newest first. Admins only
    (a system actor qualifies when it acts for an admin)."""
    if not me(request).is_admin:
        raise HTTPException(status_code=403, detail="Only an admin can see restore points")
    points = restore.list_points(restore.restore_dir(request.app.state.db_path))
    return {"restore_points": [
        {"name": p["name"], "created_at": p["created_at"].isoformat(), "action": p["action"],
         "changelog_seq": p["changelog_seq"], "user_version": p["user_version"], "size": p["size"]}
        for p in reversed(points)
    ]}
