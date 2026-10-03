# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Personal, local-first MTG collection and spending tracker. A SQLite database is the source of truth for money and card ownership. It is deliberately not a deckbuilder or a card scanner. See README.md for the roadmap and the known gaps from the first real import.

## Commands

```
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # setup
.venv/bin/pytest                                             # all tests
.venv/bin/pytest tests/test_decks.py::test_borrow_cut_reassemble_worked_example   # single test
.venv/bin/pytest --cov=mtgtracker --cov-branch --cov-report=term-missing  # coverage
.venv/bin/ruff check .                                       # lint (config in pyproject.toml)
.venv/bin/mtg import-order path/to/Order.order               # CLI (typer app in mtgtracker/cli.py)
.venv/bin/mtg list-decks
.venv/bin/mtg reassemble-deck <deck-id>
.venv/bin/mtg serve [--db PATH] [--port 8000] [--reload]      # web UI, binds 127.0.0.1 (MTG_AUTH=dev to try several users)
.venv/bin/mtg add-user NAME [--email E] [--admin]            # users exist only if added (sign-in allowlist)
.venv/bin/mtg rename-user 1 "Pat"                            # user 1 starts as "Owner"; import-order takes --user EMAIL|ID
python scripts/scrub_order.py <in.order> <out.order>         # make a real .order safe to commit as a fixture
deploy/minikube.sh up | load-db | pull-db | import FILE.order | forward [PORT] | down | destroy   # minikube (see below); forward reconnects across redeploys
```

The CLI defaults to `data/mtgtracker.db` and `receipts/`, and every command runs migrations on connect. Linting is ruff, configured in `pyproject.toml`. `ruff format` is intentionally not enforced. `B008` is suppressed for `typer.Option`/`Argument` and `fastapi.Depends` via `extend-immutable-calls`.

## Architecture

**Data flow:** `mtg import-order` → `importers/order_wallet.py` (Apple Wallet `.order` zip → `transactions` + `products` + content-addressed `receipts/<sha256>.order`) → for each product, `importers/precon_decklist.py` matches `display_name` against `seed/precon_decklists/*.json`. On a match it resolves the list against Scryfall, creates a deck with one `lots` row per physical card (all equipped), and calls `allocation.allocate()`.

**Schema:** `mtgtracker/migrations/NNNN_*.sql`, applied in order by `db.migrate()`. Progress is tracked with `PRAGMA user_version`, which equals the count of applied files. Add new migrations as new numbered files and never edit applied ones. There is no ORM. All reads and writes are explicit SQL in each module. `models.py` holds plain dataclasses only, and connections use `sqlite3.Row`.

