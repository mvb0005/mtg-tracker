-- Core schema: transactions -> products -> lots, plus decks/deck_cards
-- for physical card placement, and supporting tables for receipts,
-- dispositions, and the local Scryfall cache.

CREATE TABLE transactions (
    id                     INTEGER PRIMARY KEY,
    order_type_identifier  TEXT,
    order_identifier       TEXT,
    order_number           TEXT,
    vendor                 TEXT NOT NULL,
    status                 TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    subtotal_cents         INTEGER NOT NULL,
    tax_cents              INTEGER NOT NULL,
    shipping_cents         INTEGER NOT NULL DEFAULT 0,
    total_cents            INTEGER NOT NULL,
    currency               TEXT NOT NULL DEFAULT 'USD',
    payment_method         TEXT,
    source                 TEXT NOT NULL CHECK (source IN ('wallet_order', 'manual')),
    UNIQUE (order_type_identifier, order_identifier)
) STRICT;

CREATE TABLE products (
    id                    INTEGER PRIMARY KEY,
    transaction_id        INTEGER NOT NULL REFERENCES transactions(id),
    sku                   TEXT,
    title                 TEXT NOT NULL,
    display_name          TEXT NOT NULL,
    qty                   INTEGER NOT NULL DEFAULT 1,
    unit_price_cents      INTEGER NOT NULL,
    line_subtotal_cents   INTEGER NOT NULL,
    tax_allocated_cents   INTEGER NOT NULL DEFAULT 0,
    cost_basis_cents      INTEGER NOT NULL,
    product_type          TEXT
) STRICT;

CREATE TABLE lots (
    id                    INTEGER PRIMARY KEY,
    product_id            INTEGER REFERENCES products(id),
    transaction_id        INTEGER NOT NULL REFERENCES transactions(id),
    scryfall_id           TEXT,
    card_name             TEXT NOT NULL,
    set_code              TEXT,
    collector_number      TEXT,
    finish                TEXT NOT NULL DEFAULT 'nonfoil' CHECK (finish IN ('nonfoil', 'foil', 'etched')),
    allocated_cost_cents  INTEGER,
    acquired_date         TEXT NOT NULL,
    notes                 TEXT
) STRICT;

CREATE TABLE decks (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    format      TEXT NOT NULL DEFAULT 'Commander',
    created_at  TEXT NOT NULL,
    notes       TEXT
) STRICT;

-- (deck_id, lot_id) row = "this physical card is on this deck's list".
-- `equipped` marks which one deck currently physically holds it; the
-- partial unique index below enforces that at the database level.
CREATE TABLE deck_cards (
    deck_id   INTEGER NOT NULL REFERENCES decks(id),
    lot_id    INTEGER NOT NULL REFERENCES lots(id),
    equipped  INTEGER NOT NULL DEFAULT 0 CHECK (equipped IN (0, 1)),
    PRIMARY KEY (deck_id, lot_id)
) STRICT;

CREATE UNIQUE INDEX one_equipped_per_lot ON deck_cards(lot_id) WHERE equipped = 1;

CREATE TABLE deck_costs (
    id              INTEGER PRIMARY KEY,
    deck_id         INTEGER REFERENCES decks(id),
    transaction_id  INTEGER REFERENCES transactions(id),
    description     TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL,
    acquired_date   TEXT NOT NULL
) STRICT;

CREATE TABLE dispositions (
    id                INTEGER PRIMARY KEY,
    lot_id            INTEGER NOT NULL UNIQUE REFERENCES lots(id),
    disposed_at       TEXT NOT NULL,
    disposition_type  TEXT NOT NULL CHECK (disposition_type IN ('sold', 'traded', 'lost', 'other')),
    proceeds_cents    INTEGER NOT NULL DEFAULT 0,
    notes             TEXT
) STRICT;

CREATE TABLE receipts (
    id                 INTEGER PRIMARY KEY,
    transaction_id     INTEGER REFERENCES transactions(id),
    sha256             TEXT NOT NULL UNIQUE,
    original_filename  TEXT NOT NULL,
    mime_type          TEXT NOT NULL,
    file_ext           TEXT NOT NULL,
    imported_at        TEXT NOT NULL,
    kind               TEXT NOT NULL CHECK (kind IN ('wallet_order', 'pdf', 'email', 'photo'))
) STRICT;

CREATE TABLE scryfall_cards (
    scryfall_id       TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    set_code          TEXT NOT NULL,
    collector_number  TEXT NOT NULL,
    finishes          TEXT NOT NULL,
    last_synced_at    TEXT NOT NULL
) STRICT;
