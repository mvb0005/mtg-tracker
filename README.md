# mtgtracker

Personal Magic: The Gathering collection and spending tracker. Local-first,
single-user SQLite database — the source of truth for money and ownership,
not a deckbuilder (that's Moxfield/Archidekt) or a scanner (that's ManaBox).

See `/home/mvb/.claude/plans/sequential-frolicking-grove.md` for the full
design writeup (data model, schema, and the reasoning behind it).

## Setup

```
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Usage

```
mtg import-order path/to/Order.order   # imports the purchase, seeds known precon decks
mtg list-decks
mtg reassemble-deck <deck-id>          # what to pull back to reassemble a deck's list
```

## Tests

```
.venv/bin/pytest
```

## Data model, in short

- `transactions` → `products` (sealed line items) → `lots` (one row per
  physical card — no collapsed quantities, so a card can't be equipped to
  two decks at once).
- `deck_cards` is a many-to-many junction: a `(deck_id, lot_id)` row means
  "this card is on this deck's list." An `equipped` flag (enforced unique
  per lot by a partial index) marks which one deck physically holds it
  right now. Borrowing a card into another deck just moves `equipped`;
  cutting it from a deck's list deletes the row outright. `reassemble`
  reports every card on a deck's list that isn't currently equipped there.
- Cost basis is allocated evenly by card count across a product's lots
  (largest-remainder rounding, so cents always reconcile), not by market
  value — there's no live pricing in v1.

## What's not built yet

Live/nightly price snapshots, ManaBox CSV import, Moxfield/Archidekt
export, `deck_costs` entries (sleeves/playmat), and the web UI are all on
the roadmap but out of this round's scope — see the plan doc.

## Known gaps from the first real import

Importing the two Foundations Commander precons (`mtg import-order`)
surfaces a few cards Scryfall can't resolve to a single printing without
the physical box in hand:

- **Sol Ring** and **Arcane Signet** each have two `fdc`-set printings (an
  original-wave foil-only version and a newer nonfoil/foil version) — one
  lot per copy was created with `scryfall_id` left `NULL` rather than
  guessed.
- **Dragonstorm** (in Reign of Dragons) has no `fdc` printing at all — the
  physical copy in the box must be a reprint from elsewhere, so its lot
  also has `scryfall_id = NULL`.
- Basic lands (32 Mountain, 34 Island) are never resolved to a specific art
  — there's no way to know which printing shipped in the box.

None of this blocks cost allocation (it's by count, not by card), only
future Scryfall-id-dependent features. Re-run resolution later if you want
to pin these down by hand.
