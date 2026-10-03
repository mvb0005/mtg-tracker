-- Cards a deck's list includes that you don't own yet ("to acquire").
--
-- A deck's list is its deck_cards rows (physical copies you own, equipped
-- here or elsewhere) plus its deck_wants rows. Wants are never lots: they
-- have no transaction or cost basis and never appear in the collection --
-- one row per wanted copy, mirroring one lot per physical card.
CREATE TABLE deck_wants (
    id           INTEGER PRIMARY KEY,
    deck_id      INTEGER NOT NULL REFERENCES decks(id),
    card_name    TEXT NOT NULL,
    scryfall_id  TEXT,
    created_at   TEXT NOT NULL
) STRICT;

CREATE INDEX deck_wants_by_deck ON deck_wants(deck_id);

-- Version snapshots include the wanted cards too.
CREATE TABLE deck_version_wants (
    version_id   INTEGER NOT NULL REFERENCES deck_versions(id),
    card_name    TEXT NOT NULL,
    scryfall_id  TEXT
) STRICT;

CREATE INDEX deck_version_wants_by_version ON deck_version_wants(version_id);
