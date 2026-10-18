"""Who is making a request. Set by MTG_AUTH:

- single (default): everyone is user 1 -- the original single-user app.
- dev: local testing of several users; `/dev/as/<id>` sets a cookie.
  Never enable on a public deployment.
- cloudflare: the site sits behind Cloudflare Access. Each request carries
  Access's signed JWT (Cf-Access-Jwt-Assertion header, or the
  CF_Authorization cookie), verified against the team's published keys
  (RS256; aud = CF_ACCESS_AUD, iss = https://CF_ACCESS_TEAM_DOMAIN, exp).
  Its email must belong to an existing user (an allowlist; MTG_OWNER_EMAIL
  claims user 1 on first sign-in). Requests from loopback act as user 1:
  only `kubectl port-forward` reaches the pod that way (cloudflared runs in
  its own pod, and uvicorn ignores X-Forwarded-For).

Every request has a principal (whose things it touches; request.state.user,
which all owner guards key on) and an actor (who is really asking;
request.state.actor). For a person they are the same. A system user (an
automated actor, authenticated by a Cloudflare Access service token whose
Client ID is a registered credential) must say who it acts for with
X-Act-For; the principal is then that person. A person sending X-Act-For is
refused. In dev mode, `X-Dev-Service: <client id>` stands in for the service
token so this can be tried locally; no other mode reads it.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

import jwt

from mtgtracker import users
from mtgtracker.users import OWNER_ID, User

LOOPBACK = {"127.0.0.1", "::1"}
DEV_COOKIE = "mtg_dev_user"
DEV_SERVICE_HEADER = "x-dev-service"
ACT_FOR_HEADER = "x-act-for"


class NotSignedIn(Exception):
    """No acceptable identity on the request."""


class ActForError(Exception):
    """X-Act-For is missing, malformed, or names someone the actor can't act for."""

    def __init__(self, status: int, code: str, message: str, *, as_json: bool):
        super().__init__(message)
        self.status, self.code, self.message, self.as_json = status, code, message, as_json


@dataclass(frozen=True)
class AccessIdentity:
    """What a verified Access token says. Exactly one of the two is set."""
    email: str | None = None  # a person
    common_name: str | None = None  # a service token's Client ID


@dataclass(frozen=True)
class AuthConfig:
    mode: str = "single"
    team_domain: str | None = None
    audience: str | None = None
    owner_email: str | None = None

    @classmethod
    def from_env(cls) -> AuthConfig:
        mode = os.environ.get("MTG_AUTH", "single").strip().lower() or "single"
        if mode not in ("single", "dev", "cloudflare"):
            raise ValueError(f"MTG_AUTH must be single, dev or cloudflare, not {mode!r}")
        config = cls(
            mode=mode,
            team_domain=(os.environ.get("CF_ACCESS_TEAM_DOMAIN") or "").strip() or None,
            audience=(os.environ.get("CF_ACCESS_AUD") or "").strip() or None,
            owner_email=(os.environ.get("MTG_OWNER_EMAIL") or "").strip() or None,
        )
        if mode == "cloudflare" and not (config.team_domain and config.audience):
            raise ValueError("MTG_AUTH=cloudflare needs CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD")
        return config


class AccessVerifier:
    """Verifies Cloudflare Access JWTs. The key set is cached and refetched
    when a token names an unknown key (PyJWKClient does both)."""

    def __init__(self, config: AuthConfig, jwks_client=None):
        self.config = config
        self.issuer = f"https://{config.team_domain}"
        self.jwks = jwks_client or jwt.PyJWKClient(
            f"{self.issuer}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600
        )

    def verify(self, token: str) -> AccessIdentity:
        """Check the token (RS256 signature, aud, iss, exp) and say who it
        is: an email for a person, or the `common_name` (the Client ID) for
        a service token, whose JWT has no email and an empty sub."""
        try:
            key = self.jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token, key, algorithms=["RS256"], audience=self.config.audience, issuer=self.issuer,
                options={"require": ["exp", "aud", "iss"]},
            )
        except jwt.PyJWTError as exc:
            raise NotSignedIn(f"invalid Access token: {exc}") from exc
        email, common_name = claims.get("email"), claims.get("common_name")
        # Email wins: a token that names a person is that person, so a
        # common_name riding along can never turn a person into (or let them
        # borrow) a service identity, nor the reverse.
        if isinstance(email, str) and email.strip():
            return AccessIdentity(email=email.strip())
        if isinstance(common_name, str) and common_name.strip():
            return AccessIdentity(common_name=common_name.strip())
        raise NotSignedIn("Access token has neither an email nor a service token")


