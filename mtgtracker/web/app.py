"""Server-rendered web UI over the tracker database.

Mostly read-only; the write flows are pinning unresolved lots to a printing
(/printings) and choosing a deck's commander. POSTs from another origin are rejected, so a random web page
can't drive the forms of this localhost app.

Every request opens its own short-lived connection. FastAPI runs sync
dependencies and endpoints in a threadpool, so a connection may be created on
one thread and used on another -- hence check_same_thread=False, which is safe
because a connection is never shared between requests. Migrations run once,
when the app is created.
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from mtgtracker import commanders, db, printings, scryfall
from mtgtracker.cardinfo import BASIC_LAND_NAMES
from mtgtracker.web import queries

SET_CODE_RE = re.compile(r"^[a-z0-9]{2,8}$")

WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=WEB_DIR / "templates")


def format_cents(cents: int | None, currency: str = "USD") -> str:
    """Integer cents -> "$1,234.56". Integer arithmetic only, never floats."""
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    dollars, remainder = divmod(abs(cents), 100)
    amount = f"{dollars:,}.{remainder:02d}"
    if currency == "USD":
        return f"{sign}${amount}"
    return f"{sign}{amount} {currency}"


templates.env.filters["money"] = format_cents


@functools.cache
def static_version(path: str) -> str:
    """Short content hash of a static file, appended as ?v= so browsers can
    cache it forever yet pick up changes immediately. Computed once per
    process -- static files only change with a new build or restart."""
    return hashlib.sha256((WEB_DIR / "static" / path).read_bytes()).hexdigest()[:12]


templates.env.globals["static_version"] = static_version
templates.env.globals["basic_names"] = BASIC_LAND_NAMES


def create_app(db_path: str | Path) -> FastAPI:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(db_path)
    db.migrate(conn)
    conn.close()

    app = FastAPI(title="mtgtracker", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(db_path, check_same_thread=False)
        try:
            yield conn
        finally:
            conn.close()

    def render(request: Request, template: str, status_code: int = 200, **context) -> HTMLResponse:
        return templates.TemplateResponse(request, template, context, status_code=status_code)

    def redirect(path: str, **params: str) -> RedirectResponse:
        query = urlencode({k: v for k, v in params.items() if v})
        return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)

    @app.middleware("http")
    async def static_cache_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            # Versioned URLs (?v=<content hash>) never change; anything else
            # must be revalidated so a stale stylesheet can't stick around.
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable" if "v" in request.query_params else "no-cache"
            )
        return response

    @app.middleware("http")
    async def reject_cross_origin_writes(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            cross_site = request.headers.get("sec-fetch-site") == "cross-site"
            if cross_site or (origin and urlsplit(origin).netloc != request.headers.get("host")):
                return PlainTextResponse("Cross-origin request rejected", status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        return render(
            request, "dashboard.html",
            stats=queries.dashboard_stats(conn),
            transactions=queries.list_transactions(conn)[:5],
            decks=queries.list_decks(conn),
            needs_input=queries.needs_input(conn),
        )

    @app.get("/transactions", response_class=HTMLResponse)
    def transactions(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        return render(request, "transactions.html", transactions=queries.list_transactions(conn))

    @app.get("/transactions/{transaction_id}", response_class=HTMLResponse)
    def transaction_detail(
        transaction_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ):
        detail = queries.get_transaction(conn, transaction_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="No such transaction")
        return render(request, "transaction_detail.html", **detail)

    @app.get("/decks", response_class=HTMLResponse)
    def decks(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        return render(request, "decks.html", decks=queries.list_decks(conn))

    @app.get("/decks/{deck_id}", response_class=HTMLResponse)
    def deck_detail(
        deck_id: int, request: Request, view: str = "", notice: str = "", error: str = "",
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        detail = queries.get_deck(conn, deck_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="No such deck")
        commander_options = (
            commanders.eligible(conn, deck_id) if detail["deck"]["format"] == "Commander" else []
        )
        return render(
            request, "deck_detail.html", view=view, notice=notice, error=error,
            commander_options=commander_options, **detail,
        )

    @app.post("/decks/{deck_id}/commander")
    async def deck_set_commander(
        deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ):
        form = await request.form()
        card_name = str(form.get("commander", "")).strip() or None
        try:
            commanders.set_commander(conn, deck_id, card_name)
        except commanders.CommanderError as exc:
            if conn.execute("SELECT 1 FROM decks WHERE id = ?", (deck_id,)).fetchone() is None:
                raise HTTPException(status_code=404, detail="No such deck") from exc
            return redirect(f"/decks/{deck_id}", error=str(exc))
        notice = f"{card_name} is now the commander." if card_name else "Commander cleared."
        return redirect(f"/decks/{deck_id}", notice=notice)

    @app.get("/cards", response_class=HTMLResponse)
    def cards(
        request: Request, q: str = "", view: str = "", conn: sqlite3.Connection = Depends(get_conn)
    ):
        return render(request, "cards.html", card_rows=queries.search_cards(conn, q), q=q, view=view)

    @app.get("/printings", response_class=HTMLResponse)
    def printings_queue(
        request: Request, notice: str = "", conn: sqlite3.Connection = Depends(get_conn)
    ):
        return render(
            request, "printings.html",
            groups=printings.unresolved_groups(conn),
            cached_sets=printings.cached_set_codes(),
            notice=notice,
        )

    def resolve_page(
        request: Request, conn: sqlite3.Connection, lot_id: int,
        *, notice: str = "", error: str = "", status_code: int = 200,
    ) -> HTMLResponse:
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

    @app.get("/printings/{lot_id}", response_class=HTMLResponse)
    def printing_resolve(
        lot_id: int, request: Request, notice: str = "", error: str = "",
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        return resolve_page(request, conn, lot_id, notice=notice, error=error)

    @app.post("/printings/{lot_id}")
    async def printing_assign(
        lot_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ):
        group = printings.group_for_lot(conn, lot_id)
        if group is None:
            raise HTTPException(status_code=404, detail="No unresolved card with that id")

        form = await request.form()
        assignments: list[tuple[str, str, int]] = []
        try:
            if choice := form.get("choice"):
                scryfall_id, finish = str(choice).split("|", 1)
                assignments.append((scryfall_id, finish, 1))
            for key, value in form.multi_items():
                if key.startswith("qty|") and str(value).strip():
                    _, scryfall_id, finish = key.split("|", 2)
                    assignments.append((scryfall_id, finish, int(value)))
            assigned = printings.assign(conn, lot_id, assignments)
        except (ValueError, printings.PrintingError) as exc:
            message = str(exc) if isinstance(exc, printings.PrintingError) else "Invalid form input"
            return resolve_page(request, conn, lot_id, error=message, status_code=400)

        notice = f"Pinned {assigned} {group.card_name}."
        if assigned < group.qty:
            return redirect(f"/printings/{group.lot_ids[assigned]}", notice=notice)
        return redirect("/printings", notice=notice)

    @app.post("/printings/{lot_id}/fetch-set")
    async def printing_fetch_set(lot_id: int, request: Request):
        """Explicit, user-initiated Scryfall lookup of a whole set (cached
        afterwards). Page loads never hit the network."""
        form = await request.form()
        set_code = str(form.get("set_code", "")).strip().lower()
        if not SET_CODE_RE.match(set_code):
            return redirect(f"/printings/{lot_id}", error=f"'{set_code}' doesn't look like a set code")
        try:
            cards = scryfall.fetch_set_cards(set_code)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return redirect(f"/printings/{lot_id}", error=f"Scryfall has no set '{set_code}'")
            return redirect(f"/printings/{lot_id}", error=f"Scryfall error {exc.response.status_code}")
        except httpx.HTTPError:
            return redirect(f"/printings/{lot_id}", error="Couldn't reach Scryfall")
        return redirect(
            f"/printings/{lot_id}", notice=f"Loaded {len(cards)} printings from {set_code.upper()}."
        )

    return app


def app_from_env() -> FastAPI:
    """App factory for `uvicorn --reload`, which needs an import string rather
    than an app object. `mtg serve --reload` passes the db path via env."""
    return create_app(os.environ.get("MTGTRACKER_DB", "data/mtgtracker.db"))
