"""Play with a game server per game (mtgtracker/play/tables.py, FORGE_TABLES=kube): table pods on a fake
Kubernetes API, the warm spare, the cap, a deploy swapping the spare, and finished tables cleared away."""

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from mtgtracker import db
from mtgtracker.play import games, tables

from .conftest import install_transport
from .test_play import ROSTER, SECRET, _deck, _start

NS = "/api/v1/namespaces/forge"


class FakeCluster:
    """The Kubernetes API for namespace forge, and the control API of each table pod it runs."""

    def __init__(self):
        self.image = "forge-web:one"
        self.pods: dict[str, dict] = {}
        self.matches: dict[str, dict] = {}  # table id -> its match
        self.ready = True  # whether new pods come up ready

    def template(self) -> dict:
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"labels": {"app": "forge-table"}},
            "spec": {"subdomain": "tables", "containers": [{"name": "table", "image": self.image}]},
        }

    def by_state(self, state):
        return [p for p in self.pods.values() if p["metadata"]["labels"].get(tables.STATE) == state]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "kubernetes.default.svc":
            assert request.headers["authorization"] == "Bearer kube-token"
            return self.kube(request)
        table = request.url.host.split(".")[0]
        assert request.url.host == f"{table}.tables.forge.svc.cluster.local"
        assert request.url.port == 7790
        assert request.headers.get("authorization") == f"Bearer {SECRET}"
        return self.table(table, request)

    def kube(self, request):
        path, method = request.url.path, request.method
        if path == f"{NS}/configmaps/forge-table":
            return httpx.Response(200, json={"data": {"pod.json": json.dumps(self.template())}})
        if path == f"{NS}/pods" and method == "GET":
            assert request.url.params["labelSelector"] == "app=forge-table"
            return httpx.Response(200, json={"items": list(self.pods.values())})
        if path == f"{NS}/pods" and method == "POST":
            pod = json.loads(request.content)
            assert pod["spec"]["hostname"] == pod["metadata"]["labels"]["forge/table"]
            pod["status"] = {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True" if self.ready else "False"}],
            }
            self.pods[pod["metadata"]["name"]] = pod
            return httpx.Response(201, json=pod)
        name = path.removeprefix(f"{NS}/pods/")
        pod = self.pods.get(name)
        if pod is None:
            return httpx.Response(404, json={"message": "not found"})
        if method == "GET":
            return httpx.Response(200, json=pod)
        if method == "DELETE":
            del self.pods[name]
            return httpx.Response(200, json={})
        if method == "PATCH":
            assert request.headers["content-type"] == "application/merge-patch+json"
            pod["metadata"]["labels"].update(json.loads(request.content)["metadata"]["labels"])
            return httpx.Response(200, json=pod)
        return httpx.Response(405, json={})

    def table(self, table, request):
        path, method = request.url.path, request.method
        match = self.matches.get(table)
        if path == "/opponents":
            return httpx.Response(200, json={"opponents": ROSTER})
        if path == "/match" and method == "GET":
            if match is None:
                return httpx.Response(200, json={"state": "idle"})
            return httpx.Response(200, json=match)
        if path == "/match" and method == "POST":
            if match is not None:
                return httpx.Response(409, json={"error": "A game is already running here"})
            spec = json.loads(request.content)
            assert spec["matchId"] == table
            seats = [
                {
                    "index": i,
                    "kind": s["kind"],
                    "name": s.get("name") or s.get("opponent", "AI").title(),
                    **({"person": s["person"], "ticket": table * 2} if s["kind"] == "human" else {}),
                }
                for i, s in enumerate(spec["seats"])
            ]
            self.matches[table] = {"state": "playing", "matchId": table, "seats": seats}
            return httpx.Response(201, json={"matchId": table, "seats": seats})
        if path == f"/match/{table}" and method == "DELETE" and match and match["state"] == "playing":
            match.update(
                state="finished", result={"matchId": table, "status": "ended", "summary": "Ended", "turn": 2}
            )
            return httpx.Response(202, json={"ok": True})
        return httpx.Response(404, json={"error": "No running match with that id"})

    def exit(self, table):
        """The table's server is done (its pod ended itself)."""
        self.pods[f"forge-table-{table}"]["status"]["phase"] = "Succeeded"


@pytest.fixture
def cluster(monkeypatch, cache_dir, tmp_path):
    monkeypatch.setenv("FORGE_TABLES", "kube")
    monkeypatch.delenv("FORGE_CONTROL_URL", raising=False)
    monkeypatch.setenv("FORGE_CONTROL_SECRET", SECRET)
    monkeypatch.setenv("PLAY_RESULTS_URL", "http://tracker.test")
    sa = tmp_path / "sa"
    sa.mkdir()
    (sa / "token").write_text("kube-token\n")
    monkeypatch.setattr(tables, "SA_DIR", sa)
    monkeypatch.setattr(tables, "READY_WAIT", 0.0)
    fake = FakeCluster()
    install_transport(monkeypatch, httpx.MockTransport(fake))
    return fake


def _location(response):
    return urlsplit(response.headers["location"])


def test_the_play_page_warms_a_spare(client, cluster):
    page = client.get("/play")
    assert page.status_code == 200
    assert "Neyali" in page.text
    assert len(cluster.by_state("spare")) == 1
    client.get("/play")
    assert len(cluster.pods) == 1, "one spare, not one per visit"


