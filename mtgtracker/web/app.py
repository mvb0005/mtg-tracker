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
from urllib.parse import quote, urlencode, urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from mtgtracker import cardinfo, commanders, db, deckmods, printings, scryfall, users
from mtgtracker.cardinfo import BASIC_LAND_NAMES, SECTION_ORDER
from mtgtracker.importers import collection_csv
from mtgtracker.web import filters, queries
from mtgtracker.web.auth import DEV_COOKIE, AccessVerifier, AuthConfig, NotSignedIn
from mtgtracker.web.auth import resolve as resolve_user

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


MAX_UPLOAD_BYTES = 5 * 1024 * 1024


def create_app(db_path: str | Path, *, auth: AuthConfig | None = None, jwks_client=None) -> FastAPI:
    """The web app over db_path. `auth` defaults to AuthConfig.from_env()
    (MTG_AUTH etc.); tests pass one, plus a fake `jwks_client` for
    cloudflare mode."""
    auth = auth or AuthConfig.from_env()
    verifier = AccessVerifier(auth, jwks_client) if auth.mode == "cloudflare" else None
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
        context.setdefault("user", getattr(request.state, "user", None))
        context.setdefault("can_edit", False)  # owner-only controls are opt-in per page
        context.setdefault("can_add_people", False)
        context.setdefault("auth_mode", auth.mode)
        return templates.TemplateResponse(request, template, context, status_code=status_code)

    def redirect(path: str, **params: str) -> RedirectResponse:
        query = urlencode({k: v for k, v in params.items() if v})
        return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)

    @app.middleware("http")
    async def identify(request: Request, call_next):
        """Who's asking (web/auth.py), as request.state.user. Static files
        and the kubelet's /healthz probe need no identity."""
        if not (request.url.path.startswith("/static/") or request.url.path == "/healthz"):
            conn = db.connect(db_path, check_same_thread=False)
            try:
                request.state.user = resolve_user(
                    conn, auth, verifier, client_host=request.client.host if request.client else None,
                    headers=request.headers, cookies=request.cookies,
                )
            except NotSignedIn as exc:
                return HTMLResponse(
                    templates.get_template("signed_out.html").render(reason=str(exc)), status_code=401
                )
            finally:
                conn.close()
        return await call_next(request)

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz():
        return "ok"

    if auth.mode == "dev":
        @app.get("/dev/as/{user_id}")
        def dev_as(user_id: int):
            """Local multi-user testing only: act as another user."""
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(DEV_COOKIE, str(user_id), httponly=True, samesite="lax")
            return response

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

    # --- who may do what -----------------------------------------------------
    # Friends can view each other's decks and cards; only an owner changes
    # their own. Every write path runs these guards on every id it's given,
    # so a posted deck_id or lot_id that isn't yours is a 403, never a quiet
    # way to take someone's card.

    def me(request: Request) -> users.User:
        return request.state.user

    def load_deck(conn: sqlite3.Connection, deck_id: int) -> sqlite3.Row:
        deck = conn.execute("SELECT * FROM decks WHERE id = ?", (deck_id,)).fetchone()
        if deck is None:
            raise HTTPException(status_code=404, detail="No such deck")
        return deck

    def require_own_deck(conn: sqlite3.Connection, deck_id: int, user: users.User) -> sqlite3.Row:
        deck = load_deck(conn, deck_id)
        if deck["owner_id"] != user.id:
            raise HTTPException(status_code=403, detail="That isn't your deck")
        return deck

    def require_own_lot(conn: sqlite3.Connection, lot_id: int | None, user: users.User) -> None:
        if lot_id is None:
            return
        lot = conn.execute("SELECT owner_id FROM lots WHERE id = ?", (lot_id,)).fetchone()
        if lot is None:
            raise HTTPException(status_code=404, detail="No such card")
        if lot["owner_id"] != user.id:
            raise HTTPException(status_code=403, detail="That card isn't yours")

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        user = me(request)
        return render(
            request, "dashboard.html",
            stats=queries.dashboard_stats(conn, user.id),
            transactions=queries.list_transactions(conn, user.id)[:5],
            decks=queries.list_decks(conn, user.id),
            needs_input=queries.needs_input(conn, user.id),
        )

    @app.get("/transactions", response_class=HTMLResponse)
    def transactions(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        return render(
            request, "transactions.html", transactions=queries.list_transactions(conn, me(request).id)
        )

    @app.get("/transactions/{transaction_id}", response_class=HTMLResponse)
    def transaction_detail(
        transaction_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ):
        detail = queries.get_transaction(conn, transaction_id)
        # Purchases are private: someone else's is simply not found.
        if detail is None or detail["txn"]["owner_id"] != me(request).id:
            raise HTTPException(status_code=404, detail="No such transaction")
        return render(request, "transaction_detail.html", **detail)

    @app.get("/decks", response_class=HTMLResponse)
    def decks(
        request: Request, notice: str = "", error: str = "", conn: sqlite3.Connection = Depends(get_conn)
    ):
        return render(
            request, "decks.html", decks=queries.list_decks(conn, me(request).id), notice=notice, error=error
        )

    @app.post("/decks/new")
    async def deck_new(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        form = await request.form()
        text = str(form.get("decklist", ""))
        if len(text) > 50_000:
            return redirect("/decks", error="That list is too long")
        try:
            summary = deckmods.create_deck_from_list(
                conn, str(form.get("name", "")), text, owner_id=me(request).id
            )
        except deckmods.ChangeError as exc:
            return redirect("/decks", error=str(exc))
        except httpx.HTTPError:
            return redirect("/decks", error="Couldn't reach Scryfall")
        return redirect(f"/decks/{summary.deck_id}", notice=summary.message())

    @app.get("/decks/{deck_id}", response_class=HTMLResponse)
    def deck_detail(
        deck_id: int, request: Request, view: str = "", notice: str = "", error: str = "",
        sort: str = "", color: list[str] = Query([]), color_mode: str = "", type: list[str] = Query([]),
        mv: list[str] = Query([]),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        card_filters = filters.CardFilters.parse(
            sort=sort, color=color, color_mode=color_mode, type=type, mv=mv
        )
        user = me(request)
        detail = queries.get_deck(conn, deck_id, card_filters, viewer_id=user.id)
        if detail is None:
            raise HTTPException(status_code=404, detail="No such deck")
        can_edit = detail["deck"]["owner_id"] == user.id
        commander_options = (
            commanders.eligible(conn, deck_id) if can_edit and detail["deck"]["format"] == "Commander" else []
        )
        return render(
            request, "deck_detail.html", view=view, notice=notice, error=error,
            commander_options=commander_options, filters=card_filters, can_edit=can_edit,
            owner=users.get(conn, detail["deck"]["owner_id"]), **detail,
        )

    @app.post("/decks/{deck_id}/commander")
    async def deck_set_commander(
        deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ):
        require_own_deck(conn, deck_id, me(request))
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

    def collection_page(
        request: Request, conn: sqlite3.Connection, owner: users.User, *, q, view, sort, color, color_mode,
        type, mv, deck, action: str,
    ) -> HTMLResponse:
        """A user's collection (/cards for your own, /people/{id}/cards for a
        friend's, read-only and without cost)."""
        mine = owner.id == me(request).id
        decks = queries.list_decks(conn, owner.id)
        card_filters = filters.CardFilters.parse(
            sort=sort, color=color, color_mode=color_mode, type=type, mv=mv, deck=deck,
            deck_ids={d["id"] for d in decks},
        )
        # Filter individual copies first (so a deck filter counts only that
        # deck's copies), then merge copies of the same card into one row.
        copies = queries.search_cards(conn, owner.id, q)
        if not mine:
            copies = [{**row, "cost_cents": None} for row in copies]
        matching = [row for row in copies if card_filters.matches(row)]
        card_rows = sorted(queries.collapse_by_name(matching), key=card_filters.sort_key)
        return render(
            request, "cards.html", card_rows=card_rows, sections=queries.sections_by_type(card_rows),
            q=q, view=view, filters=card_filters, decks=decks,
            deck_options={**{str(d["id"]): d["name"] for d in decks}, filters.NO_DECK: "Not in a deck"},
            facets=card_filters.facet_counts(copies),
            total=queries.count_cards(conn, owner.id), shown=sum(r["qty"] for r in card_rows),
            owner=owner, can_edit=mine, action=action,
        )

    @app.get("/cards", response_class=HTMLResponse)
    def cards(
        request: Request, q: str = "", view: str = "",
        sort: str = "", color: list[str] = Query([]), color_mode: str = "", type: list[str] = Query([]),
        mv: list[str] = Query([]), deck: list[str] = Query([]),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        return collection_page(request, conn, me(request), q=q, view=view, sort=sort, color=color,
                               color_mode=color_mode, type=type, mv=mv, deck=deck, action="/cards")

    # --- friends -------------------------------------------------------------

    def load_person(conn: sqlite3.Connection, person_id: int) -> users.User:
        person = users.get(conn, person_id)
        if person is None:
            raise HTTPException(status_code=404, detail="No such person")
        return person

    @app.get("/people", response_class=HTMLResponse)
    def people(
        request: Request, notice: str = "", error: str = "", conn: sqlite3.Connection = Depends(get_conn)
    ):
        rows = []
        for person in users.list_users(conn):
            rows.append({
                "user": person,
                "decks": len(queries.list_decks(conn, person.id)),
                "cards": queries.count_cards(conn, person.id),
            })
        return render(request, "people.html", people=rows, notice=notice, error=error,
                      can_add_people=may_add_people(request))

    def may_act_for(request: Request, person: users.User) -> bool:
        """Acting on someone else's collection (importing, later loans) needs
        a real sign-in mode: in single mode every visitor is the admin."""
        actor = me(request)
        if person.id == actor.id:
            return True
        return auth.mode in ("dev", "cloudflare") and users.can_act_for(actor, person)

    def may_add_people(request: Request) -> bool:
        """Adding a person adds an email to the sign-in allowlist. In single
        mode every visitor is the admin, so the web form stays off there
        (use `mtg add-user`) -- otherwise a stranger on the public site could
        pre-authorize themselves for when Cloudflare sign-in is turned on."""
        return auth.mode in ("dev", "cloudflare") and me(request).is_admin

    @app.post("/people")
    async def people_add(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        if not may_add_people(request):
            raise HTTPException(status_code=403, detail="Only a signed-in admin can add people")
        form = await request.form()
        try:
            person = users.create(conn, str(form.get("display_name", "")), str(form.get("email", "")))
        except users.UserError as exc:
            return redirect("/people", error=str(exc))
        return redirect("/people", notice=f"Added {person.display_name}.")

    @app.get("/people/{person_id}", response_class=HTMLResponse)
    def person_page(
        person_id: int, request: Request, notice: str = "", error: str = "",
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        person = load_person(conn, person_id)
        can_import = may_act_for(request, person)
        imports = conn.execute(
            "SELECT original_filename, format, card_count, imported_at FROM collection_imports"
            " WHERE owner_id = ? ORDER BY id DESC", (person.id,),
        ).fetchall() if can_import else []
        return render(
            request, "person.html", person=person, decks=queries.list_decks(conn, person.id),
            card_count=queries.count_cards(conn, person.id), can_import=can_import, imports=imports,
            notice=notice, error=error,
        )

    @app.post("/people/{person_id}/import")
    async def person_import(person_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        """Import a collection CSV into this person's collection: your own,
        or (as an admin) for someone who hasn't signed in yet."""
        person = load_person(conn, person_id)
        actor = me(request)
        if not may_act_for(request, person):
            raise HTTPException(status_code=403, detail="You can't import into that collection")
        back = f"/people/{person.id}"
        # Refuse an oversized body before the form parser spools it.
        if int(request.headers.get("content-length") or 0) > MAX_UPLOAD_BYTES + 64 * 1024:
            return redirect(back, error="That file is too big (5 MB at most)")
        form = await request.form()
        upload = form.get("file")
        if upload is None or isinstance(upload, str) or not upload.filename:
            return redirect(back, error="Choose a CSV file to import")
        data = await upload.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            return redirect(back, error="That file is too big (5 MB at most)")
        try:
            summary = collection_csv.import_collection(
                conn, data, owner_id=person.id, imported_by=actor.id, filename=upload.filename[:200]
            )
        except collection_csv.CollectionImportError as exc:
            return redirect(back, error=str(exc))
        except httpx.HTTPError:
            return redirect(back, error="Couldn't reach Scryfall; nothing was imported")
        return redirect(back, notice=summary.message())

    @app.get("/people/{person_id}/cards", response_class=HTMLResponse)
    def person_cards(
        person_id: int, request: Request, q: str = "", view: str = "",
        sort: str = "", color: list[str] = Query([]), color_mode: str = "", type: list[str] = Query([]),
        mv: list[str] = Query([]), deck: list[str] = Query([]),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        person = load_person(conn, person_id)
        return collection_page(request, conn, person, q=q, view=view, sort=sort, color=color,
                               color_mode=color_mode, type=type, mv=mv, deck=deck,
                               action=f"/people/{person.id}/cards")

    # --- deck modifications & versions ---------------------------------------

    def modify_page(
        request: Request, conn: sqlite3.Connection, deck_id: int,
        *, q: str = "", notice: str = "", error: str = "", status_code: int = 200,
    ) -> HTMLResponse:
        user = me(request)
        deck = require_own_deck(conn, deck_id, user)
        # Display rows (same shape as the deck page's) so changes, the
        # replacement picker and search results render as readable card
        # tiles with the shared hover panel / tap sheet.
        groups = queries.get_deck(conn, deck_id, viewer_id=user.id)["groups"]
        by_name: dict[str, dict] = {}
        for group in groups:
            by_name.setdefault(group["card_name"], group)

        pending = deckmods.pending_changes(conn, deck_id)
        for change in pending:
            art = cardinfo.lookup(change["add_scryfall_id"], change["add_card_name"],
                                  change["add_finish"] or "nonfoil")
            if not change["ready"]:
                where = "Not owned yet (to acquire)"
            elif change["add_from_deck_name"]:
                where = f"In {change['add_from_deck_name']} (would be borrowed)"
            else:
                where = "In your collection, not in a deck"
            change["insert_row"] = {
                "card_name": change["add_card_name"], "art": art, "qty": 1,
                "finish": change["add_finish"] or "nonfoil", "set_code": change["add_set_code"],
                "collector_number": change["add_collector_number"], "cost_cents": change["add_cost_cents"],
                "where": where, "decks": [], "disposition_type": None,
            }
            if not change["ready"]:
                change["insert_row"]["printing"] = "Not owned yet"
            cut = by_name.get(change["cut_card_name"]) if change["cut_card_name"] else None
            change["cut_row"] = {**cut, "qty": 1} if cut else None
            change["owned_copies"] = (
                [] if change["ready"] else deckmods.owned_copies_of(conn, deck_id, change["add_card_name"])
            )

        identity = queries.deck_identities(conn).get(deck_id, ())
        friend_copies = queries.FriendCopies(conn, {user.id})
        for change in pending:
            if not change["ready"]:
                change["insert_row"]["friends"] = friend_copies(change["add_card_name"])
        # Cards the deck lists but you don't own, each replaceable by one you do.
        wanted = []
        for group in deckmods.wanted_groups(conn, deck_id):
            art = cardinfo.lookup(group["scryfall_id"], group["card_name"], "nonfoil")
            row = {
                "card_name": group["card_name"], "art": art, "qty": group["qty"], "finish": "nonfoil",
                "set_code": None, "collector_number": None, "cost_cents": None, "decks": [],
                "disposition_type": None, "category": art.category(group["card_name"]),
                "wanted": True, "where": "Not owned yet: need to acquire", "printing": "Not owned yet",
                "friends": friend_copies(group["card_name"]),
            }
            wanted.append({**group, "row": row})
        owned_rows = []
        for item in deckmods.owned_fitting(conn, deck_id, identity):
            owned_rows.append({
                **item, "category": item["art"].category(item["card_name"]), "decks": [],
                "disposition_type": None,
                "where": (f"In {item['holder_name']}" if item["holder_name"]
                          else "In your collection, not in a deck"),
            })
        owned_rows.sort(key=lambda r: cardinfo.mana_value_order(r["card_name"], r["art"]))

        cut_choices = []
        for option in deckmods.cut_options(conn, deck_id):
            row = by_name.get(option["card_name"])
            if row:
                cut_choices.append({**row, "cut_lot_id": option["lot_id"], "qty": option["qty"]})
        cut_choices.sort(key=lambda r: cardinfo.mana_value_order(r["card_name"], r["art"]))

        ready = [c for c in pending if c["ready"]]
        size = deckmods.list_size(conn, deck_id)
        candidates = deckmods.owned_candidates(conn, deck_id, q)
        for candidate in candidates:
            candidate.update(
                art=cardinfo.lookup(candidate["scryfall_id"], candidate["card_name"], candidate["finish"]),
                where=(f"{candidate['qty']} in {candidate['holder_name']}" if candidate["holder_name"]
                       else f"{candidate['qty']} not in a deck"),
                decks=[], disposition_type=None, cost_cents=None,
            )
        return render(
            request, "deck_modify.html", status_code=status_code, can_edit=True,
            identity=identity,
            deck=deck, pending=pending, ready_count=len(ready),
            size=size, size_after=size + len(pending) - sum(1 for c in pending if c["cut_lot_id"]),
            cut_options=deckmods.cut_options(conn, deck_id),
            cut_sections=queries.sections_by_type(cut_choices),
            wanted=[w for w in wanted if w["want_id"] is not None],
            wanted_total=sum(w["qty"] for w in wanted),
            own_sections=queries.sections_by_type(owned_rows), own_count=len(owned_rows),
            candidates=candidates, q=q,
            versions=deckmods.versions(conn, deck_id),
            notice=notice, error=error,
        )

    @app.get("/decks/{deck_id}/modify", response_class=HTMLResponse)
    def deck_modify(
        deck_id: int, request: Request, q: str = "", notice: str = "", error: str = "",
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        return modify_page(request, conn, deck_id, q=q, notice=notice, error=error)

    def change_action(deck_id: int, conn: sqlite3.Connection, action, notice: str, q: str = "",
                      *, user: users.User):
        """Run a deck_changes edit on the user's own deck; redirect back to
        the modify page with a notice, or with the error if it was rejected."""
        require_own_deck(conn, deck_id, user)
        try:
            result = action()
        except deckmods.ChangeError as exc:
            return redirect(f"/decks/{deck_id}/modify", error=str(exc), q=q)
        except httpx.HTTPError:
            return redirect(f"/decks/{deck_id}/modify", error="Couldn't reach Scryfall", q=q)
        return redirect(f"/decks/{deck_id}/modify", notice=notice.format(result=result), q=q)

    def form_int(value) -> int | None:
        value = str(value or "").strip()
        return int(value) if value.isdigit() else None

    @app.post("/decks/{deck_id}/changes")
    async def deck_add_change(deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        user = me(request)
        form = await request.form()
        q = str(form.get("q", ""))
        lot_id = form_int(form.get("lot_id"))
        if lot_id is not None:
            require_own_lot(conn, lot_id, user)
            return change_action(deck_id, conn, lambda: deckmods.add_owned(conn, deck_id, lot_id),
                                 "Added to the change list.", q, user=user)
        name = str(form.get("planned_name", ""))
        return change_action(deck_id, conn, lambda: deckmods.add_planned(conn, deck_id, name),
                             "Added to the change list as to acquire.", q, user=user)

    @app.post("/decks/{deck_id}/import")
    async def deck_import_list(deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        text = str((await request.form()).get("decklist", ""))
        if len(text) > 50_000:
            return redirect(f"/decks/{deck_id}/modify", error="That list is too long")
        return change_action(deck_id, conn, lambda: deckmods.import_list(conn, deck_id, text).message(),
                             "{result}", user=me(request))

    @app.post("/decks/{deck_id}/changes/{change_id}/cut")
    async def deck_change_cut(
        deck_id: int, change_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ):
        user = me(request)
        form = await request.form()
        cut, cut_want = form_int(form.get("cut_lot_id")), form_int(form.get("cut_want_id"))
        require_own_lot(conn, cut, user)
        return change_action(
            deck_id, conn, lambda: deckmods.set_cut(conn, deck_id, change_id, cut, cut_want_id=cut_want),
            "Replacement updated.", user=user,
        )

    @app.post("/decks/{deck_id}/swaps")
    async def deck_swap(deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        """Replace a card the deck lists but you don't own with one you do."""
        user = me(request)
        form = await request.form()
        lot_id, want_id = form_int(form.get("lot_id")), form_int(form.get("want_id"))
        if lot_id is None or want_id is None:
            require_own_deck(conn, deck_id, user)
            return redirect(
                f"/decks/{deck_id}/modify", error="Pick a card to replace and one of yours to use"
            )
        require_own_lot(conn, lot_id, user)
        return change_action(deck_id, conn, lambda: deckmods.add_swap(conn, deck_id, lot_id, want_id),
                             "Queued the swap; apply to make it the next version.", user=user)

    @app.post("/decks/{deck_id}/changes/{change_id}/link")
    async def deck_change_link(
        deck_id: int, change_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ):
        user = me(request)
        lot_id = form_int((await request.form()).get("lot_id"))
        if lot_id is None:
            require_own_deck(conn, deck_id, user)
            return redirect(f"/decks/{deck_id}/modify", error="Pick a copy to use")
        require_own_lot(conn, lot_id, user)
        return change_action(deck_id, conn, lambda: deckmods.link_owned(conn, deck_id, change_id, lot_id),
                             "Linked your copy; the change is ready.", user=user)

    @app.post("/decks/{deck_id}/changes/{change_id}/delete")
    def deck_change_delete(
        deck_id: int, change_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ):
        return change_action(deck_id, conn, lambda: deckmods.remove_change(conn, deck_id, change_id),
                             "Removed from the change list.", user=me(request))

    @app.post("/decks/{deck_id}/apply")
    async def deck_apply(deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        note = str((await request.form()).get("note", ""))
        return change_action(deck_id, conn, lambda: deckmods.apply_changes(conn, deck_id, note),
                             "Applied as version {result}.", user=me(request))

    # --- exports -------------------------------------------------------------

    def export_detail(request: Request, conn: sqlite3.Connection, deck_id: int) -> dict:
        detail = queries.get_deck(conn, deck_id, viewer_id=me(request).id)
        if detail is None:
            raise HTTPException(status_code=404, detail="No such deck")
        return detail

    @app.get("/decks/{deck_id}/export", response_class=HTMLResponse)
    def deck_export_page(deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        """The deck as text to copy or download: decklist, shopping list, and
        what to borrow from each friend. Read-only, so friends can use it too."""
        detail = export_detail(request, conn, deck_id)
        return render(
            request, "deck_export.html", deck=detail["deck"], identity=detail["identity"],
            exports=queries.deck_exports(detail),
        )

    @app.get("/decks/{deck_id}/export/{kind}.txt", response_class=PlainTextResponse)
    def deck_export_text(
        deck_id: int, kind: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ):
        detail = export_detail(request, conn, deck_id)
        exports = queries.deck_exports(detail)
        texts = {"decklist": exports["decklist"], "shopping": exports["shopping"]}
        texts |= {f"borrow-{b['id']}": b["text"] for b in exports["borrow"]}
        if kind not in texts:
            raise HTTPException(status_code=404, detail="No such export")
        slug = re.sub(r"[^a-z0-9]+", "-", detail["deck"]["name"].lower()).strip("-") or "deck"
        return PlainTextResponse(
            texts[kind], headers={"Content-Disposition": f'attachment; filename="{slug}-{kind}.txt"'}
        )

    @app.get("/decks/{deck_id}/versions/{version}", response_class=HTMLResponse)
    def deck_version(
        deck_id: int, version: int, request: Request, view: str = "",
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        deck = conn.execute("SELECT * FROM decks WHERE id = ?", (deck_id,)).fetchone()
        lot_ids = deckmods.version_lot_ids(conn, deck_id, version) if deck else None
        if lot_ids is None:
            raise HTTPException(status_code=404, detail="No such deck version")
        rows = queries.lots_as_rows(conn, lot_ids, where=f"On the v{version} list",
                                    wants=deckmods.version_wants(conn, deck_id, version),
                                    viewer_id=me(request).id)
        info = next(v for v in deckmods.versions(conn, deck_id) if v["version"] == version)
        return render(
            request, "deck_version.html", deck=deck, version=info,
            identity=queries.deck_identities(conn).get(deck_id, ()),
            can_edit=deck["owner_id"] == me(request).id,
            sections=queries.sections_by_type(rows), total=sum(r["qty"] for r in rows),
        )

    @app.get("/printings", response_class=HTMLResponse)
    def printings_queue(
        request: Request, notice: str = "", conn: sqlite3.Connection = Depends(get_conn)
    ):
        return render(
            request, "printings.html",
            groups=printings.unresolved_groups(conn, me(request).id),
            cached_sets=printings.cached_set_codes(),
            notice=notice,
        )

    def resolve_page(
        request: Request, conn: sqlite3.Connection, lot_id: int,
        *, notice: str = "", error: str = "", status_code: int = 200,
    ) -> HTMLResponse:
        require_own_lot(conn, lot_id, me(request))
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
        require_own_lot(conn, lot_id, me(request))
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
    async def printing_fetch_set(lot_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        """Explicit, user-initiated Scryfall lookup of a whole set (cached
        afterwards). Page loads never hit the network."""
        require_own_lot(conn, lot_id, me(request))
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
