-- Deck versioning and planned modifications.
--
-- decks.version is the deck's current version (0 = never recorded, e.g. a
-- deck made with a bare create_deck). Every recorded version keeps a full
-- snapshot of the deck's list (one row per lot), so any past list can be
-- shown and diffs are computed from consecutive snapshots.
--
-- deck_changes is the modification list: cards to insert into a deck, each
-- optionally paired with a card to replace (cut). add_lot_id NULL means the
-- card is planned (not owned yet; add_scryfall_id identifies the card).
-- Applying the ready changes records the next version; applied rows keep
-- the version they went into.
ALTER TABLE decks ADD COLUMN version INTEGER NOT NULL DEFAULT 0;

CREATE TABLE deck_versions (
    id          INTEGER PRIMARY KEY,
    deck_id     INTEGER NOT NULL REFERENCES decks(id),
    version     INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    note        TEXT,
    UNIQUE (deck_id, version)
) STRICT;

CREATE TABLE deck_version_cards (
    version_id  INTEGER NOT NULL REFERENCES deck_versions(id),
    lot_id      INTEGER NOT NULL REFERENCES lots(id),
    PRIMARY KEY (version_id, lot_id)
) STRICT;

CREATE TABLE deck_changes (
    id               INTEGER PRIMARY KEY,
    deck_id          INTEGER NOT NULL REFERENCES decks(id),
    add_card_name    TEXT NOT NULL,
    add_lot_id       INTEGER REFERENCES lots(id),
    add_scryfall_id  TEXT,
    cut_lot_id       INTEGER REFERENCES lots(id),
    created_at       TEXT NOT NULL,
    applied_version  INTEGER
) STRICT;

CREATE INDEX deck_changes_by_deck ON deck_changes(deck_id, applied_version);

-- Existing decks become version 1, snapshotting their current list.
UPDATE decks SET version = 1;

INSERT INTO deck_versions (deck_id, version, created_at, note)
SELECT id, 1, created_at, 'Initial list' FROM decks;

INSERT INTO deck_version_cards (version_id, lot_id)
SELECT deck_versions.id, deck_cards.lot_id
FROM deck_cards
JOIN deck_versions ON deck_versions.deck_id = deck_cards.deck_id AND deck_versions.version = 1;
