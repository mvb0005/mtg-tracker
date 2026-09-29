# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Personal, local-first, single-user MTG collection and spending tracker. A SQLite database is the source of truth for money and card ownership. It is deliberately not a deckbuilder or a card scanner. See README.md for the roadmap and the known gaps from the first real import.

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
.venv/bin/mtg serve [--db PATH] [--port 8000] [--reload]      # web UI, binds 127.0.0.1
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
- **Deck membership vs. physical location:** a `deck_cards` row means the card is on that deck's list. `equipped=1` means the deck physically holds it, and a partial unique index allows only one equipped deck per lot. `add_to_deck(equipped=True)` unequips the card elsewhere first. `borrow` moves `equipped`, `cut` deletes the row, and `reassemble` lists cards on a deck's list that aren't equipped there.
- **Resolution is honest, not guessed.** A card name with multiple printings in the set (e.g. Sol Ring, Arcane Signet in `fdc`) is reported as ambiguous. Such cards, not-found names, and basic lands still get lots, but with `scryfall_id` NULL. The commander is expected to be foil.
- **Seeding is recoverable.** `seed_precon_deck_for_product` is a no-op if the product already has lots. `import-order` always attempts seeding, so re-running it recovers from a failed Scryfall fetch.

**Web UI** (`mtgtracker/web/`): FastAPI + Jinja2, server-rendered, with no JS build. It is read-only except for `/printings`. It is one CSS file with light/dark tokens. The file is linked as `style.css?v=<content hash>` via `static_version()`, and `/static` responses get `immutable` when versioned and `no-cache` otherwise. Reference any new static file the same way, or browsers will keep a stale copy. `create_app(db_path)` runs migrations once. Each request opens its own connection with `check_same_thread=False`, because FastAPI's threadpool may create it on one thread and use it on another. Never share a connection. Page-only read SQL lives in `web/queries.py`. Money renders only through the `money` Jinja filter (`format_cents`, integer math). Never serve `receipts/`, since those files contain credentials. Page loads never call the Scryfall API. The only network fetch is the explicit "look up set" POST, which goes through the cached `fetch_set_cards`. Card images are hotlinked from Scryfall's image CDN (intentional). They're looked up through `mtgtracker/cardinfo.py`, which indexes `scryfall.cached_cards()`, a parse of every cached set that's memoized until the cache files change. Pinned lots show their exact printing. Unpinned lots show a representative printing flagged `exact=False`: one that matches the lot's finish and isn't a promo. `cardinfo` also sorts cards into deck sections by type line and supplies mana value (`cmc`). Deck grids show a Commander section first, using `decks.commander` (added in migration 0002 and set by seeding), then the type sections. Deck and card lists order by mana value, highest first. Among equal mana values, colored mana outranks colorless: more distinct colors, then more colored symbols ({1}{U}{R} > {1}{R}{R} > {3}; `cardinfo.color_weight`). Name breaks remaining ties. Decks and cards default to an image grid, and `?view=list` gives the table. Tiles have no captions. A card that isn't physically in the deck (or has been disposed of, on `/cards`) is greyed out with the `.away` class. Unpinned non-basics get a "needs printing" flag.

**Deck breakdown and list view:** the deck page opens with a breakdown built by `web/breakdown.py` from the display groups: a mana curve of non-land cards, mana colors (each non-land card counted once, with Multicolor and Colorless buckets), and card types. It's plain HTML with no chart library. Chart colors are CSS tokens: `--series-N` are the dataviz skill's validated categorical slots, fixed per card type in `TYPE_SLOTS`, and `--mana-*` map those hues to mana meaning. Both were checked with the dataviz `validate_palette.js`; re-run it if you change them. Legends always show counts, so color never carries meaning alone. The list view (`?view=list`) is two CSS columns of type sections, each row showing qty, name, and cost via the `mana_symbols` Jinja filter. Rows carry `data-card` for the panel.

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

**Card size:** the grid's `--tile-min` (190px) is the smallest readable card width. The grid drops a column rather than shrinking cards: 5 across when the results area is 1008px, 4 at 1600px with the panel showing. Phones use 140px for 2 columns.

**Card details panel:** on wide screens with a mouse (`(hover: hover) and (min-width: 1480px)`), hovering or focusing a card shows a sticky detail panel on the right. Everywhere else, including phones, tablets, and narrow windows, tapping a card or pressing Enter/Space on it opens the same content in a modal `<dialog>` sheet (`#card-dialog`, a bottom sheet on phones). Taps on links and controls inside a card keep their normal behavior. Both are driven by `static/card-panel.js`, whose `render()` builds the content for either. In both, the Decks row lists every deck whose list has the card, as `1× Deck` links. When there's more than one deck, the one that holds the card is labeled current (`queries._decks_for`, merged by `_merge_decks` on `/cards`). The sheet animates: it slides up and down on phones, and lifts and fades on wider screens. The open animation is pure CSS. Closing adds `.closing` and waits for `animationend` before `dialog.close()`, and this covers Escape (`cancel`), backdrop taps, and swipe-down. There's no close button. On phones, dragging the sheet down once its content is scrolled to the top moves it with the finger. Release past about a third of its height, or flick faster than 0.5 px/ms, to close; otherwise it snaps back. Swiping sideways steps to the next or previous card, and so do the arrow keys in the sheet. The order is the page's `.tile[data-card]` or `.deck-row[data-card]` elements, so it follows the current sort and filters, and a position counter shows where you are. The first ~8px of movement decides between sideways and vertical. At either end of the list the sheet resists and springs back. It's instant under `prefers-reduced-motion`. When the pop-up has a card image, it hides the first face's repeated name, type, and rules text via CSS. Placeholder cards and double-faced backs keep their text, and the desktop panel always shows it. Every tile and list row carries `data-card='<json>'`, built by the `panel_json` macro in `_card_tiles.html` from `CardInfo` (faces, rarity, set, artist) plus the row's location and cost. The script builds DOM nodes with `textContent`, never `innerHTML`, and turns `{X}` into Scryfall mana-symbol SVGs. The panel's image slot has a fixed aspect ratio so the text doesn't jump while images load. Pages opt in with `{% block body_class %}with-panel{% endblock %}` plus the `panel` block.

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
