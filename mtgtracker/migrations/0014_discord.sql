-- A person's Discord account (mtgtracker/discord). Unused: the Activity knows players by their Discord id
-- alone (no linking); kept because this migration has run. Unique, so one Discord account is one person.
ALTER TABLE users ADD COLUMN discord_id TEXT;
CREATE UNIQUE INDEX users_discord_id ON users (discord_id) WHERE discord_id IS NOT NULL;
