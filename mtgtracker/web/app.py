"""Server-rendered, read-only web UI over the tracker database.

Every request opens its own short-lived connection. FastAPI runs sync
dependencies and endpoints in a threadpool, so a connection may be created on
one thread and used on another -- hence check_same_thread=False, which is safe
because a connection is never shared between requests. Migrations run once,
when the app is created.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from mtgtracker import db
from mtgtracker.web import queries

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

    def render(request: Request, template: str, **context) -> HTMLResponse:
        return templates.TemplateResponse(request, template, context)

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
    def deck_detail(deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        detail = queries.get_deck(conn, deck_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="No such deck")
        return render(request, "deck_detail.html", **detail)

    @app.get("/cards", response_class=HTMLResponse)
    def cards(request: Request, q: str = "", conn: sqlite3.Connection = Depends(get_conn)):
        return render(request, "cards.html", cards=queries.search_cards(conn, q), q=q)

    return app


def app_from_env() -> FastAPI:
    """App factory for `uvicorn --reload`, which needs an import string rather
    than an app object. `mtg serve --reload` passes the db path via env."""
    return create_app(os.environ.get("MTGTRACKER_DB", "data/mtgtracker.db"))
