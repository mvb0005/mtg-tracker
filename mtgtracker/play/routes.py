"""/play: pick a deck and AI opponents, start a game on a game server (tables.py), rejoin or end it, and
the games so far. /play/results and /play/replays/<match>/<seat> are where a game server posts each
match's result and each seat's replay (its shared secret, no sign-in)."""

from __future__ import annotations

import hmac
import os
import sqlite3
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from mtgtracker import db
from mtgtracker.play import games, tables
from mtgtracker.play.forge import Busy
from mtgtracker.services import ServiceError, UpstreamError
from mtgtracker.web import queries
from mtgtracker.web.auth import LOOPBACK
from mtgtracker.web.deps import form_int, get_conn, me, redirect, render, require_own_deck

router = APIRouter()
MAX_OPPONENTS = 3
RESULTS_PATH = "/play/results"
REPLAYS_PATH = "/play/replays"


def _settle(conn: sqlite3.Connection, where: tables.OneServer | tables.Pods) -> None:
    """Games the tracker thinks are running but no game server is: take the result it kept, or call them
    abandoned (a server that stopped lost its game). A server that doesn't answer right now is left alone."""
    for g in games.playing(conn):
        try:
            table = where.status(g["match_id"])
        except UpstreamError:
            continue
        if table and table.get("state") == "playing":
            continue
        result = (table or {}).get("result") or {}
        if result.get("matchId") == g["match_id"]:
            games.record_result(conn, result, commit=False)
        else:
            games.record_result(
                conn,
                {
                    "matchId": g["match_id"],
                    "status": "abandoned",
                    "summary": "The game server stopped without a result",
                },
                commit=False,
            )


def _who(table: dict | None) -> str:
    humans = [s["name"] for s in (table or {}).get("seats", []) if s.get("kind") == "human"]
    return ", ".join(humans) or "Someone"


@router.get("/play", response_class=HTMLResponse)
def play_page(
    request: Request, notice: str = "", error: str = "", conn: sqlite3.Connection = Depends(get_conn)
):
    person = me(request)
    where = tables.from_env()
    opponents: list[dict] = []
    running: list[dict] = []
    problem = None
    if where is None:
        problem = "No game server is set up for this tracker."
    else:
        try:
            running = where.running()
            _settle(conn, where)
            conn.commit()
            opponents = where.lobby().opponents()
        except ServiceError as exc:
            problem = str(exc)
    ids = {t.get("matchId") for t in running}
    mine = [g for g in games.playing(conn, person.id) if g["match_id"] in ids]
    my_ids = {g["match_id"] for g in mine}
    others = [dict(t, who=_who(t)) for t in running if t.get("matchId") not in my_ids]
    capacity = where.capacity if where else 0
    return render(
        request,
        "play.html",
        active="play",
        decks=queries.list_decks(conn, person.id),
        opponents=opponents,
        problem=problem,
        mine=mine,
        others=others,
        full=len(running) >= capacity,
        capacity=capacity,
        can_end_any=person.is_admin,
        table_link=where.table_link if where else None,
        recent=[
            dict(g, replays=games.replay_seats(request.app.state.db_path, g["match_id"]))
            for g in games.recent(conn, person.id)
        ],
        notice=notice,
        error=error,
    )


MAX_SEATS = 4


@dataclass(frozen=True)
class Seat:
    """A human seat: who sits there (a tracker person id, or another id the game server only echoes back),
    their name at the table, the deck they play (a tracker deck with a commander) and, from Discord, their
    picture."""

    name: str
    person: str
    deck_id: int
    avatar: str | None = None


