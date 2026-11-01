"""/discord/: the Discord Activity's page and API.

Inside Discord the Activity's root is this tracker's /discord/ (the application's root URL mapping), so the
page and everything it loads live under /discord/. The sign-in middleware skips /discord and /discord/...
(and Cloudflare Access lets them through): every route here authenticates by Discord, the API by the session
token from POST /discord/api/session. Players are their Discord ids; they pick from the owner's decks.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from mtgtracker import db
from mtgtracker.discord import auth, rooms
from mtgtracker.play import routes as play
from mtgtracker.play import tables
from mtgtracker.services import ServiceError
from mtgtracker.web import queries
from mtgtracker.web.deps import render

router = APIRouter()
STATIC = Path(__file__).parent / "static"
ROSTER_SECONDS = 60.0
_roster: tuple[float, list[dict]] = (0.0, [])


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


@router.get("/discord", include_in_schema=False)
def activity_root() -> RedirectResponse:
    return RedirectResponse("/discord/", status_code=308)


@router.get("/discord/", response_class=HTMLResponse)
def activity(request: Request):
    """The Activity's page. Static: everything it shows comes from the API once Discord has signed you in."""
    discord = auth.Discord.from_env()
    page = render(
        request, "activity.html", client_id=discord.client_id if discord else "",
        version={name: _version(name) for name in ("activity.js", "activity.css")},
    )
    page.headers["Cache-Control"] = "no-cache"  # so a new deploy's files reach an Activity opened before
    return page


def _version(name: str) -> str:
    """Short content hash of an Activity file, appended as ?v= (as the tracker's static_version does)."""
    return hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:12]


@router.post("/discord/api/session")
async def session(request: Request):
    """Signs a Discord user in: {code} from the SDK's authorize -> {session, accessToken, id, name}."""
    discord = auth.Discord.from_env()
    if discord is None:
        return _error(503, "not_configured", "Discord isn't set up on this tracker")
    if not auth.EXCHANGES.allow():
        return _error(429, "busy", "Too many sign-ins right now. Try again in a minute.")
    try:
        body = await request.json()
    except ValueError:
        body = {}
    code = body.get("code") if isinstance(body, dict) else None
    try:
        token = discord.exchange(str(code or ""))
        who = discord.user(token)
    except auth.NotMember as exc:
        return _error(403, "not_member", str(exc))
    except ServiceError as exc:
        return _error(401, "sign_in", str(exc))
    session = discord.session(who["id"], who["name"], avatar=who["avatar"])
    return {"session": session, "accessToken": token, **who}


class _Refused(Exception):
    def __init__(self, response: JSONResponse):
        self.response = response


def install(app) -> None:
    app.include_router(router)

    @app.exception_handler(_Refused)
    def refused(request: Request, exc: _Refused):
        return exc.response


