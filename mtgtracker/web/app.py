"""Server-rendered web UI over the tracker database.

This module owns the Jinja environment and create_app, which wires the
middlewares (web/middleware.py) and the routers (web/routes/). Request-level
helpers are in web/deps.py, and the write logic behind the forms is in
mtgtracker/services/. POSTs from another origin are rejected, so a random web
page can't drive the forms of this localhost app.

Every request opens its own short-lived connection (deps.get_conn), never
shared. Migrations run once, when the app is created.
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from mtgtracker import confirm, db
from mtgtracker.cardinfo import BASIC_LAND_NAMES, SECTION_ORDER
from mtgtracker.money import format_cents
from mtgtracker.web import filters, middleware, queries
from mtgtracker.web.api import v1 as api_v1
from mtgtracker.web.auth import DEV_COOKIE, AccessVerifier, AuthConfig
from mtgtracker.web.deps import require_person
from mtgtracker.web.routes import decks, exports, modify, pages, people, printings, purchases

WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=WEB_DIR / "templates")


templates.env.filters["money"] = format_cents

_MANA_SYMBOL_RE = re.compile(r"\{([^}]+)\}")


def mana_symbols(cost: str | None) -> Markup:
    """"{2}{R}{R}" -> Scryfall mana-symbol images (same source as the hover
    panel). Anything that isn't a {symbol} is escaped as plain text."""
    parts = []
    for i, piece in enumerate(_MANA_SYMBOL_RE.split(cost or "")):
        if i % 2 == 0:
            parts.append(escape(piece))
        else:
            name = quote(piece.replace("/", ""), safe="")
            parts.append(Markup(
                '<img class="mana" src="https://svgs.scryfall.io/card-symbols/{}.svg" alt="{{{}}}"'
                ' referrerpolicy="no-referrer" loading="lazy">'
            ).format(name, piece))
    return Markup("").join(parts)


templates.env.filters["mana_symbols"] = mana_symbols


def theme_attrs(identity) -> Markup:
    """Body attributes that theme a deck page by its colors: --accent is the
    first color's ink (links, buttons, labels), --accent-2 the last color's
    (secondary data), --identity a hard-stop stripe of all of them
    (colorless decks use the neutral ink). Only W/U/B/R/G ever reach the
    markup."""
    colors = [c for c in (identity or ()) if c in "WUBRG"] or ["C"]
    n = len(colors)
    stops = ", ".join(
        f"var(--ink-{c}) {100 * i / n:.2f}% {100 * (i + 1) / n:.2f}%" for i, c in enumerate(colors)
    )
    template = (
        'data-identity="{}" style="--accent: var(--ink-{}); --accent-2: var(--ink-{});'
        ' --identity: linear-gradient(90deg, {})"'
    )
    return Markup(template).format("".join(colors), colors[0], colors[-1], Markup(stops))


def identity_pips(identity) -> Markup:
    """A deck's colors as mana symbols ({C} for colorless)."""
    colors = [c for c in (identity or ()) if c in "WUBRG"] or ["C"]
    return mana_symbols("".join(f"{{{c}}}" for c in colors))


templates.env.globals["theme_attrs"] = theme_attrs
templates.env.globals["borrow_summary"] = queries.borrow_summary
templates.env.globals["identity_pips"] = identity_pips


@functools.cache
def static_version(path: str) -> str:
    """Short content hash of a static file, appended as ?v= so browsers can
    cache it forever yet pick up changes immediately. Computed once per
    process -- static files only change with a new build or restart."""
    return hashlib.sha256((WEB_DIR / "static" / path).read_bytes()).hexdigest()[:12]


templates.env.globals["static_version"] = static_version
templates.env.globals["basic_names"] = BASIC_LAND_NAMES
templates.env.globals["sorts"] = filters.SORTS
templates.env.globals["colors"] = filters.COLORS
templates.env.globals["type_labels"] = SECTION_ORDER
templates.env.globals["type_options"] = {label: label for label in SECTION_ORDER}
templates.env.globals["mana_values"] = filters.MANA_VALUES
templates.env.globals["color_modes"] = filters.COLOR_MODES


def create_app(db_path: str | Path, *, auth: AuthConfig | None = None, jwks_client=None) -> FastAPI:
    """The web app over db_path. `auth` defaults to AuthConfig.from_env()
    (MTG_AUTH etc.); tests pass one, plus a fake `jwks_client` for
    cloudflare mode."""
    auth = auth or AuthConfig.from_env()
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(db_path)
    db.migrate(conn)
    conn.close()

    app = FastAPI(title="mtgtracker", docs_url=None, redoc_url=None, openapi_url=None)
    # What request handlers and middleware need from setup (web/deps.py reads these).
    app.state.db_path = db_path
    app.state.auth = auth
    app.state.verifier = AccessVerifier(auth, jwks_client) if auth.mode == "cloudflare" else None
    app.state.templates = templates
    # Signs the confirm tokens system actors need for risky actions
    # (mtgtracker/confirm.py). Random per start unless MTG_CONFIRM_SECRET is set.
    secret = os.environ.get("MTG_CONFIRM_SECRET", "")
    if secret and len(secret) < 16:
        raise ValueError("MTG_CONFIRM_SECRET must be at least 16 characters")
    app.state.confirm_secret = secret.encode() or secrets.token_bytes(32)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    middleware.install(app)

    @app.exception_handler(confirm.ConfirmRequired)
    def confirm_required(request: Request, exc: confirm.ConfirmRequired):
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.message}, "preview": exc.preview,
             "confirm": exc.token},
            status_code=409,
        )

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz():
        return "ok"

    if auth.mode == "dev":
        @app.get("/dev/as/{user_id}", dependencies=[Depends(require_person)])
        def dev_as(user_id: int):
            """Local multi-user testing only: act as another user."""
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(DEV_COOKIE, str(user_id), httponly=True, samesite="lax")
            return response

    for module in (pages, decks, people, modify, exports, printings, purchases):
        app.include_router(module.router)
    api_v1.install(app)

    return app


def app_from_env() -> FastAPI:
    """App factory for `uvicorn --reload`, which needs an import string rather
    than an app object. `mtg serve --reload` passes the db path via env."""
    return create_app(os.environ.get("MTGTRACKER_DB", "data/mtgtracker.db"))