**Key invariants (don't break these):**
- **Money is integer cents.** Parse amounts with `money.parse_cents` (Decimal, raises on fractional cents). Never use floats. Every split goes through `money.largest_remainder_split`, so totals reconcile exactly with a deterministic tie-break by index.
- **Order import reconciles or raises.** Subtotal + tax + shipping must equal the total, and the line items must sum to the subtotal. Tax and shipping are allocated pro-rata into each product's `cost_basis_cents`. Re-import is idempotent by receipt sha256. Transactions upsert on `(order_type_identifier, order_identifier)` and only when the incoming `updatedAt` is newer. Products are created only on first insert.
- **One lot = one physical card.** Quantities are never collapsed. Cost basis is split evenly by lot count, not by card value. `allocate()` raises `AlreadyDisposedError` if any lot in the product has a disposition.
- **Wanted cards are never lots.** A deck's list is its `deck_cards` rows (copies you own, equipped here or elsewhere) plus its `deck_wants` rows (migration 0004): cards you don't own yet, one row per copy, "to acquire". Never create lots or transactions for unowned cards. Wants stay out of `/cards`, collection totals, and cost basis, but every reader of a deck's list must include them: display groups (`queries._wanted_rows`, `wanted=True`), identity, breakdown, counts, version snapshots (`deck_version_wants`), the commander check in `apply_changes`, and import skips.
- **Deck membership vs. physical location:** a `deck_cards` row means the card is on that deck's list. `equipped=1` means the deck physically holds it, and a partial unique index allows only one equipped deck per lot. `add_to_deck(equipped=True)` unequips the card elsewhere first. `borrow` moves `equipped`, `cut` deletes the row, and `reassemble` lists cards on a deck's list that aren't equipped there.
- **Resolution is honest, not guessed.** A card name with multiple printings in the set (e.g. Sol Ring, Arcane Signet in `fdc`) is reported as ambiguous. Such cards, not-found names, and basic lands still get lots, but with `scryfall_id` NULL. The commander is expected to be foil.
- **Seeding is recoverable.** `seed_precon_deck_for_product` is a no-op if the product already has lots. `import-order` always attempts seeding, so re-running it recovers from a failed Scryfall fetch.

**Web UI** (`mtgtracker/web/`): FastAPI + Jinja2, server-rendered, with no JS build. It is one CSS file with light/dark tokens. The file is linked as `style.css?v=<content hash>` via `static_version()`, and `/static` responses get `immutable` when versioned and `no-cache` otherwise. Reference any new static file the same way, or browsers will keep a stale copy. `create_app(db_path)` runs migrations once. Each request opens its own connection with `check_same_thread=False`, because FastAPI's threadpool may create it on one thread and use it on another. Never share a connection. Page-only read SQL lives in `web/queries.py`. Money renders only through the `money` Jinja filter (`format_cents`, integer math). Never serve `receipts/`, since those files contain credentials. Page loads never call the Scryfall API. The only network fetch is the explicit "look up set" POST, which goes through the cached `fetch_set_cards`. Card images are hotlinked from Scryfall's image CDN (intentional). They're looked up through `mtgtracker/cardinfo.py`, which indexes `scryfall.cached_cards()`, a parse of every cached set that's memoized until the cache files change. Pinned lots show their exact printing. Unpinned lots show a representative printing flagged `exact=False`: one that matches the lot's finish and isn't a promo. `cardinfo` also sorts cards into deck sections by type line and supplies mana value (`cmc`). Deck grids show a Commander section first, using `decks.commander` (added in migration 0002 and set by seeding), then the type sections. Deck and card lists order by mana value, highest first. Among equal mana values, colored mana outranks colorless: more distinct colors, then more colored symbols ({1}{U}{R} > {1}{R}{R} > {3}; `cardinfo.color_weight`). Name breaks remaining ties. Decks and cards default to an image grid, and `?view=list` gives the table. Tiles have no captions. A card that isn't physically in the deck (or has been disposed of, on `/cards`) is greyed out with the `.away` class. Unpinned non-basics get a "needs printing" flag.

**Design:** a printed deck-sheet look. Avoid generic "app" styling: no purple, no boxed cards, no pill tags, no rainbow charts.
- **Surface and type:** warm paper (`--bg`), ink text, and `--font-display` (Spectral from Google Fonts, falling back to Georgia) for the wordmark, headings, and headline numbers. The body uses the system sans.
- **Structure:** sections are separated by rules, not boxes. `h2` is the serif heading over a hairline. `h3` labels are uppercase, tracked, and in the accent. Headline numbers are a ledger row (`.stats` / `dl.ledger`). Tags are squared small caps. Corners use `--radius` (4px); card images keep their own card-shaped radius.
- **Deck color themes:** `cardinfo.deck_identity()` gives a deck's colors, from the commander's color identity or otherwise every non-land card's colors. `queries.deck_identities()` computes all decks in one query.
  - On deck, modify, and version pages, the `theme_attrs(identity)` Jinja global sets `data-identity`, `--accent: var(--ink-X)`, and `--identity`, a hard-stop stripe of all the colors, on `<body>` via the `body_attrs` block.
  - The header's bottom edge becomes that stripe, and so do the strong rules: the ledger, the chart columns, and the list section heads. They're drawn as a 2px background line, so hairlines stay hairlines.
  - Links, buttons, section labels, the mana curve, and list-row tints follow `--accent` (the first color). Card-type bars use `--accent-2` (the last color), so a two-color deck shows both in its data. A one-color deck sets both to the same ink.
  - `identity_pips()` renders the colors as mana symbols.
  - `--ink-*` colors are text-safe on `--bg` in both modes. Other pages use the neutral `--ink-C`.

**Deck breakdown and list view:** the deck page opens with a breakdown built by `web/breakdown.py` from the display groups: a mana curve of non-land cards, mana colors (each non-land card counted once, with Multicolor and Colorless buckets), and card types. It's plain HTML with no chart library. Mana colors use the `--mana-*` tokens, the dataviz skill's validated categorical hues mapped to mana meaning; re-run `validate_palette.js` if you change them. **Color buckets** are `breakdown._color_key`:
- a card's own color(s) in WUBRG order: "R", or "UR" for a multicolor card, labelled "Blue-Red"
- `C` for colorless
- `X` when the colors aren't cached

Each bucket carries a CSS `fill` (`_fill`), which templates use as inline `background` for segments and swatches alike: the `--mana-*` hue for one color, 135° stripes of each color for a combination, and hatched gray for `X`.

**One-color decks** draw the mana curve in `--accent` and card types in `--accent-2`, as plain bars. **Decks with two or more colors** (`deck_breakdown(groups, identity)` → `stacked`) split both charts by color bucket: curve bars bottom-up, type bars left to right. Both use 2px gaps, per-segment tooltips, and their own counted legend.

`cardinfo.lookup` treats an uncached basic land as known (colorless, cmc 0, "Basic Land — X"). (`TYPE_SLOTS`/`--series-N` remain in the data for a categorical view, but nothing renders them now.) Legends always show counts, so color never carries meaning alone. The list view (`?view=list`) is two CSS columns of type sections, each row showing qty, name, and cost via the `mana_symbols` Jinja filter. Rows carry `data-card` for the panel.

**Sorting and filtering:** this is a shop-style sidebar on deck pages and `/cards`, for both views, built by `facet_sidebar` and `results_bar` in `_card_tiles.html` inside a `.catalog` grid.
- **Filtering logic:** `web/filters.py` (`CardFilters`) handles the multi-value GET facets `color`, `type`, `mv`, and on `/cards` also `deck`, plus `sort` and `color_mode`.
  - Within a facet, values are OR'd. The exception is color with `color_mode=all`, where they're AND'd. Facets are AND'd with each other.
  - Type matching uses every type on the type line (`CardInfo.type_labels`), so an Artifact Creature matches both Artifacts and Creatures. Display sections still use one `category` each.
  - `facet_counts` counts each facet with the other facets applied, not itself.
  - `parse()` drops unknown values, and `href()` keeps the current filters and view in links.
- **Page behavior:**
  - The Sort select sits in the results bar and belongs to the form via `form="facet-form"`.
  - `static/filters.js` submits on every change and drops empty or default params.
  - Below 900px the sidebar is a "Filters" disclosure that is always folded by default. An inline script right after the `<details>` does this before first paint, so the sidebar doesn't flash open.
  - At 600px and below, the header nav wraps. Keep every page free of horizontal page scroll down to 390px wide.
  - On deck pages, filters narrow only `sections`. The counts, breakdown, and commander picker always cover the whole deck.
  - On `/cards`, filters run on individual copies first, then `queries.collapse_by_name` merges copies of the same card into one row, whatever their deck or printing, with a `where`/`printing`/`finish` summary for the panel.

**Layout:** `base.html` wraps `main` and the panel in `.page`. On wide hover-capable screens, that becomes a centered grid of the content column plus a 356px sticky panel. `--content-max` (1040px, or 1284px on `.wide` catalog pages to fit the 220px sidebar) sets the content width.

**Deck versions and modifications:** `mtgtracker/deckmods.py`, migration 0003, and `/decks/{id}/modify`.
- **Versions:**
  - `decks.version` is the current version. 0 means never recorded, e.g. a bare `create_deck`.
  - `decks.record_version()` bumps it and snapshots the whole list into `deck_version_cards`. Seeding records v1 after the lots exist, and the migration backfilled existing decks as v1.
  - Diffs are computed between consecutive snapshots.
- **Changes:** `deck_changes` rows insert a card, optionally replacing (`cut_lot_id`) a card on the list.
  - An owned insert references a lot, which may be listed in another deck; applying it borrows that copy.
  - A planned insert has `add_lot_id` NULL and is found by `scryfall.fetch_named_card`, which caches to `CACHE_DIR/cards/`, is included in `cached_cards()`, and is never a set.
- **Applying:** `apply_changes()` applies every pending change as one version, all or nothing. It validates the batch first, then writes in one transaction without using the committing `decks.*` helpers.
  - Owned inserts are equipped here (borrowed if another deck held them).
  - Planned inserts become `deck_wants` rows.
  - Replaced cards leave the list and become unassigned.
  - The commander is cleared only when no copy of it remains, owned or wanted.
- **New deck from a list:** `deckmods.create_deck_from_list` backs `POST /decks/new`. It resolves on Scryfall first, then writes the deck, owned copies (`equipped=0`, so the deck starts unassembled and nothing moves), wants, the commander from the list's Commander section (`Entry.commander`, owned or not), and the v1 snapshot in one transaction. The deck page shows three states: here, `.away` (owned, in another deck), and `.wanted` (dashed, faded, "To acquire"). Reassemble splits into per-deck pull checklists and a to-acquire list. Version diffs compare card-name multisets, so acquiring a wanted card won't show as a change.
- **List import:** `importers/decklist_text.py` parses Moxfield/Archidekt/Arena text: an optional qty or `Nx`, `(SET) number`, `*F*`/`*E*`, and section headers, with sideboard-like sections skipped. `deckmods.import_list` resolves names through `scryfall.fetch_collection` (POST `/cards/collection`, 75 per batch, cached per card, with up to 10 fuzzy retries). It skips copies already in the deck or queued, so re-imports are harmless, uses free owned lots first (unassigned before borrowed), queues the rest as planned, and writes all of it in one transaction.
- **Modify page UI:** each change shows the card to add and the card it replaces as readable `card_face` tiles, a link-free tile macro that can sit inside a button, with panel data.
  - The replacement picker (`#cut-picker`) is rendered once and moved under whichever change opens it by `static/deck-modify.js`. It shows the deck's cards as tiles grouped by type, with hover/panel, and clicking a tile submits that change's `/cut`.
  - Without JS, each change falls back to a `<noscript>` `<select>`.
  - Don't render a picker per change: 40+ queued changes would mean thousands of tiles.
- **Replacing cards you don't own:** a change can cut a `deck_wants` row (`cut_want_id`, migration 0005, deliberately with no foreign key because apply deletes the want) instead of a lot.
  - `add_swap` queues "add this owned copy, replacing that wanted card".
  - `wanted_groups` lists the deck's wants, holding back copies already being replaced.
  - `owned_fitting` lists insertable owned cards whose color identity fits the deck's colors. Colorless fits, and uncached cards are offered too.
  - On the Modify page, the "Cards you don't own" section shares one `#own-picker`, rendered once and moved by `deck-modify.js`, which puts the clicked card's type section first. The `#cut-picker` also offers wanted cards via `cut_want_id`.
- **Known gap:** only precon seeding creates lots today, so there's no purchase flow yet to turn a wanted card into an owned one.

**Card size:** the grid's `--tile-min` (190px) is the smallest readable card width. The grid drops a column rather than shrinking cards: 5 across when the results area is 1008px, 4 at 1600px with the panel showing. Phones use 140px for 2 columns.

**Card details panel:** on wide screens with a mouse (`(hover: hover) and (min-width: 1480px)`), hovering or focusing a card shows a sticky detail panel on the right. Everywhere else, including phones, tablets, and narrow windows, tapping a card or pressing Enter/Space on it opens the same content in a modal `<dialog>` sheet (`#card-dialog`, a bottom sheet on phones). Taps on links and controls inside a card keep their normal behavior. Both are driven by `static/card-panel.js`, whose `render()` builds the content for either. In both, the Decks row lists every deck whose list has the card, as `1× Deck` links. When there's more than one deck, the one that holds the card is labeled current (`queries._decks_for`, merged by `_merge_decks` on `/cards`). The sheet animates: it slides up and down on phones, and lifts and fades on wider screens. The open animation is pure CSS. Closing adds `.closing` and waits for `animationend` before `dialog.close()`, and this covers Escape (`cancel`), backdrop taps, and swipe-down. There's no close button. On phones, dragging the sheet down once its content is scrolled to the top moves it with the finger. Release past about a third of its height, or flick faster than 0.5 px/ms, to close; otherwise it snaps back. Swiping sideways steps to the next or previous card, and so do the arrow keys in the sheet. The order is the page's `.tile[data-card]` or `.deck-row[data-card]` elements, so it follows the current sort and filters, and a position counter shows where you are. The first ~8px of movement decides between sideways and vertical. At either end of the list the sheet resists and springs back. It's instant under `prefers-reduced-motion`. When the pop-up has a card image, it hides the first face's repeated name, type, and rules text via CSS. Placeholder cards and double-faced backs keep their text, and the desktop panel always shows it. Every tile and list row carries `data-card='<json>'`, built by the `panel_json` macro in `_card_tiles.html` from `CardInfo` (faces, rarity, set, artist) plus the row's location and cost. The script builds DOM nodes with `textContent`, never `innerHTML`, and turns `{X}` into Scryfall mana-symbol SVGs. The panel's image slot has a fixed aspect ratio so the text doesn't jump while images load. Pages opt in with `{% block body_class %}with-panel{% endblock %}` plus the `panel` block.

**Users and sign-in** (migration 0006, `mtgtracker/users.py`, `web/auth.py`):
- **Ownership:** every lot, deck and transaction has `owner_id` (existing rows belong to user 1, the original owner and an admin). `lots.transaction_id` is nullable, so a card can exist without a recorded purchase.
- **Auth mode** comes from `MTG_AUTH`: `single` (default; everyone is user 1, as before), `dev` (local only; `/dev/as/{id}` sets a cookie), or `cloudflare`.
  - In `cloudflare` mode, the Access JWT (header or `CF_Authorization` cookie) is verified with RS256 against the team's keys, checking `aud` (`CF_ACCESS_AUD`), `iss` (`https://CF_ACCESS_TEAM_DOMAIN`) and `exp`.
  - Its email must match an existing user: an allowlist, never auto-created. `MTG_OWNER_EMAIL` claims user 1 on its first sign-in.
  - Loopback clients act as user 1 so port-forward works. That's only safe because uvicorn runs with `proxy_headers=False` and cloudflared runs in its own pod.
  - Anything else gets the 401 `signed_out.html`. `/static/` and `/healthz` (the k8s probes) need no identity.
- **Scoping:** `request.state.user` is set by the `identify` middleware. List queries take an `owner_id`.
- **Visibility:** friends can view each other's decks, deck versions, `/people/{id}` and `/people/{id}/cards`, with `can_edit=False`, so there's no Modify, commander picker, printing links or cost-sort.
  - Money stays private: someone else's lot shows cost `None`, a deck's cost basis shows only to its owner, and another user's transaction is a 404.
- **Write guards:** every write route calls `require_own_deck` / `require_own_lot` on every id it's posted (403). `deckmods`' candidate and free-lot queries offer only the deck owner's lots, and `_check_insertable` refuses others' lots too.
- **Acting for others:** `users.can_act_for` lets an admin act for someone who has never signed in. It's used by the coming loans phase.
- **Admins:** an admin adds people at `/people`, but only in `dev`/`cloudflare` mode. In `single` mode every visitor is the admin, so the web can't touch the allowlist there; use `mtg add-user`.
- **`can_edit` fails closed:** `render()` defaults it to False, and pages pass True only when the viewer owns what's shown. Gate every new owner-only control on it.

**Commander:** `mtgtracker/commanders.py` lets any legendary creature on a Commander-format deck's list be its commander, checked against the cached type line. The deck page has the picker (`POST /decks/{id}/commander`). There is one commander per deck, with no partners. A middleware rejects POSTs whose `Origin` doesn't match `Host`, or that carry `Sec-Fetch-Site: cross-site`. Write routes use POST, then a 303 redirect, with the notice or error passed as a query param.

**Printings** (`mtgtracker/printings.py`) is the backend for `/printings`. It pins unresolved lots (`scryfall_id IS NULL`) to a printing the user picks from the physical card.
- **Candidates** come from every `*.json` in `scryfall.CACHE_DIR`. Always read `CACHE_DIR` as a module attribute at call time so tests can patch it.
- **Groups:** lots are grouped by card name, finish, product, and current equipped deck (NULL-safe `IS`). A group is addressed by its lowest lot id.
- **Assigning:** `assign()` validates the name, the finish against the printing's `finishes`, the quantity (at most the group size), and that the lots are still unresolved. It writes nothing if any of these fail. It fills lots in ascending id order and upserts `scryfall_cards`. Picking a printing can change `lots.finish`.
- **Seeding stays honest.** Never auto-pick a printing in `precon_decklist.py`.

**Test fixtures** (`tests/conftest.py`): `db_path` is a copy of a session-scoped, seeded, file-backed DB. Seeding it fresh per test would take seconds on WSL because of per-card commits. `cache_dir` patches `scryfall.CACHE_DIR` to a temp copy of the fdc fixture, and makes any httpx request fail. Use `install_transport(monkeypatch, httpx.MockTransport(...))` to fake a Scryfall fetch. Web tests must use `cache_dir`, or requests would read the real gitignored cache.

**Scryfall:** `scryfall.py` fetches one whole set through the search API and caches it in `.scryfall_cache/<set>.json`, which is gitignored. It never makes per-card calls. Tests must stay network-free: `test_precon_decklist.py` monkeypatches `scryfall.CACHE_DIR` to a temp dir seeded from `tests/fixtures/scryfall_fdc.json`.

**Seed decklists** (`seed/precon_decklists/*.json`) have the keys `deck_name`, `product_display_name` (must exactly match the cleaned product title), `format`, `commander`, `set_code`, `cards` (unique non-basic names), and `basics` (name → count). `cards` plus the `basics` counts must total 100. A test enforces this.

## minikube

`Dockerfile` plus `deploy/k8s/` (kustomize), driven by `deploy/minikube.sh`.
- **Image:** `pip install -e .` at `/app` on purpose. `SEED_DIR` and `scryfall.CACHE_DIR` resolve relative to the source tree, so a site-packages install would break seeding. The image is non-root, with a read-only root filesystem.
- **Storage:** one PVC at `/data` holds the DB, the receipts, and the Scryfall cache (mounted into `/app/.scryfall_cache` via `subPath`).
- **Deployment:** `replicas: 1`, `strategy: Recreate`, because SQLite sits on an RWO volume. The image tag is `mtgtracker:dev` with `imagePullPolicy: Never`, built by `minikube image build`. `up` does a `rollout restart` to pick up a rebuild.
- **Access:** the Service is ClusterIP only, reached with `forward`, which binds 127.0.0.1. There is no NodePort or Ingress.
- **Cloudflare Tunnel:** optional, in `deploy/k8s/tunnel/`, and applied by `deploy/minikube.sh tunnel`, not by `up`. It's a remotely managed tunnel: the hostname and service URL (`mtgtracker.mtgtracker.svc.cluster.local:80`) live in the Cloudflare dashboard. The connector reads the tunnel token from the `cloudflared-token` Secret. The script creates it from, in order, piped stdin, `CLOUDFLARE_TUNNEL_TOKEN` in the gitignored `.env`, or a hidden prompt. `.env` is parsed by `env_value` and never sourced. Never put the token in argv or a tracked file. `.env` is also in `.dockerignore`. The user chose a fully public site with no Cloudflare Access, so the app has no auth. The only write protection is the same-origin check on POSTs.
- **Moving data:** `load-db` and `pull-db` both take SQLite online-backup snapshots and move them into place under a temp name. Never copy the live file. The cluster DB is a separate copy that the UI writes to. `pull-db` keeps the old local file as `data/mtgtracker.backup-<timestamp>.db`, which is gitignored by `data/*.db`.
- **Keep data out of the image:** `.dockerignore` must keep excluding `data/`, `receipts/`, `.claude/`, and `tests/`. The scrubbed fixture still contains a card's last four digits.

## Sensitive data

Real `.order` files contain auth tokens and PII. `receipts/*` and `data/*.db` are gitignored. Only scrubbed fixtures (via `scripts/scrub_order.py`) belong in `tests/fixtures/`.