def start_table(
    conn: sqlite3.Connection, host, seats: list[Seat], picked: list[str], *, return_url: str
) -> tuple[dict, dict[str, str]]:
    """Starts a Commander game: the human seats, then the curated AI opponents in `picked`, 2 to MAX_SEATS
    seats in all, recorded in play_games under `host` (a tracker person) with the first seat's deck. Deck
    access is the caller's to check. Returns the game server's answer and each human seat's ticket by its
    `person`. Raises ServiceError with a message for the people at the table (the play page and the Discord
    Activity both start games)."""
    if not seats:
        raise ServiceError("Nobody is sitting down")
    if not 2 <= len(seats) + len(picked) <= MAX_SEATS:
        raise ServiceError(f"A game needs 2 to {MAX_SEATS} players, AIs included")
    decks = []
    for seat in seats:
        deck = conn.execute("SELECT * FROM decks WHERE id = ?", (seat.deck_id,)).fetchone()
        if deck is None:
            raise ServiceError(f"{seat.name} hasn't picked a deck")
        if not deck["commander"]:
            raise ServiceError(f"{deck['name']} has no commander. Pick one on its deck page.")
        detail = queries.get_deck(conn, seat.deck_id, viewer_id=deck["owner_id"])
        decks.append((deck, queries.deck_exports(detail)["decklist"]))
    where = tables.from_env()
    if where is None:
        raise ServiceError("No game server is set up for this tracker")
    try:
        roster = {o["id"]: o for o in where.lobby().opponents()}
        unknown = [o for o in picked if o not in roster]
        if unknown:
            raise ServiceError(f"Unknown opponent {unknown[0]}")
        _settle(conn, where)
        spec = {
            "format": "commander",
            "seats": [
                {"kind": "human", "name": seat.name, "person": seat.person,
                 "deck": {"name": deck["name"], "list": decklist},
                 **({"avatar": seat.avatar} if seat.avatar else {})}
                for seat, (deck, decklist) in zip(seats, decks, strict=True)
            ]
            + [{"kind": "ai", "opponent": o} for o in picked],
            "returnUrl": return_url,
        }
        # Where the game server reaches this tracker in-cluster (e.g. http://mtgtracker.mtgtracker.svc.cluster.local).
        results_base = os.environ.get("PLAY_RESULTS_URL", "").strip()
        if results_base:
            spec["resultsUrl"] = results_base.rstrip("/") + RESULTS_PATH
            spec["replayUrl"] = results_base.rstrip("/") + REPLAYS_PATH
        started = where.start(spec)
    except Busy as exc:
        conn.commit()
        if where.capacity > 1:
            raise ServiceError(
                f"All {where.capacity} tables are in use. Try again when a game ends."
            ) from exc
        try:
            who = ", ".join(_who(t) for t in where.running()) or "Someone"
        except ServiceError:
            who = "Someone"
        raise ServiceError(
            f"{who} is playing on the game server right now. It hosts one game at a time."
        ) from exc
    except ServiceError:
        conn.commit()
        raise
    tickets = {s["person"]: s["ticket"] for s in started["seats"] if s.get("ticket") and s.get("person")}
    others = [s for s in started["seats"] if s["kind"] == "human"][1:]
    games.record_start(
        conn,
        match_id=started["matchId"],
        host_id=host.id,
        format="commander",
        deck_id=seats[0].deck_id,
        deck_name=decks[0][0]["name"],
        opponents=[{"name": s["name"], "deck": d[0]["name"]} for s, d in zip(others, decks[1:], strict=True)]
        + [
            {"name": s["name"], "deck": roster[o]["deck"]}
            for s, o in zip([s for s in started["seats"] if s["kind"] == "ai"], picked, strict=True)
        ],
        ticket=tickets.get(str(host.id)),
    )
    started["deckName"] = decks[0][0]["name"]
    return started, tickets


def start_game(
    conn: sqlite3.Connection, person, deck_id: int | None, picked: list[str], *, return_url: str
) -> tuple[dict, str]:
    """A goldfish game from the play page: the person's own deck against 1 to MAX_OPPONENTS curated
    opponents. Returns the game server's answer and the person's seat ticket."""
    if deck_id is None:
        raise ServiceError("Pick one of your decks")
    if not 1 <= len(picked) <= MAX_OPPONENTS:
        raise ServiceError(f"Pick one to {MAX_OPPONENTS} opponents")
    require_own_deck(conn, deck_id, person)
    seat = Seat(person.display_name, str(person.id), deck_id)
    started, tickets = start_table(conn, person, [seat], picked, return_url=return_url)
    return started, tickets.get(str(person.id))


