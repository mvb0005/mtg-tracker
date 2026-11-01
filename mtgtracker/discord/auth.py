"""Signing in through Discord for the Activity.

The Activity asks Discord to authorize this application (scopes `identify` and `guilds.members.read`) and
sends the code here. The code is exchanged with the application's secret, so a request that didn't come
through Discord has nothing to exchange; the user is whoever Discord says the token belongs to, and must be a
member of DISCORD_GUILD_ID. The page then gets a session token (signed, short-lived, sent as a bearer header:
cookies are unreliable in Discord's iframe) and Discord's access token for the SDK's `authenticate`.

Players are known by their Discord id (D-D2, Matt): no tracker account needed. For now everyone plays from the
tracker owner's decks, and games are recorded under the owner.

Configured from the environment: DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, DISCORD_GUILD_ID. Without all
three the Activity says Discord isn't set up.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
from dataclasses import dataclass

import httpx

from mtgtracker import users
from mtgtracker.services import ServiceError, UpstreamError

API = "https://discord.com/api/v10"
SCOPES = ("identify", "guilds.members.read")  # the table also asks for rpc.voice.read, to show who is talking
CDN = "https://cdn.discordapp.com"
SESSION_SECONDS = 12 * 3600
MAX_CODE = 512
# For everyone together: every request arrives through Discord's proxy and the tunnel.
EXCHANGES_PER_MINUTE = 30
TIMEOUT = 10.0


class NotMember(ServiceError):
    """Signed in to Discord, but not in the tracker's Discord server."""


@dataclass(frozen=True)
class Discord:
    client_id: str
    client_secret: str
    guild_id: str

    @classmethod
    def from_env(cls) -> Discord | None:
        keys = ("DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_GUILD_ID")
        values = [os.environ.get(k, "").strip() for k in keys]
        return cls(*values) if all(values) else None

    def _call(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                return client.request(method, API + path, **kwargs)
        except httpx.HTTPError as exc:
            raise UpstreamError("Discord isn't answering") from exc

    def exchange(self, code: str) -> str:
        """Discord's access token for an authorize code from the Activity."""
        if not code or len(code) > MAX_CODE:
            raise ServiceError("No sign-in code")
        res = self._call(
            "POST",
            "/oauth2/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "authorization_code",
                "code": code,
            },
        )
        token = res.json().get("access_token") if res.status_code == 200 else None
        if not token:
            raise ServiceError("Discord didn't accept the sign-in")
        return token

    def user(self, token: str) -> dict:
        """{id, name, avatar} of the token's Discord user (avatar: their picture's URL), if they are in the
        guild."""
        auth = {"Authorization": f"Bearer {token}"}
        res = self._call("GET", "/users/@me", headers=auth)
        if res.status_code != 200:
            raise ServiceError("Discord didn't say who you are")
        me = res.json()
        member = self._call("GET", f"/users/@me/guilds/{self.guild_id}/member", headers=auth)
        if member.status_code != 200:
            answer = f"HTTP {member.status_code} {member.text[:200]}"
            print(f"discord: membership check for {me.get('id')}: {answer}", flush=True)
            raise NotMember("This game is for members of Matt's Discord server")
        nick = member.json().get("nick")
        name = nick or me.get("global_name") or me.get("username") or "Discord user"
        return {"id": str(me["id"]), "name": name, "avatar": avatar_url(str(me["id"]), me.get("avatar"))}

    def _key(self) -> bytes:
        # Stable across pods and restarts, and changes when the client secret does.
        return hashlib.sha256(b"mtgtracker discord session\0" + self.client_secret.encode()).digest()

    def session(
        self, discord_id: str, name: str, now: float | None = None, avatar: str | None = None
    ) -> str:
        body = {"d": discord_id, "n": name, "a": avatar, "exp": int((now or time.time()) + SESSION_SECONDS)}
        payload = _b64(json.dumps(body, separators=(",", ":")).encode())
        return payload + "." + _b64(hmac.new(self._key(), payload.encode(), hashlib.sha256).digest())

    def read_session(self, token: str, now: float | None = None) -> dict | None:
        """{d: discord id, n: name, a: avatar URL} of a valid session token, else None."""
        payload, _, sig = (token or "").partition(".")
        good = _b64(hmac.new(self._key(), payload.encode(), hashlib.sha256).digest())
        if not payload or not hmac.compare_digest(sig, good):
            return None
        try:
            body = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except (ValueError, json.JSONDecodeError):
            return None
        return body if body.get("exp", 0) > (now or time.time()) else None


def avatar_url(user_id: str, avatar: str | None) -> str:
    """A Discord user's picture on Discord's CDN: theirs, or the default Discord gives them."""
    if avatar:
        return f"{CDN}/avatars/{user_id}/{avatar}.png?size=128"
    return f"{CDN}/embed/avatars/{(int(user_id) >> 22) % 6}.png"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Limiter:
    """At most `limit` events a minute, for everyone together."""

    def __init__(self, limit: int = EXCHANGES_PER_MINUTE):
        self.limit = limit
        self._lock = threading.Lock()
        self._window = 0
        self._count = 0

    def allow(self, now: float | None = None) -> bool:
        window = int((now or time.time()) // 60)
        with self._lock:
            if window != self._window:
                self._window, self._count = window, 0
            self._count += 1
            return self._count <= self.limit


EXCHANGES = Limiter()


def deck_owner(conn) -> users.User | None:
    """Whose decks the Activity offers, and whose history its games join: the tracker's first admin."""
    row = conn.execute(
        "SELECT * FROM users WHERE is_admin = 1 AND kind = 'person' AND disabled_at IS NULL"
        " ORDER BY id LIMIT 1"
    ).fetchone()
    return users._user(row)
