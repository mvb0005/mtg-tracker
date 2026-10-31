-- A person's handle (mtgtracker/play invites name people by it). Optional, and unique ignoring case;
-- users.clean_username keeps it lowercase.
ALTER TABLE users ADD COLUMN username TEXT COLLATE NOCASE;
CREATE UNIQUE INDEX users_username ON users (username) WHERE username IS NOT NULL;