@router.post("/play/start")
async def play_start(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    person = me(request)
    form = await request.form()
    picked = [str(o) for o in form.getlist("opponent") if str(o).strip()]
    try:
        started, ticket = start_game(conn, person, form_int(form.get("deck_id")), picked, return_url="/play")
    except ServiceError as exc:
        return redirect("/play", error=str(exc))
    names = [s["name"] for s in started["seats"] if s["kind"] == "ai"]
    where = tables.from_env()
    request.state.audit_notice = f"Started a game: {started['deckName']} against {', '.join(names)}"
    return RedirectResponse(where.table_link(started["matchId"], ticket), status_code=303)


@router.post("/play/end")
async def play_end(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """End a running game: your own, or anyone's for an admin."""
    person = me(request)
    form = await request.form()
    where = tables.from_env()
    if where is None:
        return redirect("/play", error="No game server is set up for this tracker")
    match_id = str(form.get("match_id", ""))
    game = conn.execute("SELECT host_id, status FROM play_games WHERE match_id = ?", (match_id,)).fetchone()
    if game is not None and game["host_id"] != person.id and not person.is_admin:
        raise HTTPException(status_code=403, detail="That isn't your game")
    if game is None and not person.is_admin:
        raise HTTPException(status_code=403, detail="That isn't your game")
    try:
        server = where.server_for(match_id)
        ended = server is not None and server.end(match_id)
        if not ended:
            _settle(conn, where)
            conn.commit()
    except ServiceError as exc:
        return redirect("/play", error=str(exc))
    request.state.audit_notice = "Ended a game"
    return redirect("/play", notice="The game is ending." if ended else "That game had already ended.")


def _not_the_game_server(request: Request) -> JSONResponse | None:
    """None when the caller has the shared control secret (or, with no secret configured, is loopback: local
    development), else the 401 to send. These routes are outside the sign-in middleware: the game server
    is not a person."""
    secret = tables.control_secret()
    sent = request.headers.get("authorization", "").encode()
    if secret:
        if not hmac.compare_digest(sent, f"Bearer {secret}".encode()):
            return JSONResponse(
                {"error": {"code": "unauthorized", "message": "Wrong secret"}}, status_code=401
            )
    elif not (request.client and request.client.host in LOOPBACK):
        return JSONResponse(
            {"error": {"code": "unauthorized", "message": "No secret configured"}}, status_code=401
        )
    return None


@router.post(RESULTS_PATH)
async def play_results(request: Request):
    """The game server's result for a match (forge-gui-web PROTOCOL.md "Results")."""
    refused = _not_the_game_server(request)
    if refused:
        return refused
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": {"code": "bad_request", "message": "Not JSON"}}, status_code=400)
    if not isinstance(body, dict) or not body.get("matchId"):
        return JSONResponse({"error": {"code": "bad_request", "message": "No matchId"}}, status_code=400)
    conn = db.connect(request.app.state.db_path, check_same_thread=False)
    try:
        games.record_result(conn, body)
    finally:
        conn.close()
    return Response(status_code=204)


@router.post(REPLAYS_PATH + "/{match_id}/{seat}")
async def play_replay_upload(match_id: str, seat: int, request: Request):
    """One seat's replay of a match (forge-gui-web PROTOCOL.md "Replays"), gzipped JSON lines, kept as a
    file beside the database. A second upload of the same seat replaces the first."""
    refused = _not_the_game_server(request)
    if refused:
        return refused
    try:
        games.save_replay(request.app.state.db_path, match_id, seat, await request.body())
    except ValueError as exc:
        return JSONResponse({"error": {"code": "bad_request", "message": str(exc)}}, status_code=400)
    return Response(status_code=204)


@router.get("/play/games/{match_id}/replay/{seat}")
def play_replay_download(
    match_id: str, seat: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
):
    """A replay file, for the game's host or an admin."""
    person = me(request)
    game = conn.execute("SELECT host_id FROM play_games WHERE match_id = ?", (match_id,)).fetchone()
    if game is None or (game["host_id"] != person.id and not person.is_admin):
        raise HTTPException(status_code=404, detail="No such game")
    path = games.replay_path(request.app.state.db_path, match_id, seat)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="No replay for that game")
    return FileResponse(
        path, media_type="application/gzip", filename=f"forge-replay-{match_id}-seat{seat}.jsonl.gz"
    )
