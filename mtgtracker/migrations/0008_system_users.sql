-- System users, service credentials and the audit log. Additive only
-- (rolling updates run the old code on this schema for a few seconds).
--
-- A system user is an automated actor (Claude) that acts for people. It is
-- never a person: no email, never an admin, never signs in by email. It
-- authenticates with a Cloudflare Access service token, matched by that
-- token's Client ID (an identifier, not a secret -- the secret never
-- reaches this database).
ALTER TABLE users ADD COLUMN kind TEXT NOT NULL DEFAULT 'person' CHECK (kind IN ('person', 'system'));
ALTER TABLE users ADD COLUMN disabled_at TEXT;  -- NULL = enabled

-- Defense in depth beside users.py: a system user can't be given an email
-- (the sign-in allowlist is by email) or admin rights.
CREATE TRIGGER users_system_no_email_insert BEFORE INSERT ON users
WHEN NEW.kind = 'system' AND (NEW.email IS NOT NULL OR NEW.is_admin = 1)
BEGIN SELECT RAISE(ABORT, 'a system user has no email and is never an admin'); END;

CREATE TRIGGER users_system_no_email_update BEFORE UPDATE ON users
WHEN NEW.kind = 'system' AND (NEW.email IS NOT NULL OR NEW.is_admin = 1)
BEGIN SELECT RAISE(ABORT, 'a system user has no email and is never an admin'); END;

CREATE TABLE service_credentials (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    cf_client_id  TEXT NOT NULL UNIQUE,  -- the service token's Client ID
    label         TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    revoked_at    TEXT                   -- NULL = usable
) STRICT;

-- One row per successful write request. actor_id is who made the request,
-- principal_id whose things it changed (the same user for a person).
CREATE TABLE audit_log (
    id            INTEGER PRIMARY KEY,
    at            TEXT NOT NULL,
    actor_id      INTEGER NOT NULL REFERENCES users(id),
    principal_id  INTEGER NOT NULL REFERENCES users(id),
    method        TEXT NOT NULL,
    path          TEXT NOT NULL,
    action        TEXT NOT NULL,         -- the endpoint function
    target        TEXT,                  -- path params, as JSON
    summary_json  TEXT,                  -- {"notice": ..} and/or the confirmed preview
    restore_point TEXT                   -- reserved: set by the restore-point phase
) STRICT;

CREATE INDEX audit_log_by_principal ON audit_log(principal_id, at);
