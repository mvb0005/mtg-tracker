"""/play: pick a deck and AI opponents, start a game on a game server (tables.py), rejoin or end it, and
the games so far. /play/results and /play/replays/<match>/<seat> are where a game server posts each
match's result and each seat's replay (its shared secret, no sign-in)."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from mtgtracker import cardinfo, db
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


def _against(table: dict | None) -> str:
    ais = [s["name"] for s in (table or {}).get("seats", []) if s.get("kind") == "ai"]
    return ", ".join(ais)


DECK_BLURBS: dict[str, tuple[str, str]] = {
    "Reign of Dragons": (
        "Dragon tribal",
        "Ramps into big red dragons while Lathliss spawns a 5/5 Dragon token for every one you cast.",
    ),
    "Keen Engineering": (
        "Artifact tokens",
        "Casts cheap artifacts to flood the air with 1/1 Thopters, then sacrifices them to draw cards.",
    ),
    "Saheeli's Workshop": (
        "Artifact clone",
        "Makes hasty artifact copies of your best creatures and artifacts for explosive value turns.",
    ),
    "Heliod's Eclipse": (
        "Flash group draw",
        "Lets everyone draw extra cards, then flips Heliod to cast big X spells at instant speed for cheap.",
    ),
    "Animar, Soul of Elements": (
        "Creature engine",
        "Casts and bounces ETB creatures to grow Animar and discount big creatures down to their colored pips.",
    ),
    "Atla Palani Eggs": (
        "Egg polymorph",
        "Hatches Egg tokens into sacrifice outlets to cheat massive creatures straight from the library.",
    ),
    "Crimson Wedding": (
        "Blood & vampires",
        "Drains the table to make Blood tokens, then sacrifices two on attack to drop huge vampires in.",
    ),
    "Nine Lives": (
        "Cat aristocrats",
        "Recasts cheap cats from the graveyard every turn to fuel sacrifice drains and token lords.",
    ),
}


def deck_blurb(deck: dict) -> tuple[str, str]:
    """(archetype, one-sentence plan) for a tracker deck."""
    known = DECK_BLURBS.get(deck.get("name") or "")
    if known:
        return known
    notes = (deck.get("notes") or "").strip()
    for line in notes.splitlines():
        s = line.strip()
        if not s or s.upper() in ("PLAN", "RULES", "STATUS"):
            continue
        if s.lower().startswith("plan:"):
            s = s[5:].strip()
        first = s.split(". ")[0].rstrip(".")
        if first:
            return ("Commander", first + ".")
    return ("Commander", "Custom Commander list.")


def with_blurb(deck: dict) -> dict:
    archetype, plan = deck_blurb(deck)
    cmd = deck.get("commander") or ""
    info = cardinfo.lookup(None, cmd, "nonfoil") if cmd else None
    face = info.faces[0] if info and info.faces else {}
    return {
        **deck,
        "archetype": archetype,
        "plan": plan,
        "img": info.image if info else None,
        "manaCost": face.get("mana_cost") or "",
        "typeLine": face.get("type_line") or (info.type_line if info else "") or "",
        "oracleText": face.get("oracle_text") or "",
        "pt": face.get("stats") or "",
    }


_IMPORTED: dict[str, dict] = {}


def _imported_file(conn: sqlite3.Connection) -> Path | None:
    row = conn.execute("PRAGMA database_list").fetchone()
    db_file = row[2] if row and len(row) > 2 else ""
    return Path(db_file).parent / "imported_decks.json" if db_file else None


def imported_decks(conn: sqlite3.Connection) -> list[dict]:
    path = _imported_file(conn)
    if not _IMPORTED and path and path.is_file():
        try:
            for item in json.loads(path.read_text()):
                if isinstance(item, dict) and item.get("id"):
                    _IMPORTED[item["id"]] = item
        except Exception:
            pass
    return list(reversed(_IMPORTED.values()))


def import_deck_url(conn: sqlite3.Connection, url: str) -> dict:
    """Imports a Commander deck from a Moxfield, Archidekt, TappedOut or MTGGoldfish URL via the game server."""
    where = tables.from_env()
    if where is None:
        raise ServiceError("No game server is set up for this tracker")
    imported_decks(conn)
    raw = where.lobby().import_url(url)
    src = raw.get("sourceUrl") or url
    host = (urlsplit(src).hostname or "URL").removeprefix("www.")
    did = "url:" + hashlib.sha256(src.encode()).hexdigest()[:12]
    entry = {
        "id": did,
        "name": raw["name"],
        "commander": raw["commander"],
        "archetype": "Imported",
        "plan": f"Imported from {host}.",
        "colors": raw.get("colors") or "",
        "img": None,
        "manaCost": raw.get("manaCost") or "",
        "typeLine": raw.get("typeLine") or "",
        "oracleText": raw.get("oracleText") or "",
        "pt": raw.get("pt") or "",
        "list": raw["list"],
        "sourceUrl": src,
    }
    _IMPORTED[did] = entry
    path = _imported_file(conn)
    if path:
        try:
            path.write_text(json.dumps(list(_IMPORTED.values())))
        except Exception:
            pass
    return entry


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
    others = [dict(t, who=_who(t), against=_against(t)) for t in running if t.get("matchId") not in my_ids]
    capacity = where.capacity if where else 0
    return render(
        request,
        "play.html",
        active="play",
        decks=imported_decks(conn) + [with_blurb(d) for d in queries.list_decks(conn, person.id)],
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
    their name at the table, the deck they play (a tracker deck id or curated precon id) and, from Discord,
    their picture."""

    name: str
    person: str
    deck_id: int | str
    avatar: str | None = None


