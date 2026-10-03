-- Collection imports: cards added from a collection export (ManaBox,
-- Moxfield, Archidekt or a plain name/quantity CSV) rather than a recorded
-- purchase. Each file is imported once per user (by its sha256), and every
-- lot it created points back at it, so an import can be traced or undone.
CREATE TABLE collection_imports (
    id                 INTEGER PRIMARY KEY,
    owner_id           INTEGER NOT NULL REFERENCES users(id),
    sha256             TEXT NOT NULL,
    original_filename  TEXT,
    format             TEXT NOT NULL,
    card_count         INTEGER NOT NULL,
    imported_by        INTEGER NOT NULL REFERENCES users(id),
    imported_at        TEXT NOT NULL,
    UNIQUE (owner_id, sha256)
) STRICT;

ALTER TABLE lots ADD COLUMN collection_import_id INTEGER REFERENCES collection_imports(id);
