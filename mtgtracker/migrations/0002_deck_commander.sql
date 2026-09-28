-- The deck's commander, by card name (NULL for non-Commander decks or
-- decks without one). Precon seeding fills it in from the seed decklist's
-- "commander"; this backfills the two precon decks seeded before the column
-- existed (values from seed/precon_decklists/*.json).
ALTER TABLE decks ADD COLUMN commander TEXT;

UPDATE decks SET commander = 'Lathliss, Dragon Queen'
WHERE name = 'Reign of Dragons' AND commander IS NULL;

UPDATE decks SET commander = 'Sai, Master Thopterist'
WHERE name = 'Keen Engineering' AND commander IS NULL;