def _load_tracker_deck(conn: sqlite3.Connection, deck_id: int, who: str) -> tuple[sqlite3.Row, str]:
    deck = conn.execute("SELECT * FROM decks WHERE id = ?", (deck_id,)).fetchone()
    if deck is None:
        raise ServiceError(f"{who} hasn't picked a deck")
    if not deck["commander"]:
        raise ServiceError(f"{deck['name']} has no commander. Pick one on its deck page.")
    detail = queries.get_deck(conn, deck_id, viewer_id=deck["owner_id"])
    return deck, queries.deck_exports(detail)["decklist"]


def start_table(
    conn: sqlite3.Connection, host, seats: list[Seat], picked: list[str], *, return_url: str
) -> tuple[dict, dict[str, str]]:
    """Starts a Commander game: the human seats, then the AI opponents in `picked`, 2 to MAX_SEATS
    seats in all, recorded in play_games under `host` (a tracker person) with the first seat's deck. Deck
    access is the caller's to check. Returns the game server's answer and each human seat's ticket by its
    `person`. Raises ServiceError with a message for the people at the table (the play page and the Discord
    Activity both start games)."""
    if not seats:
        raise ServiceError("Nobody is sitting down")
    if not 2 <= len(seats) + len(picked) <= MAX_SEATS:
        raise ServiceError(f"A game needs 2 to {MAX_SEATS} players, AIs included")
    preloaded: dict[int, tuple[sqlite3.Row, str]] = {}
    url_decks = {d["id"]: d for d in imported_decks(conn)}
    for seat in seats:
        if isinstance(seat.deck_id, int):
            preloaded[seat.deck_id] = _load_tracker_deck(conn, seat.deck_id, seat.name)
    where = tables.from_env()
    if where is None:
        raise ServiceError("No game server is set up for this tracker")
    try:
        roster = {o["id"]: o for o in where.lobby().opponents()}
        human_specs = []
        human_meta: list[tuple[int | None, str]] = []
        for seat in seats:
            if isinstance(seat.deck_id, int):
                deck, decklist = preloaded[seat.deck_id]
                human_specs.append({
                    "kind": "human",
                    "name": seat.name,
                    "person": seat.person,
                    "deck": {"name": deck["name"], "list": decklist},
                    **({"avatar": seat.avatar} if seat.avatar else {}),
                })
                human_meta.append((deck["id"], deck["name"]))
            elif str(seat.deck_id) in url_decks:
                imp = url_decks[str(seat.deck_id)]
                human_specs.append({
                    "kind": "human",
                    "name": seat.name,
                    "person": seat.person,
                    "deck": {"name": imp["name"], "list": imp["list"]},
                    **({"avatar": seat.avatar} if seat.avatar else {}),
                })
                human_meta.append((None, imp["name"]))
            else:
                pid = str(seat.deck_id).removeprefix("precon:")
                if pid not in roster:
                    raise ServiceError(f"{seat.name} hasn't picked a deck")
                human_specs.append({
                    "kind": "human",
                    "name": seat.name,
                    "person": seat.person,
                    "opponent": pid,
                    **({"avatar": seat.avatar} if seat.avatar else {}),
                })
                human_meta.append((None, roster[pid]["deck"]))
        ai_specs = []
        ai_decks: list[str] = []
        for o in picked:
            if o.startswith("deck:") and o.removeprefix("deck:") in url_decks:
                imp = url_decks[o.removeprefix("deck:")]
                cmd = (imp["commander"] or imp["name"]).split(",")[0]
                ai_specs.append({"kind": "ai", "name": cmd, "deck": {"name": imp["name"], "list": imp["list"]}})
                ai_decks.append(imp["name"])
            elif o in url_decks:
                imp = url_decks[o]
                cmd = (imp["commander"] or imp["name"]).split(",")[0]
                ai_specs.append({"kind": "ai", "name": cmd, "deck": {"name": imp["name"], "list": imp["list"]}})
                ai_decks.append(imp["name"])
            elif o.startswith("deck:") and o.removeprefix("deck:").isdigit():
                did = int(o.removeprefix("deck:"))
                deck, decklist = preloaded.get(did) or _load_tracker_deck(conn, did, "AI")
                cmd = (deck["commander"] or deck["name"]).split(",")[0]
                ai_specs.append({"kind": "ai", "name": cmd, "deck": {"name": deck["name"], "list": decklist}})
                ai_decks.append(deck["name"])
            elif o in roster:
                ai_specs.append({"kind": "ai", "opponent": o})
                ai_decks.append(roster[o]["deck"])
            else:
                raise ServiceError(f"Unknown opponent {o}")
        _settle(conn, where)
        spec = {
            "format": "commander",
            "seats": human_specs + ai_specs,
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
        deck_id=human_meta[0][0],
        deck_name=human_meta[0][1],
        opponents=[{"name": s["name"], "deck": m[1]} for s, m in zip(others, human_meta[1:], strict=True)]
        + [
            {"name": s["name"], "deck": dname}
            for s, dname in zip([s for s in started["seats"] if s["kind"] == "ai"], ai_decks, strict=True)
        ],
        ticket=tickets.get(str(host.id)),
    )
    started["deckName"] = human_meta[0][1]
    return started, tickets


