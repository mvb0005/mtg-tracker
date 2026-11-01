"""Where games run: a game server per game, or one shared game server.

Pods (FORGE_TABLES=kube, how the cluster runs it): every game gets its own Forge game server, a pod in
namespace FORGE_TABLES_NAMESPACE (default forge) made from the forge-table template that forge-gui-web's
deploy writes. Pod forge-table-<id> has hostname <id>, so its control API is
http://<id>.tables.<namespace>.svc.cluster.local:7790 and players reach it at /play/table/<id>/ through
the forge router; the match takes the same id. One spare is kept warm so a game starts at once, at most
CAPACITY games run, and a pod ends itself after its game (deleted here once it has). A deploy only changes
the template: the spare is swapped for one on the new build, running games keep theirs.

One server (FORGE_CONTROL_URL, local development and tests): a single game server hosting one game at a time.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from mtgtracker.play.forge import Busy, GameServer
from mtgtracker.services import ServiceError, UpstreamError

CAPACITY = 4
SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
API = "https://kubernetes.default.svc"
STATE = "forge/state"  # spare | playing | done (over, the pod closes itself soon)
READY_WAIT = 60.0
SWEEP_EVERY = 30.0


def from_env() -> OneServer | Pods | None:
    if os.environ.get("FORGE_TABLES", "").strip() == "kube":
        return Pods.from_env()
    server = GameServer.from_env()
    return OneServer(server) if server else None


def control_secret() -> str | None:
    return os.environ.get("FORGE_CONTROL_SECRET") or None


def _table_url() -> str:
    return (os.environ.get("FORGE_TABLE_URL", "").strip() or "/play/table").rstrip("/") + "/"


@dataclass
class OneServer:
    """One game server, one game at a time."""

    server: GameServer
    capacity: int = 1

    def table_link(self, match_id: str, ticket: str) -> str:
        return self.server.table_link(match_id, ticket)

    def lobby(self) -> GameServer:
        return self.server

    def start(self, spec: dict) -> dict:
        return self.server.start(spec)

    def server_for(self, match_id: str) -> GameServer | None:
        return self.server

    def status(self, match_id: str) -> dict | None:
        """That game's status, or None when the server no longer knows it."""
        table = self.server.status()
        return table if table.get("matchId") == match_id else None

    def running(self) -> list[dict]:
        table = self.server.status()
        return [table] if table.get("state") == "playing" else []

    def sweep(self) -> None:
        pass


class Kube:
    """The Kubernetes API, as this pod's service account (the token is re-read: it rotates)."""

    def __init__(self, namespace: str, base: str = API, sa_dir: Path | None = None):
        self.namespace = namespace
        self.base = base
        self.sa_dir = sa_dir or SA_DIR

    def call(
        self, method: str, path: str, body: dict | None = None, *, patch: bool = False
    ) -> tuple[int, dict]:
        token = (self.sa_dir / "token").read_text().strip()
        headers = {"Authorization": f"Bearer {token}"}
        if patch:
            headers["Content-Type"] = "application/merge-patch+json"
        ca = self.sa_dir / "ca.crt"
        try:
            with httpx.Client(timeout=15.0, verify=str(ca) if ca.exists() else True) as client:
                res = client.request(
                    method,
                    f"{self.base}/api/v1/namespaces/{self.namespace}{path}",
                    content=json.dumps(body) if body is not None else None,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise UpstreamError("The cluster isn't answering") from exc
        if res.status_code >= 500 or res.status_code in (401, 403):
            raise UpstreamError(f"The cluster refused ({res.status_code})")
        return res.status_code, res.json() if res.content else {}


def _ready(pod: dict) -> bool:
    return any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in pod.get("status", {}).get("conditions", [])
    )


def _image(pod: dict) -> str:
    return pod["spec"]["containers"][0]["image"]


