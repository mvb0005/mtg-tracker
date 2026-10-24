-- Suggested cards for a deck, to compare before changing anything. A
-- suggestion is not on the deck's list: it isn't a want, a lot or a change.
-- Accepting one queues a planned deck_changes row (change_id, deliberately
-- without a foreign key: removing that change from the Modify page makes
-- the suggestion open again). Dismissed rows stay as "don't suggest this
-- again". cut_card_name is the card on the list it would replace, if any;
-- price_cents is the suggester's snapshot (cheapest paper USD), for display.
CREATE TABLE deck_suggestions (
    id             INTEGER PRIMARY KEY,
    deck_id        INTEGER NOT NULL REFERENCES decks(id),
    card_name      TEXT NOT NULL,
    scryfall_id    TEXT,
    cut_card_name  TEXT,
    theme          TEXT,
    reason         TEXT,
    price_cents    INTEGER CHECK (price_cents IS NULL OR price_cents >= 0),
    suggested_by   INTEGER REFERENCES users(id),
    status         TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'accepted', 'dismissed')),
    change_id      INTEGER,
    created_at     TEXT NOT NULL,
    decided_at     TEXT
) STRICT;

CREATE INDEX deck_suggestions_by_deck ON deck_suggestions(deck_id, status);
