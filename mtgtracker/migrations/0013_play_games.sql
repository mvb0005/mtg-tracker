-- Games on the Forge game server (mtgtracker/play). The play page writes a row when it starts a match;
-- the game server's result completes it, matched by its match_id (idempotent: a result arrives once
-- per match however often it is sent). deck_id has no foreign key, so a deleted deck keeps its games.
-- ticket is the host's seat ticket, kept so they can rejoin the table from the play page.
CREATE TABLE play_games (
    id          INTEGER PRIMARY KEY,
    match_id    TEXT NOT NULL UNIQUE,
    host_id     INTEGER NOT NULL REFERENCES users (id),
    format      TEXT NOT NULL,
    deck_id     INTEGER,
    deck_name   TEXT NOT NULL,
    opponents   TEXT NOT NULL,  -- JSON: [{"name", "deck"}] in seat order
    ticket      TEXT,
    status      TEXT NOT NULL CHECK (status IN ('playing', 'finished', 'ended', 'abandoned')),
    winner      TEXT,
    turn        INTEGER,
    summary     TEXT,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    log         TEXT            -- JSON: the game log's lines, oldest first
) STRICT;
CREATE INDEX play_games_host ON play_games (host_id, started_at);