def test_a_game_takes_the_spare_and_a_new_spare_warms(client, cluster, db_path):
    client.get("/play")
    (spare,) = cluster.by_state("spare")
    table = spare["metadata"]["labels"]["forge/table"]
    res = _start(client, _deck(db_path)["id"], "neyali", "jeleva")
    assert res.status_code == 303
    assert res.headers["location"] == f"/play/table/{table}/?ticket={table * 2}"
    assert spare["metadata"]["labels"][tables.STATE] == "playing"
    assert len(cluster.by_state("spare")) == 1
    conn = db.connect(db_path)
    (game,) = games.playing(conn)
    conn.close()
    assert game["match_id"] == table
    page = client.get("/play")
    assert f"/play/table/{table}/?ticket=" in page.text
    assert "End this game" in page.text


def test_two_games_run_at_once_and_the_fifth_is_refused(client, cluster, db_path):
    deck = _deck(db_path)["id"]
    links = {_location(_start(client, deck, "neyali")).path for _ in range(4)}
    assert len(links) == 4, "each game at its own table"
    assert len(cluster.by_state("playing")) == 4
    assert cluster.by_state("spare") == [], "no spare while every table is taken"
    res = _start(client, deck, "neyali")
    assert parse_qs(_location(res).query)["error"] == ["All 4 tables are in use. Try again when a game ends."]
    page = client.get("/play")
    assert "All 4 tables are in use" in page.text
    assert page.text.count("End this game") == 4


def test_a_deploy_swaps_the_spare_and_leaves_running_games(client, cluster, db_path):
    _start(client, _deck(db_path)["id"], "neyali")
    (playing,) = cluster.by_state("playing")
    cluster.image = "forge-web:two"
    client.get("/play")
    (spare,) = cluster.by_state("spare")
    assert spare["spec"]["containers"][0]["image"] == "forge-web:two"
    assert cluster.by_state("playing") == [playing]
    assert playing["spec"]["containers"][0]["image"] == "forge-web:one"


def test_a_game_that_is_over_frees_its_table_at_once(client, cluster, db_path):
    deck = _deck(db_path)["id"]
    for _ in range(4):
        _start(client, deck, "neyali")
    first = cluster.by_state("playing")[0]["metadata"]["labels"]["forge/table"]
    client.post("/play/end", data={"match_id": first}, follow_redirects=False)
    client.get("/play")
    assert [p["metadata"]["labels"]["forge/table"] for p in cluster.by_state("done")] == [first]
    assert len(cluster.by_state("spare")) == 1, "the ended game no longer counts against the cap"
    assert _start(client, deck, "neyali").headers["location"].startswith("/play/table/")


def test_a_finished_table_is_cleared_and_its_game_settled(client, cluster, db_path):
    _start(client, _deck(db_path)["id"], "neyali")
    (playing,) = cluster.by_state("playing")
    table = playing["metadata"]["labels"]["forge/table"]
    cluster.exit(table)
    client.get("/play")
    assert f"forge-table-{table}" not in cluster.pods
    conn = db.connect(db_path)
    row = conn.execute("SELECT status FROM play_games WHERE match_id = ?", (table,)).fetchone()
    conn.close()
    assert row["status"] == "abandoned"


def test_ending_a_game_reaches_its_own_table(client, cluster, db_path):
    _start(client, _deck(db_path)["id"], "neyali")
    _start(client, _deck(db_path)["id"], "jeleva")
    first, second = (p["metadata"]["labels"]["forge/table"] for p in cluster.by_state("playing"))
    res = client.post("/play/end", data={"match_id": first}, follow_redirects=False)
    assert parse_qs(_location(res).query)["notice"] == ["The game is ending."]
    assert cluster.matches[first]["state"] == "finished"
    assert cluster.matches[second]["state"] == "playing"


def test_no_ready_table_says_so(client, cluster, db_path):
    cluster.ready = False
    page = client.get("/play")
    assert "The game server is warming up" in page.text
    res = _start(client, _deck(db_path)["id"], "neyali")
    assert "warming up" in parse_qs(_location(res).query)["error"][0]


def test_extra_spares_are_trimmed(cluster):
    where = tables.Pods.from_env()
    where.sweep()
    where._create_spare(where._template())
    assert len(cluster.pods) == 2
    where.sweep()
    assert len(cluster.pods) == 1


def test_the_sweeper_sweeps_at_once_when_a_deploy_changes_the_template(cluster):
    where = tables.Pods.from_env()
    state: dict = {}
    assert tables.sweep_tick(where, state, 0.0)  # first look: sweeps (warms a spare)
    assert not tables.sweep_tick(where, state, 3.0)  # nothing changed, not due yet
    cluster.image = "forge-web:two"
    assert tables.sweep_tick(where, state, 6.0)  # a deploy: swept now, not at SWEEP_EVERY
    (spare,) = cluster.by_state("spare")
    assert spare["spec"]["containers"][0]["image"] == "forge-web:two"
    assert not tables.sweep_tick(where, state, 9.0)
    assert tables.sweep_tick(where, state, 6.0 + tables.SWEEP_EVERY)  # and still every SWEEP_EVERY