def signed_in(request: Request) -> Iterator[sqlite3.Connection]:
    """A connection for an API call from a signed-in Discord user, who is request.state.session ({d: id,
    n: name})."""
    discord = auth.Discord.from_env()
    header = request.headers.get("authorization", "")
    found = discord.read_session(header.removeprefix("Bearer ").strip()) if discord else None
    if found is None:
        raise _Refused(_error(401, "signed_out", "Sign in again"))
    request.state.session = found
    conn = db.connect(request.app.state.db_path, check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


def _opponents() -> list[dict]:
    """The curated AI opponents, asked of a game server at most once a ROSTER_SECONDS (not on every poll)."""
    global _roster
    fetched, roster = _roster
    if time.time() - fetched > ROSTER_SECONDS or not roster:
        where = tables.from_env()
        if where is None:
            raise ServiceError("No game server is set up for this tracker")
        roster = [
            {k: o.get(k) for k in ("id", "commander", "archetype", "plan", "deck", "colors")}
            for o in where.lobby().opponents()
        ]
        _roster = (time.time(), roster)
    return roster


def _decks(conn: sqlite3.Connection) -> list[dict]:
    owner = auth.deck_owner(conn)
    if owner is None:
        return []
    return [
        {"id": d["id"], "name": d["name"], "commander": d["commander"]}
        for d in queries.list_decks(conn, owner.id) if d["commander"]
    ]


def _view(room: rooms.Room, you: str, decks: list[dict], roster: list[dict], problem: str | None) -> dict:
    by_id = {d["id"]: d for d in decks}
    started = room.started
    return {
        "you": you,
        "members": [
            {"id": m.id, "name": m.name, "avatar": m.avatar, "deck": by_id.get(m.deck_id)}
            for m in sorted(room.members.values(), key=lambda m: m.joined)
        ],
        "opponents": room.opponents,
        "maxSeats": play.MAX_SEATS,
        "decks": decks,
        "roster": roster,
        "starting": room.starting,
        "started": (
            {"matchId": started["matchId"], "ticket": started["tickets"].get(you)} if started else None
        ),
        "error": room.error,
        "problem": problem,
    }


@router.post("/discord/api/room")
async def room(request: Request, conn: sqlite3.Connection = Depends(signed_in)):
    """The Activity instance's lobby, polled by everyone in it: {instanceId} marks you present and returns the
    room; add {deckId} to pick your deck, {opponents: [id]} to set the room's AI opponents, {start: true} to
    start the game with everyone who has picked a deck."""
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict) or not rooms.valid_instance(str(body.get("instanceId", ""))):
        return _error(400, "bad_request", "No Activity instance")
    me = request.state.session
    decks = _decks(conn)
    try:
        roster, problem = _opponents(), None
    except ServiceError as exc:
        roster, problem = [], str(exc)
    with rooms.LOCK:
        room = rooms.touch(body["instanceId"], me["d"], me["n"], me.get("a"))
        if room.started and _over(conn, room.started["matchId"]):
            room.started = None
        if "deckId" in body and any(d["id"] == body["deckId"] for d in decks):
            room.members[me["d"]].deck_id = body["deckId"]
            room.error = None
        if isinstance(body.get("opponents"), list):
            known = {o["id"] for o in roster}
            room.opponents = [str(o) for o in body["opponents"] if str(o) in known][: play.MAX_SEATS - 1]
            room.error = None
        start = bool(body.get("start")) and not room.started and not room.starting
        if start:
            room.starting = True
            seated = sorted(
                (m for m in room.members.values() if m.deck_id is not None), key=lambda m: m.joined
            )
            picked = list(room.opponents)
    if start:
        _start(conn, room, seated, picked)
    with rooms.LOCK:
        return _view(room, me["d"], decks, roster, problem)


def _over(conn: sqlite3.Connection, match_id: str) -> bool:
    row = conn.execute("SELECT status FROM play_games WHERE match_id = ?", (match_id,)).fetchone()
    return row is not None and row["status"] != "playing"


def _start(conn: sqlite3.Connection, room: rooms.Room, seated: list[rooms.Member], picked: list[str]) -> None:
    """Starts the room's game: a seat per person with a deck, then the room's AIs; back from the table is the
    Activity's lobby (its root). Recorded under the deck owner."""
    error, started = None, None
    try:
        owner = auth.deck_owner(conn)
        if owner is None:
            raise ServiceError("This tracker has no owner to play decks from")
        if not seated:
            raise ServiceError("Pick a deck first")
        seats = [play.Seat(m.name, f"discord:{m.id}", m.deck_id, m.avatar) for m in seated]
        answer, tickets = play.start_table(conn, owner, seats, picked, return_url="/")
        started = {
            "matchId": answer["matchId"],
            "tickets": {m.id: tickets.get(f"discord:{m.id}") for m in seated},
        }
    except ServiceError as exc:
        error = str(exc)
    with rooms.LOCK:
        room.starting = False
        room.started = started
        room.error = error