@dataclass
class Pods:
    kube: Kube
    capacity: int = CAPACITY
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def from_env(cls) -> Pods:
        return cls(Kube(os.environ.get("FORGE_TABLES_NAMESPACE", "").strip() or "forge"))

    def server_at(self, table_id: str) -> GameServer:
        host = f"{table_id}.tables.{self.kube.namespace}.svc.cluster.local"
        return GameServer(f"http://{host}:7790", control_secret(), _table_url())

    def table_link(self, match_id: str, ticket: str) -> str:
        return f"{_table_url()}{match_id}/?ticket={ticket}"

    def _pods(self) -> list[dict]:
        _, body = self.kube.call("GET", "/pods?labelSelector=app%3Dforge-table")
        return [p for p in body.get("items", []) if not p["metadata"].get("deletionTimestamp")]

    def _template(self) -> dict:
        status, body = self.kube.call("GET", "/configmaps/forge-table")
        if status != 200:
            raise ServiceError("No table template: deploy the game server first")
        return json.loads(body["data"]["pod.json"])

    def _delete(self, pod: dict) -> None:
        self.kube.call("DELETE", f"/pods/{pod['metadata']['name']}")

    def _create_spare(self, template: dict) -> dict:
        table_id = secrets.token_hex(8)
        pod = json.loads(json.dumps(template))
        meta = pod.setdefault("metadata", {})
        meta["name"] = f"forge-table-{table_id}"
        meta.setdefault("labels", {}).update({STATE: "spare", "forge/table": table_id})
        pod["spec"]["hostname"] = table_id
        status, body = self.kube.call("POST", "/pods", pod)
        if status not in (200, 201):
            raise ServiceError(body.get("message", f"Couldn't make a table ({status})"))
        return body

    def _over(self, pod: dict) -> bool:
        """Whether a playing table's game has ended (it lingers a while for results and a last look)."""
        try:
            table = self.server_at(pod["metadata"]["labels"]["forge/table"]).status()
        except UpstreamError:
            return False
        return table.get("state") == "finished"

    def _label(self, pod: dict, state: str) -> None:
        self.kube.call(
            "PATCH", f"/pods/{pod['metadata']['name']}", {"metadata": {"labels": {STATE: state}}}, patch=True
        )
        pod["metadata"]["labels"][STATE] = state

    def sweep(self) -> list[dict]:
        """Put the tables right: delete the closed ones and spares on an old build, mark games that are over,
        and keep one spare while there is room. Returns the pods left."""
        with self._lock:
            template = self._template()
            image = _image(template)
            live = []
            for pod in self._pods():
                labels = pod["metadata"].get("labels", {})
                done = pod.get("status", {}).get("phase") in ("Succeeded", "Failed")
                stale = labels.get(STATE) == "spare" and _image(pod) != image
                if done or stale:
                    self._delete(pod)
                else:
                    live.append(pod)
            for pod in live:
                if pod["metadata"]["labels"].get(STATE) == "playing" and _ready(pod) and self._over(pod):
                    self._label(pod, "done")
            spares = [p for p in live if p["metadata"]["labels"].get(STATE) == "spare"]
            playing = [p for p in live if p["metadata"]["labels"].get(STATE) == "playing"]
            spares.sort(key=lambda p: not _ready(p))
            for extra in spares[1:]:
                self._delete(extra)
                live.remove(extra)
            if not spares and len(playing) < self.capacity:
                live.append(self._create_spare(template))
            return live

    def _spares(self, live: list[dict]) -> list[dict]:
        return [p for p in live if p["metadata"]["labels"].get(STATE) == "spare" and _ready(p)]

    def lobby(self) -> GameServer:
        """A table to ask for the roster and deck checks: the spare, or any running one."""
        live = self.sweep()
        ready = [p for p in live if _ready(p)]
        ready.sort(key=lambda p: p["metadata"]["labels"].get(STATE) != "spare")
        if not ready:
            raise ServiceError("The game server is warming up. Try again in a moment.")
        return self.server_at(ready[0]["metadata"]["labels"]["forge/table"])

    def start(self, spec: dict) -> dict:
        """Starts the game on the spare (waiting for one to be ready) and marks it playing."""
        deadline = time.monotonic() + READY_WAIT
        while True:
            live = self.sweep()
            if sum(p["metadata"]["labels"].get(STATE) == "playing" for p in live) >= self.capacity:
                raise Busy(f"All {self.capacity} tables are in use")
            for pod in self._spares(live):
                table_id = pod["metadata"]["labels"]["forge/table"]
                try:
                    started = self.server_at(table_id).start({**spec, "matchId": table_id})
                except Busy:
                    continue  # someone else's start took this spare first
                self._label(pod, "playing")
                self.sweep()  # the next spare
                return started
            if time.monotonic() > deadline:
                raise ServiceError("No table came up in time. Try again in a moment.")
            time.sleep(2)

    def _pod(self, table_id: str) -> dict | None:
        status, body = self.kube.call("GET", f"/pods/forge-table-{table_id}")
        return body if status == 200 and not body["metadata"].get("deletionTimestamp") else None

    def server_for(self, match_id: str) -> GameServer | None:
        return self.server_at(match_id) if self._pod(match_id) else None

    def status(self, match_id: str) -> dict | None:
        """That game's status, None once its pod is gone. Raises UpstreamError when the pod is there but
        doesn't answer: that's no reason to give up on the game."""
        pod = self._pod(match_id)
        if pod is None or pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            return None
        return self.server_at(match_id).status()

    def running(self) -> list[dict]:
        out = []
        for pod in self._pods():
            if pod["metadata"].get("labels", {}).get(STATE) not in ("playing", "done"):
                continue
            try:
                table = self.server_at(pod["metadata"]["labels"]["forge/table"]).status()
            except UpstreamError:
                continue
            if table.get("state") == "playing":
                out.append(table)
        return out


_sweeper: threading.Thread | None = None


def start_sweeper() -> None:
    """Keeps the spare warm and clears finished tables between page loads (pods mode only)."""
    global _sweeper
    tables = from_env()
    if not isinstance(tables, Pods) or _sweeper is not None:
        return

    def run() -> None:
        while True:
            try:
                tables.sweep()
            except ServiceError as exc:
                print(f"play: table sweep failed: {exc}", flush=True)
            time.sleep(SWEEP_EVERY)

    _sweeper = threading.Thread(target=run, name="play-tables", daemon=True)
    _sweeper.start()
