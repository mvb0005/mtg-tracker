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
.venv/bin/mtg serve [--db PATH] [--port 8000] [--reload]      # read-only web UI, binds 127.0.0.1
python scripts/scrub_order.py <in.order> <out.order>         # make a real .order safe to commit as a fixture
deploy/minikube.sh up | load-db | import FILE.order | forward | down | destroy   # minikube (see below)
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

**Web UI** (`mtgtracker/web/`): FastAPI + Jinja2, server-rendered, read-only, with no JS build. It is one CSS file with light/dark tokens. `create_app(db_path)` runs migrations once. Each request opens its own connection with `check_same_thread=False`, because FastAPI's threadpool may create it on one thread and use it on another. Never share a connection. All SQL lives in `web/queries.py`. Money renders only through the `money` Jinja filter (`format_cents`, integer math). Never serve `receipts/`, since those files contain credentials. No Scryfall calls or image hotlinks in the request path. `tests/test_web.py` seeds one file-backed template DB per module and copies it for each test. A fresh seed per test takes seconds on WSL because of per-card commits.

**Scryfall:** `scryfall.py` fetches one whole set through the search API and caches it in `.scryfall_cache/<set>.json`, which is gitignored. It never makes per-card calls. Tests must stay network-free: `test_precon_decklist.py` monkeypatches `scryfall.CACHE_DIR` to a temp dir seeded from `tests/fixtures/scryfall_fdc.json`.

**Seed decklists** (`seed/precon_decklists/*.json`) have the keys `deck_name`, `product_display_name` (must exactly match the cleaned product title), `format`, `commander`, `set_code`, `cards` (unique non-basic names), and `basics` (name → count). `cards` plus the `basics` counts must total 100. A test enforces this.

## minikube

`Dockerfile` plus `deploy/k8s/` (kustomize), driven by `deploy/minikube.sh`.
- **Image:** `pip install -e .` at `/app` on purpose. `SEED_DIR` and `scryfall.CACHE_DIR` resolve relative to the source tree, so a site-packages install would break seeding. The image is non-root, with a read-only root filesystem.
- **Storage:** one PVC at `/data` holds the DB, the receipts, and the Scryfall cache (mounted into `/app/.scryfall_cache` via `subPath`).
- **Deployment:** `replicas: 1`, `strategy: Recreate`, because SQLite sits on an RWO volume. The image tag is `mtgtracker:dev` with `imagePullPolicy: Never`, built by `minikube image build`. `up` does a `rollout restart` to pick up a rebuild.
- **Access:** the Service is ClusterIP only, reached with `forward`, which binds 127.0.0.1. There is no NodePort or Ingress.
- **Getting data in:** `load-db` takes an SQLite online-backup snapshot and copies it in under a temp name before an atomic `mv`. Never copy the live file.
- **Keep data out of the image:** `.dockerignore` must keep excluding `data/`, `receipts/`, `.claude/`, and `tests/`. The scrubbed fixture still contains a card's last four digits.

## Sensitive data

Real `.order` files contain auth tokens and PII. `receipts/*` and `data/*.db` are gitignored. Only scrubbed fixtures (via `scripts/scrub_order.py`) belong in `tests/fixtures/`.
