"""/play: pick a deck and AI opponents, start a game on the game server, rejoin or end it, and the games so
far. /play/results is where the game server posts each match's result (its shared secret, no sign-in)."""

from __future__ import annotations

import hmac
import os
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from mtgtracker import db
from mtgtracker.play import games
from mtgtracker.play.forge import Busy, GameServer
from mtgtracker.services import ServiceError
from mtgtracker.web import queries
from mtgtracker.web.auth import LOOPBACK
from mtgtracker.web.deps import form_int, get_conn, me, redirect, render, require_own_deck

router = APIRouter()
MAX_OPPONENTS = 3
RESULTS_PATH = "/play/results"


def _settle(conn: sqlite3.Connection, table: dict) -> None:
    """Games the tracker thinks are running but the game server no longer is: take the result it kept, or
    call them abandoned (a restart loses a running game)."""
    for g in games.playing(conn):
        if table.get("state") == "playing" and table.get("matchId") == g["match_id"]:
            continue
        result = table.get("result") or {}
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
    server = GameServer.from_env()
    opponents: list[dict] = []
    table = None
    problem = None
    if server is None:
        problem = "No game server is set up for this tracker."
    else:
        try:
            opponents = server.opponents()
            table = server.status()
        except ServiceError as exc:
            problem = str(exc)
    running = table if table and table.get("state") == "playing" else None
    mine = [g for g in games.playing(conn, person.id) if running and g["match_id"] == running.get("matchId")]
    decks = queries.list_decks(conn, person.id)
    return render(
        request,
        "play.html",
        active="play",
        decks=decks,
        opponents=opponents,
        problem=problem,
        running=running,
        running_who=_who(running),
        mine=mine[0] if mine else None,
        can_end_any=person.is_admin,
        table_link=server.table_link if server else None,
        recent=games.recent(conn, person.id),
        notice=notice,
        error=error,
    )


@router.post("/play/start")
async def play_start(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    person = me(request)
    form = await request.form()
    deck_id = form_int(form.get("deck_id"))
    picked = [str(o) for o in form.getlist("opponent") if str(o).strip()]
    if deck_id is None:
        return redirect("/play", error="Pick one of your decks")
    if not 1 <= len(picked) <= MAX_OPPONENTS:
        return redirect("/play", error=f"Pick one to {MAX_OPPONENTS} opponents")
    deck = require_own_deck(conn, deck_id, person)
    if not deck["commander"]:
        return redirect("/play", error=f"{deck['name']} has no commander. Pick one on its deck page.")
    server = GameServer.from_env()
    if server is None:
        return redirect("/play", error="No game server is set up for this tracker")
    detail = queries.get_deck(conn, deck_id, viewer_id=person.id)
    decklist = queries.deck_exports(detail)["decklist"]
    try:
        roster = {o["id"]: o for o in server.opponents()}
        unknown = [o for o in picked if o not in roster]
        if unknown:
            return redirect("/play", error=f"Unknown opponent {unknown[0]}")
        _settle(conn, server.status())
        spec = {
            "format": "commander",
            "seats": [
                {
                    "kind": "human",
                    "name": person.display_name,
                    "person": str(person.id),
                    "deck": {"name": deck["name"], "list": decklist},
                }
            ]
            + [{"kind": "ai", "opponent": o} for o in picked],
            "returnUrl": "/play",
        }
        # Where the game server reaches this tracker in-cluster (e.g. http://mtgtracker.mtgtracker.svc.cluster.local).
        results_base = os.environ.get("PLAY_RESULTS_URL", "").strip()
        if results_base:
            spec["resultsUrl"] = results_base.rstrip("/") + RESULTS_PATH
        started = server.start(spec)
    except Busy:
        conn.commit()
        try:
            who = _who(server.status())
        except ServiceError:
            who = "Someone"
        return redirect(
            "/play", error=f"{who} is playing on the game server right now. It hosts one game at a time."
        )
    except ServiceError as exc:
        conn.commit()
        return redirect("/play", error=str(exc))
    ticket = next(
        (s["ticket"] for s in started["seats"] if s.get("person") == str(person.id) and s.get("ticket")), None
    )
    names = [s["name"] for s in started["seats"] if s["kind"] == "ai"]
    games.record_start(
        conn,
        match_id=started["matchId"],
        host_id=person.id,
        format="commander",
        deck_id=deck_id,
        deck_name=deck["name"],
        opponents=[
            {"name": s["name"], "deck": roster[o]["deck"]}
            for s, o in zip([s for s in started["seats"] if s["kind"] == "ai"], picked, strict=True)
        ],
        ticket=ticket,
    )
    request.state.audit_notice = f"Started a game: {deck['name']} against {', '.join(names)}"
    return RedirectResponse(server.table_link(ticket), status_code=303)


@router.post("/play/end")
async def play_end(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """End a running game: your own, or anyone's for an admin (the table is shared, one game at a time)."""
    person = me(request)
    form = await request.form()
    server = GameServer.from_env()
    if server is None:
        return redirect("/play", error="No game server is set up for this tracker")
    match_id = str(form.get("match_id", ""))
    game = conn.execute("SELECT host_id, status FROM play_games WHERE match_id = ?", (match_id,)).fetchone()
    if game is not None and game["host_id"] != person.id and not person.is_admin:
        raise HTTPException(status_code=403, detail="That isn't your game")
    if game is None and not person.is_admin:
        raise HTTPException(status_code=403, detail="That isn't your game")
    try:
        ended = server.end(match_id)
        if not ended:
            _settle(conn, server.status())
            conn.commit()
    except ServiceError as exc:
        return redirect("/play", error=str(exc))
    request.state.audit_notice = "Ended a game"
    return redirect("/play", notice="The game is ending." if ended else "That game had already ended.")


@router.post(RESULTS_PATH)
async def play_results(request: Request):
    """The game server's result for a match (forge-gui-web PROTOCOL.md "Results"), with the shared control
    secret. Outside the sign-in middleware: the game server is not a person. Without a secret configured,
    only a loopback caller (local development) is believed."""
    server = GameServer.from_env()
    secret = server.secret if server else None
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
