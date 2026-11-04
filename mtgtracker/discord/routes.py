"""/discord/: the Discord Activity's page and API.

Inside Discord the Activity's root is this tracker's /discord/ (the application's root URL mapping), so the
page and everything it loads live under /discord/. The sign-in middleware skips /discord and /discord/...
(and Cloudflare Access lets them through): every route here authenticates by Discord, the API by the session
token from POST /discord/api/session. Players are their Discord ids; they pick from the owner's decks.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from mtgtracker import db, scryfall
from mtgtracker.discord import auth
from mtgtracker.play import lobbies, tables
from mtgtracker.play import routes as play
from mtgtracker.services import ServiceError
from mtgtracker.web.deps import render

router = APIRouter()
STATIC = Path(__file__).parent / "static"
PAGE_FILES = ("activity.js", "activity.css", "lobby.js", "lobby.css")  # what the Activity's page loads


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
        version={name: asset_version(name) for name in PAGE_FILES},
        # Inside Discord the footer's links open in the browser, which needs this site's own address.
        legal=f"https://{request.headers.get('host', '')}/discord/",
    )
    page.headers["Cache-Control"] = "no-cache"  # so a new deploy's files reach an Activity opened before
    return page


@router.get("/discord/terms", response_class=HTMLResponse)
def terms(request: Request):
    """The Terms of Service, here so they're public: the application's settings in Discord link to them."""
    return render(request, "terms.html", version={"activity.css": asset_version("activity.css")})


@router.get("/discord/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return render(request, "privacy.html", version={"activity.css": asset_version("activity.css")})


def asset_version(name: str) -> str:
    """Short content hash of a file under /discord/static, appended as ?v= (as the tracker's static_version
    does). Not cached: the Activity's page is no-cache, so a new deploy's files reach an open Activity."""
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
    except ServiceError as exc:
        return _error(401, "sign_in", str(exc))
    session = discord.session(
        who["id"], who["name"], avatar=who["avatar"], guest=bool(who.get("guest"))
    )
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


def _decks(conn: sqlite3.Connection) -> list[dict]:
    owner = auth.deck_owner(conn)
    return [] if owner is None else play.lobby_decks(conn, owner.id)


def _live(you: str, my_name: str = "") -> tuple[list[dict], set[str]]:
    """Running games across the tables, and the Discord user ids of everyone seated in them."""
    where = tables.from_env()
    if where is None:
        return [], set()
    try:
        running = where.running()
    except ServiceError:
        return [], set()
    person = f"discord:{you}"
    in_game: set[str] = set()
    out = []
    for t in running:
        match_id = t.get("matchId")
        if not match_id:
            continue
        seats = t.get("seats") or []
        seat_ticket = None
        for s in seats:
            p = s.get("person") or ""
            if p.startswith("discord:"):
                in_game.add(p.removeprefix("discord:"))
            if s.get("kind") == "human" and s.get("ticket"):
                if p == person or (my_name and s.get("name") == my_name):
                    seat_ticket = s.get("ticket")
        in_game.update(lobbies.seated_ids(match_id))
        humans = [s.get("name", "Player") for s in seats if s.get("kind") == "human"]
        ais = [s.get("name", "AI") for s in seats if s.get("kind") == "ai"]
        my_ticket = lobbies.ticket_for(match_id, you) or seat_ticket
        seated = any(s.get("person") == person for s in seats) or my_ticket is not None
        out.append({
            "matchId": match_id,
            "who": ", ".join(humans) or "Someone",
            "against": ", ".join(ais),
            "seats": [{"name": s.get("name"), "kind": s.get("kind"), "deck": s.get("deck")} for s in seats],
            "watchTicket": t.get("watchTicket"),
            "ticket": my_ticket,
            "seated": seated,
        })
    return out, in_game


def _end_match(conn: sqlite3.Connection, match_id: str) -> None:
    """Ends a running match from the Activity lobby."""
    where = tables.from_env()
    if where is None or not match_id:
        return
    try:
        server = where.server_for(match_id)
        if server is not None:
            server.end(match_id)
    except ServiceError:
        pass
    play.games.record_result(conn, {"matchId": match_id, "status": "ended", "summary": "Ended"})
    lobbies.clear_match(match_id)


def _view(
    room: lobbies.Lobby,
    you: str,
    decks: list[dict],
    roster: list[dict],
    live: list[dict],
    in_game: set[str],
    problem: str | None,
    guest: bool = False,
) -> dict:
    by_id: dict[int | str, dict] = {d["id"]: d for d in decks}
    by_id |= {o["id"]: play.roster_deck(o) for o in roster}
    view = play.lobby_view(
        room, you, by_id.get, decks, roster, problem,
        # Someone already seated in a running game watches from the table, unless they left it.
        shown=lambda m: not m.in_game and (m.id not in in_game or m.left_table),
    )
    return view | {"live": live, "guest": guest}


@router.post("/discord/api/room")
async def room(request: Request, conn: sqlite3.Connection = Depends(signed_in)):
    """The Activity instance's lobby, polled by everyone in it: {instanceId} marks you present and returns the
    room; add {deckId} to pick your deck, {opponents: [id]} to set the room's AI opponents, {start: true} to
    start the game with everyone who has picked a deck."""
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict) or not lobbies.valid_instance(str(body.get("instanceId", ""))):
        return _error(400, "bad_request", "No Activity instance")
    me = request.state.session
    guest = bool(me.get("g"))
    imported = play.lobby_import(conn, body)
    decks = _decks(conn)
    try:
        roster, problem = play.opponents(), None
    except ServiceError as exc:
        roster, problem = [], str(exc)
    with lobbies.LOCK:
        key = lobbies.activity_key(body["instanceId"])
        room = lobbies.touch(key, me["d"], me["n"], me.get("a"), guest=guest)
        if "inGame" in body:
            room.members[me["d"]].in_game = bool(body["inGame"])
            room.members[me["d"]].left_table = not bool(body["inGame"])
        start = lobbies.apply(conn, room, me["d"], body, decks, roster, play.MAX_SEATS, imported)
    if isinstance(body.get("endMatchId"), str) and body["endMatchId"].strip():
        _end_match(conn, body["endMatchId"].strip())
    if start:
        # Back from the table is the Activity's lobby (its root). Recorded under the deck owner.
        play.start_lobby(
            conn, room, auth.deck_owner(conn), *start, return_url="/", person=lambda m: f"discord:{m.id}"
        )
    live, in_game = _live(me["d"], me.get("n", ""))
    with lobbies.LOCK:
        return _view(room, me["d"], decks, roster, live, in_game, problem, guest=guest)


@router.post("/discord/api/trace")
async def trace(request: Request):
    """Where the table page's Discord voice setup got to (it runs in Discord's iframe, whose console nobody
    sees): one short line in the log per step. Rate limited, and nothing is kept."""
    if not TRACES.allow():
        return Response(status_code=429)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    step = str(body.get("step", ""))[:60] if isinstance(body, dict) else ""
    detail = str(body.get("detail", ""))[:200] if isinstance(body, dict) else ""
    print(f"discord voice: {step} {detail}".rstrip(), flush=True)
    return Response(status_code=204)


_IMG_LOCK = threading.Lock()
_CMD_URLS: dict[str, str] = {}


def _img_dir(request: Request) -> Path:
    db_path = getattr(request.app.state, "db_path", None)
    base = Path(db_path).parent if db_path else Path("/tmp")
    d = base / "commander-imgs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _front_name(name: str) -> str:
    return name.split("//")[0].strip()


def _warm_commander_urls(names: list[str], img_dir: Path) -> None:
    urls_file = img_dir / "urls.json"
    with _IMG_LOCK:
        if not _CMD_URLS and urls_file.exists():
            try:
                _CMD_URLS.update(json.loads(urls_file.read_text()))
            except Exception:
                pass
        missing = [
            _front_name(n)
            for n in dict.fromkeys(names)
            if n and _front_name(n).lower() not in _CMD_URLS
        ]
        if not missing:
            return
        headers = {"User-Agent": scryfall.USER_AGENT, "Accept": "application/json"}
        found_urls: dict[str, str] = {}
        try:
            with httpx.Client(timeout=15.0) as client:
                for i in range(0, len(missing), 75):
                    batch = [{"name": n} for n in missing[i : i + 75]]
                    res = client.post(
                        "https://api.scryfall.com/cards/collection",
                        json={"identifiers": batch},
                        headers=headers,
                    )
                    if res.status_code != 200:
                        continue
                    for card in res.json().get("data", []):
                        uris = card.get("image_uris") or ((card.get("card_faces") or [{}])[0].get("image_uris")) or {}
                        url = uris.get("normal") or uris.get("small")
                        if not url:
                            continue
                        cname = card.get("name") or ""
                        found_urls[cname.lower()] = url
                        found_urls[_front_name(cname).lower()] = url
        except httpx.HTTPError:
            return
        if found_urls:
            _CMD_URLS.update(found_urls)
            try:
                urls_file.write_text(json.dumps(_CMD_URLS))
            except Exception:
                pass


@router.get("/discord/api/card-img")
def card_img(request: Request, name: str = ""):
    """Serves a commander's card image from /data/commander-imgs (fetching once from cards.scryfall.io)."""
    front = _front_name(name)
    if not front:
        return Response(status_code=404)
    img_dir = _img_dir(request)
    key = hashlib.sha256(front.lower().encode()).hexdigest()[:20]
    cached_file = img_dir / f"{key}.jpg"
    if cached_file.is_file():
        return Response(
            content=cached_file.read_bytes(),
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )
    all_names = [front] + [o.get("commander", "") for o in play._roster[1]] + list(play.DECK_BLURBS.keys())
    _warm_commander_urls(all_names, img_dir)
    url = _CMD_URLS.get(front.lower())
    if not url:
        try:
            with httpx.Client(timeout=10.0, headers={"User-Agent": scryfall.USER_AGENT}) as client:
                r = client.get("https://api.scryfall.com/cards/named", params={"fuzzy": front})
                if r.status_code == 200:
                    card = r.json()
                    uris = card.get("image_uris") or ((card.get("card_faces") or [{}])[0].get("image_uris")) or {}
                    url = uris.get("normal") or uris.get("small")
                    if url:
                        with _IMG_LOCK:
                            _CMD_URLS[front.lower()] = url
        except httpx.HTTPError:
            pass
    if not url:
        return Response(status_code=404)
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True, headers={"User-Agent": scryfall.USER_AGENT}) as client:
            res = client.get(url)
    except httpx.HTTPError:
        return Response(status_code=502)
    if res.status_code != 200:
        return Response(status_code=res.status_code)
    try:
        cached_file.write_bytes(res.content)
    except Exception:
        pass
    return Response(
        content=res.content,
        media_type=res.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


TRACES = auth.Limiter(limit=120)
