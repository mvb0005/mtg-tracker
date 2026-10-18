"""The principal's profile: read it, rename it. No email changes over the API."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from mtgtracker import users
from mtgtracker.services import people as people_service
from mtgtracker.web.api.v1.common import write
from mtgtracker.web.deps import actor, get_conn, me

router = APIRouter()


class ProfileIn(BaseModel):
    display_name: str


def _profile(request: Request, conn: sqlite3.Connection) -> dict:
    person = users.get(conn, me(request).id)
    body = {
        "id": person.id, "display_name": person.display_name, "is_admin": person.is_admin,
        "member_since": person.created_at, "signed_in": person.signed_in,
    }
    if actor(request).kind == "person":  # the email is the sign-in key: people only
        body["email"] = person.email
    return body


@router.get("/profile")
def get_profile(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return {"profile": _profile(request, conn)}


@router.put("/profile")
def put_profile(body: ProfileIn, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Change the principal's display name (a system actor may; not risky,
    just audited). ?dry_run=1 validates and changes nothing."""
    def run() -> dict:
        _, summary = people_service.rename_person(conn, me(request), body.display_name, commit=False)
        return {"profile": _profile(request, conn), "notice": summary}

    return write(request, conn, run)