def _person(conn: sqlite3.Connection, config: AuthConfig, email: str) -> User:
    user = users.by_email(conn, email)
    if user is None and config.owner_email and email.casefold() == config.owner_email.casefold():
        user = users.claim_owner(conn, email)
    if user is None:
        raise NotSignedIn(f"{email} isn't a user here yet; ask the owner to add you")
    if user.disabled:
        raise NotSignedIn(f"{user.display_name}'s access has been turned off")
    users.touch(conn, user.id)
    return user


def _service(conn: sqlite3.Connection, client_id: str) -> User:
    user = users.credential_user(conn, client_id)
    if user is None:
        raise NotSignedIn("unknown, revoked or disabled service credential")
    return user


def _principal_for(conn: sqlite3.Connection, actor: User, act_for: str | None) -> User:
    """Who the request acts for: the actor itself for a person; the
    X-Act-For user for a system actor (carte blanche over any enabled
    person -- the owner guards then apply to that person)."""
    if actor.kind != "system":
        if act_for is not None:
            raise ActForError(403, "forbidden", "X-Act-For is for system users", as_json=False)
        return actor
    if act_for is None:
        raise ActForError(
            400, "act_for_required", "system requests must say who they act for (X-Act-For)", as_json=True
        )
    valid = act_for.isascii() and act_for.isdecimal() and len(act_for) <= 12  # no overflowing ids
    target = users.get(conn, int(act_for)) if valid else None
    if target is None:
        raise ActForError(400, "bad_act_for", "X-Act-For must be the id of an existing user", as_json=True)
    if target.kind != "person":
        raise ActForError(403, "forbidden", "a system user can't act for another system user", as_json=True)
    if target.disabled:
        raise ActForError(403, "forbidden", f"{target.display_name} is disabled", as_json=True)
    return target


def resolve(conn: sqlite3.Connection, config: AuthConfig, verifier: AccessVerifier | None,
            *, client_host: str | None, headers, cookies) -> tuple[User, User]:
    """(principal, actor) for a request, or raise NotSignedIn / ActForError."""
    actor: User | None = None
    if config.mode == "dev" and headers.get(DEV_SERVICE_HEADER) is not None:
        actor = _service(conn, headers.get(DEV_SERVICE_HEADER).strip())
    elif config.mode == "single":
        actor = users.get(conn, OWNER_ID)
    elif config.mode == "dev":
        raw = cookies.get(DEV_COOKIE, "")
        user = users.get(conn, int(raw)) if raw.isdigit() else None
        if user is None or user.kind != "person":
            user = users.get(conn, OWNER_ID)
        actor = user
    elif client_host in LOOPBACK:  # cloudflare
        actor = users.get(conn, OWNER_ID)
    else:
        token = headers.get("cf-access-jwt-assertion") or cookies.get("CF_Authorization")
        if not token:
            raise NotSignedIn("sign in through mtg.example.com")
        identity = verifier.verify(token)
        if identity.email:
            actor = _person(conn, config, identity.email)
        else:
            actor = _service(conn, identity.common_name)
    if actor.disabled:
        raise NotSignedIn(f"{actor.display_name}'s access has been turned off")
    return _principal_for(conn, actor, headers.get(ACT_FOR_HEADER)), actor
