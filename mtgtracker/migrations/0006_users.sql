-- Multiple users. Every card (lot), deck and purchase now has an owner;
-- everything that existed before belongs to user 1 (the original owner,
-- an admin). A card can now exist without a recorded purchase (e.g. a
-- collection CSV import), so lots.transaction_id becomes nullable.
--
-- lots is rebuilt (SQLite can't drop a NOT NULL) using the documented
-- procedure: foreign keys off, new table, copy, drop, rename, check. Tables
-- that reference lots (deck_cards, dispositions, deck_version_cards,
-- deck_changes) refer to it by name, so they follow the renamed table.
PRAGMA foreign_keys = OFF;

BEGIN;

CREATE TABLE users (
    id            INTEGER PRIMARY KEY,
    email         TEXT UNIQUE COLLATE NOCASE,
    display_name  TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT  -- last sign-in; NULL = never signed in
) STRICT;

-- The original owner; their email is bound on first sign-in (MTG_OWNER_EMAIL).
INSERT INTO users (id, email, display_name, is_admin, created_at)
VALUES (1, NULL, 'Owner', 1, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));

ALTER TABLE decks ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id);
ALTER TABLE transactions ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id);

CREATE TABLE lots_new (
    id                    INTEGER PRIMARY KEY,
    owner_id              INTEGER NOT NULL DEFAULT 1 REFERENCES users(id),
    product_id            INTEGER REFERENCES products(id),
    transaction_id        INTEGER REFERENCES transactions(id),
    scryfall_id           TEXT,
    card_name             TEXT NOT NULL,
    set_code              TEXT,
    collector_number      TEXT,
    finish                TEXT NOT NULL DEFAULT 'nonfoil' CHECK (finish IN ('nonfoil', 'foil', 'etched')),
    allocated_cost_cents  INTEGER,
    acquired_date         TEXT NOT NULL,
    notes                 TEXT
) STRICT;

INSERT INTO lots_new (id, owner_id, product_id, transaction_id, scryfall_id, card_name, set_code,
                      collector_number, finish, allocated_cost_cents, acquired_date, notes)
SELECT id, 1, product_id, transaction_id, scryfall_id, card_name, set_code,
       collector_number, finish, allocated_cost_cents, acquired_date, notes
FROM lots;

DROP TABLE lots;
ALTER TABLE lots_new RENAME TO lots;

CREATE INDEX lots_by_owner ON lots(owner_id);
CREATE INDEX decks_by_owner ON decks(owner_id);

COMMIT;

PRAGMA foreign_keys = ON;
