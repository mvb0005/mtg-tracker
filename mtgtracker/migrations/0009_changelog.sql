-- The write log. Additive only. The triggers that fill `changelog` are not
-- in this file: db.migrate() generates them (cl_<table>_<i|u|d>) from each
-- table's columns, so they follow later schema changes by themselves.
--
-- op: I insert, U update, D delete, S schema (a migration ran: after_json is
-- {"user_version": N}). before_json/after_json hold every column of the row.
-- batch_id is the audit_log id of the web request that made the change
-- (NULL for the CLI and scripts); no foreign key, so log rows outlive anything.
CREATE TABLE changelog (
    seq         INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,
    batch_id    INTEGER,
    tbl         TEXT NOT NULL,
    op          TEXT NOT NULL CHECK (op IN ('I', 'U', 'D', 'S')),
    row_id      INTEGER,
    before_json TEXT,
    after_json  TEXT
) STRICT;

CREATE INDEX changelog_by_batch ON changelog(batch_id) WHERE batch_id IS NOT NULL;
CREATE INDEX changelog_by_row ON changelog(tbl, row_id);

-- One row, read by the triggers: the batch id of the transaction in progress.
-- A real table because a trigger in the main schema can't read TEMP objects.
CREATE TABLE changelog_context (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    batch_id INTEGER
) STRICT;
INSERT INTO changelog_context (id, batch_id) VALUES (1, NULL);