def start_game(
    conn: sqlite3.Connection, person, deck_id: int | str | None, picked: list[str], *, return_url: str
) -> tuple[dict, str]:
    """A goldfish game from the play page: the person's own deck (or one imported from a URL) against
    1 to MAX_OPPONENTS curated opponents. Returns the game server's answer and the person's seat ticket."""
    if deck_id is None:
        raise ServiceError("Pick one of your decks")
    if not 1 <= len(picked) <= MAX_OPPONENTS:
        raise ServiceError(f"Pick one to {MAX_OPPONENTS} opponents")
    if isinstance(deck_id, int):
        require_own_deck(conn, deck_id, person)
    elif str(deck_id) not in {d["id"] for d in imported_decks(conn)}:
        raise ServiceError("Pick one of your decks")
    seat = Seat(person.display_name, str(person.id), deck_id)
    started, tickets = start_table(conn, person, [seat], picked, return_url=return_url)
    return started, tickets.get(str(person.id))


def _form_deck_id(value) -> int | str | None:
    raw = str(value or "").strip()
    if raw.startswith("url:"):
        return raw
    return form_int(raw)


@router.post("/play/import")
async def play_import(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Load a Commander list from a Moxfield, Archidekt, TappedOut or MTGGoldfish URL."""
    me(request)
    try:
        imp = import_deck_url(conn, str((await request.form()).get("url", "")).strip())
    except ServiceError as exc:
        return redirect("/play", error=str(exc))
    request.state.audit_notice = f"Imported deck {imp['name']}"
    return redirect("/play", notice=f"Imported {imp['name']}.")


@router.post("/play/start")
async def play_start(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    person = me(request)
    form = await request.form()
    picked = [str(o) for o in form.getlist("opponent") if str(o).strip()]
    try:
        started, ticket = start_game(conn, person, _form_deck_id(form.get("deck_id")), picked, return_url="/play")
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
