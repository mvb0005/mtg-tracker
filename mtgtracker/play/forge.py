"""The Forge game server's control API (forge-gui-web PROTOCOL.md "Control API"), in-cluster only.

Configured from the environment: FORGE_CONTROL_URL (e.g. http://forge-control.forge.svc.cluster.local),
FORGE_CONTROL_SECRET (shared with the game server; it also signs the results it posts back here), and
FORGE_TABLE_URL, where players open the table (default /play/table/, on this site's hostname); each game is
at <table url><match id>/. Without
FORGE_CONTROL_URL there is no game server, and the play page says so.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from mtgtracker.services import ServiceError, UpstreamError

SPEC_VERSION = 1
TIMEOUT = 15.0


class Busy(ServiceError):
    """The game server is already running a match (it hosts one table)."""


@dataclass(frozen=True)
class GameServer:
    base_url: str
    secret: str | None
    table_url: str = "/play/table/"

    @classmethod
    def from_env(cls) -> GameServer | None:
        base = os.environ.get("FORGE_CONTROL_URL", "").strip().rstrip("/")
        if not base:
            return None
        return cls(
            base,
            os.environ.get("FORGE_CONTROL_SECRET") or None,
            (os.environ.get("FORGE_TABLE_URL", "").strip() or "/play/table").rstrip("/") + "/",
        )

    def _call(
        self, method: str, path: str, body: dict | None = None, timeout: float = TIMEOUT
    ) -> tuple[int, dict]:
        headers = {"Authorization": f"Bearer {self.secret}"} if self.secret else {}
        try:
            with httpx.Client(timeout=timeout) as client:
                res = client.request(method, self.base_url + path, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise UpstreamError("The game server isn't answering") from exc
        if res.status_code >= 500 or res.status_code == 401:
            raise UpstreamError(f"The game server refused ({res.status_code})")
        try:
            return res.status_code, res.json()
        except ValueError as exc:
            raise UpstreamError("The game server sent something unreadable") from exc

    def opponents(self) -> list[dict]:
        return self._call("GET", "/opponents")[1].get("opponents", [])

    def status(self) -> dict:
        return self._call("GET", "/match")[1]

    def check_deck(self, name: str, text: str) -> str | None:
        """None when Forge can play the list, else what's wrong with it."""
        body = self._call("POST", "/decks/check", {"name": name, "list": text})[1]
        return None if body.get("ok") else body.get("error", "Forge can't play this deck")

    def import_url(self, url: str) -> dict:
        """Imports a Commander deck from a Moxfield, Archidekt, TappedOut or MTGGoldfish URL."""
        body = self._call("POST", "/decks/import-url", {"url": url}, timeout=45.0)[1]
        if not body.get("ok"):
            raise ServiceError(body.get("error", "Forge couldn't import that deck URL"))
        return body

    def start(self, spec: dict) -> dict:
        """{matchId, seats: [{index, kind, name, person?, ticket?}]}. Busy when a match is running."""
        status, body = self._call("POST", "/match", {"version": SPEC_VERSION, **spec})
        if status == 409:
            raise Busy(body.get("error", "A game is already running"))
        if status != 201:
            raise ServiceError(body.get("error", f"The game server refused ({status})"))
        return body

    def end(self, match_id: str) -> bool:
        """True if that match was running and is ending now."""
        return self._call("DELETE", f"/match/{match_id}")[0] == 202

    def table_link(self, match_id: str, ticket: str) -> str:
        return f"{self.table_url}{match_id}/?ticket={ticket}"
