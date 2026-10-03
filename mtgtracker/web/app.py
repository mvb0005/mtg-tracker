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

from mtgtracker import cardinfo, commanders, db, deckmods, printings, scryfall
from mtgtracker.cardinfo import BASIC_LAND_NAMES, SECTION_ORDER
from mtgtracker.web import filters, queries

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
    def decks(
        request: Request, notice: str = "", error: str = "", conn: sqlite3.Connection = Depends(get_conn)
    ):
        return render(request, "decks.html", decks=queries.list_decks(conn), notice=notice, error=error)

    @app.post("/decks/new")
    async def deck_new(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        form = await request.form()
        text = str(form.get("decklist", ""))
        if len(text) > 50_000:
            return redirect("/decks", error="That list is too long")
        try:
            summary = deckmods.create_deck_from_list(conn, str(form.get("name", "")), text)
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
        detail = queries.get_deck(conn, deck_id, card_filters)
        if detail is None:
            raise HTTPException(status_code=404, detail="No such deck")
        commander_options = (
            commanders.eligible(conn, deck_id) if detail["deck"]["format"] == "Commander" else []
        )
        return render(
            request, "deck_detail.html", view=view, notice=notice, error=error,
            commander_options=commander_options, filters=card_filters, **detail,
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
        request: Request, q: str = "", view: str = "",
        sort: str = "", color: list[str] = Query([]), color_mode: str = "", type: list[str] = Query([]),
        mv: list[str] = Query([]), deck: list[str] = Query([]),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        decks = queries.list_decks(conn)
        card_filters = filters.CardFilters.parse(
            sort=sort, color=color, color_mode=color_mode, type=type, mv=mv, deck=deck,
            deck_ids={d["id"] for d in decks},
        )
        # Filter individual copies first (so a deck filter counts only that
        # deck's copies), then merge copies of the same card into one row.
        copies = queries.search_cards(conn, q)
        matching = [row for row in copies if card_filters.matches(row)]
        card_rows = sorted(queries.collapse_by_name(matching), key=card_filters.sort_key)
        return render(
            request, "cards.html", card_rows=card_rows, sections=queries.sections_by_type(card_rows),
            q=q, view=view, filters=card_filters, decks=decks,
            deck_options={**{str(d["id"]): d["name"] for d in decks}, filters.NO_DECK: "Not in a deck"},
            facets=card_filters.facet_counts(copies),
            total=queries.count_cards(conn), shown=sum(r["qty"] for r in card_rows),
        )

    # --- deck modifications & versions ---------------------------------------

    def modify_page(
        request: Request, conn: sqlite3.Connection, deck_id: int,
        *, q: str = "", notice: str = "", error: str = "", status_code: int = 200,
    ) -> HTMLResponse:
        deck = conn.execute("SELECT * FROM decks WHERE id = ?", (deck_id,)).fetchone()
        if deck is None:
            raise HTTPException(status_code=404, detail="No such deck")
        # Display rows (same shape as the deck page's) so changes, the
        # replacement picker and search results render as readable card
        # tiles with the shared hover panel / tap sheet.
        groups = queries.get_deck(conn, deck_id)["groups"]
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
        # Cards the deck lists but you don't own, each replaceable by one you do.
        wanted = []
        for group in deckmods.wanted_groups(conn, deck_id):
            art = cardinfo.lookup(group["scryfall_id"], group["card_name"], "nonfoil")
            row = {
                "card_name": group["card_name"], "art": art, "qty": group["qty"], "finish": "nonfoil",
                "set_code": None, "collector_number": None, "cost_cents": None, "decks": [],
                "disposition_type": None, "category": art.category(group["card_name"]),
                "wanted": True, "where": "Not owned yet: need to acquire", "printing": "Not owned yet",
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
            request, "deck_modify.html", status_code=status_code,
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

    def change_action(deck_id: int, conn: sqlite3.Connection, action, notice: str, q: str = ""):
        """Run a deck_changes edit; redirect back to the modify page with a
        notice, or with the error if it was rejected."""
        if conn.execute("SELECT 1 FROM decks WHERE id = ?", (deck_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="No such deck")
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
        form = await request.form()
        q = str(form.get("q", ""))
        lot_id = form_int(form.get("lot_id"))
        if lot_id is not None:
            return change_action(deck_id, conn, lambda: deckmods.add_owned(conn, deck_id, lot_id),
                                 "Added to the change list.", q)
        name = str(form.get("planned_name", ""))
        return change_action(deck_id, conn, lambda: deckmods.add_planned(conn, deck_id, name),
                             "Added to the change list as to acquire.", q)

    @app.post("/decks/{deck_id}/import")
    async def deck_import_list(deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        text = str((await request.form()).get("decklist", ""))
        if len(text) > 50_000:
            return redirect(f"/decks/{deck_id}/modify", error="That list is too long")
        return change_action(deck_id, conn, lambda: deckmods.import_list(conn, deck_id, text).message(),
                             "{result}")

    @app.post("/decks/{deck_id}/changes/{change_id}/cut")
    async def deck_change_cut(
        deck_id: int, change_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ):
        form = await request.form()
        cut, cut_want = form_int(form.get("cut_lot_id")), form_int(form.get("cut_want_id"))
        return change_action(
            deck_id, conn, lambda: deckmods.set_cut(conn, deck_id, change_id, cut, cut_want_id=cut_want),
            "Replacement updated.",
        )

    @app.post("/decks/{deck_id}/swaps")
    async def deck_swap(deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        """Replace a card the deck lists but you don't own with one you do."""
        form = await request.form()
        lot_id, want_id = form_int(form.get("lot_id")), form_int(form.get("want_id"))
        if lot_id is None or want_id is None:
            return redirect(
                f"/decks/{deck_id}/modify", error="Pick a card to replace and one of yours to use"
            )
        return change_action(deck_id, conn, lambda: deckmods.add_swap(conn, deck_id, lot_id, want_id),
                             "Queued the swap; apply to make it the next version.")

    @app.post("/decks/{deck_id}/changes/{change_id}/link")
    async def deck_change_link(
        deck_id: int, change_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ):
        lot_id = form_int((await request.form()).get("lot_id"))
        if lot_id is None:
            return redirect(f"/decks/{deck_id}/modify", error="Pick a copy to use")
        return change_action(deck_id, conn, lambda: deckmods.link_owned(conn, deck_id, change_id, lot_id),
                             "Linked your copy; the change is ready.")

    @app.post("/decks/{deck_id}/changes/{change_id}/delete")
    def deck_change_delete(deck_id: int, change_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        return change_action(deck_id, conn, lambda: deckmods.remove_change(conn, deck_id, change_id),
                             "Removed from the change list.")

    @app.post("/decks/{deck_id}/apply")
    async def deck_apply(deck_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        note = str((await request.form()).get("note", ""))
        return change_action(deck_id, conn, lambda: deckmods.apply_changes(conn, deck_id, note),
                             "Applied as version {result}.")

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
                                    wants=deckmods.version_wants(conn, deck_id, version))
        info = next(v for v in deckmods.versions(conn, deck_id) if v["version"] == version)
        return render(
            request, "deck_version.html", deck=deck, version=info,
            identity=queries.deck_identities(conn).get(deck_id, ()),
            sections=queries.sections_by_type(rows), total=sum(r["qty"] for r in rows),
        )

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
