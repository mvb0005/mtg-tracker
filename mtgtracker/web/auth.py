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


class NotSignedIn(Exception):
    """No acceptable identity on the request."""


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

    def email(self, token: str) -> str:
        try:
            key = self.jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token, key, algorithms=["RS256"], audience=self.config.audience, issuer=self.issuer,
                options={"require": ["exp", "aud", "iss"]},
            )
        except jwt.PyJWTError as exc:
            raise NotSignedIn(f"invalid Access token: {exc}") from exc
        email = claims.get("email")
        if not email:
            raise NotSignedIn("Access token has no email")
        return email


def resolve(conn: sqlite3.Connection, config: AuthConfig, verifier: AccessVerifier | None,
            *, client_host: str | None, headers, cookies) -> User:
    """The user for a request, or NotSignedIn."""
    if config.mode == "single":
        return users.get(conn, OWNER_ID)
    if config.mode == "dev":
        raw = cookies.get(DEV_COOKIE, "")
        user = users.get(conn, int(raw)) if raw.isdigit() else None
        return user or users.get(conn, OWNER_ID)
    # cloudflare
    if client_host in LOOPBACK:
        return users.get(conn, OWNER_ID)
    token = headers.get("cf-access-jwt-assertion") or cookies.get("CF_Authorization")
    if not token:
        raise NotSignedIn("sign in through mtg.example.com")
    email = verifier.email(token)
    user = users.by_email(conn, email)
    if user is None and config.owner_email and email.casefold() == config.owner_email.casefold():
        user = users.claim_owner(conn, email)
    if user is None:
        raise NotSignedIn(f"{email} isn't a user here yet; ask the owner to add you")
    users.touch(conn, user.id)
    return user
