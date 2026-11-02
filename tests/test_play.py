"""Play (mtgtracker/play): the play page, starting and ending games on a fake game server, results coming
back, usernames and the people list, and importing the game server's old history."""

import gzip
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from mtgtracker import db, users
from mtgtracker.play import games
from mtgtracker.web.app import create_app
from mtgtracker.web.auth import DEV_COOKIE, AuthConfig

from .conftest import install_transport
from .test_multiuser import _cf_client, signer  # noqa: F401 (fixture)

SECRET = "s3cret-for-tests"
ROSTER = [
    {
        "id": "neyali",
        "commander": "Neyali, Suns' Vanguard",
        "archetype": "Go-wide aggro",
        "plan": "Makes tokens and attacks.",
        "deck": "Rebellion Rising",
        "colors": "RW",
        "profile": "Reckless",
    },
    {
        "id": "jeleva",
        "commander": "Jeleva, Nephalia's Scourge",
        "archetype": "Control",
        "plan": "Counters and steals.",
        "deck": "Mind Seize",
        "colors": "UBR",
        "profile": "Cautious",
    },
]


class FakeGameServer:
    """The game server's control API (forge-gui-web PROTOCOL.md), one table."""

    def __init__(self):
        self.calls = []
        self.match = None  # the running match's id
        self.result = None
        self.specs = []
        self.n = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "forge.test", request.url
        self.calls.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.headers.get("authorization") != f"Bearer {SECRET}":
            return httpx.Response(401, json={"error": "Missing or wrong control secret"})
        path, method = request.url.path, request.method
        if path == "/opponents":
            return httpx.Response(200, json={"opponents": ROSTER})
        if path == "/decks/import-url" and method == "POST":
            body = json.loads(request.content)
            url = body.get("url", "")
            if "bad" in url:
                return httpx.Response(200, json={"ok": False, "error": "Only Moxfield, Archidekt, TappedOut, MTGGoldfish deck URLs are supported."})
            return httpx.Response(200, json={
                "ok": True,
                "name": "Moxfield Dragons",
                "commander": "Miirym, Sentinel Wyrm",
                "colors": "URG",
                "manaCost": "{3}{G}{U}{R}",
                "typeLine": "Legendary Creature — Dragon Spirit",
                "oracleText": "Ward {2}",
                "pt": "6/6",
                "list": "Commander\n1 Miirym, Sentinel Wyrm\n\nDeck\n99 Mountain\n",
                "sourceUrl": url,
            })
        if path == "/match" and method == "GET":
            if self.match:
                return httpx.Response(
                    200,
                    json={
                        "state": "playing",
                        "matchId": self.match,
                        "watchTicket": f"w{self.n}" * 16,
                        "seats": getattr(self, "seats", None)
                        or [{"index": 0, "kind": "human", "name": "Owner", "person": "1"}],
                    },
                )
            body = {"state": "finished" if self.result else "idle"}
            if self.result:
                body.update(matchId=self.result["matchId"], result=self.result)
            return httpx.Response(200, json=body)
        if path == "/match" and method == "POST":
            if self.match:
                return httpx.Response(409, json={"error": "A game is already running here"})
            spec = json.loads(request.content)
            self.specs.append(spec)
            self.n += 1
            self.match = f"m{self.n}"
            seats = [
                {
                    "index": i,
                    "kind": s["kind"],
                    "name": s.get("name") or s.get("opponent", "AI").title(),
                    **(
                        {"person": s["person"], "ticket": f"t{self.n}{i}" * 4} if s["kind"] == "human" else {}
                    ),
                }
                for i, s in enumerate(spec["seats"])
            ]
            self.seats = seats
            return httpx.Response(201, json={"matchId": self.match, "watchTicket": f"w{self.n}" * 16, "seats": seats})
        if path.startswith("/match/") and method == "DELETE":
            if self.match != path.split("/")[-1]:
                return httpx.Response(404, json={"error": "No running match with that id"})
            self.result = {"matchId": self.match, "status": "ended", "summary": "Ended", "turn": 3}
            self.match = None
            return httpx.Response(202, json={"ok": True})
        return httpx.Response(404, json={"error": "Not found"})


@pytest.fixture
def forge(monkeypatch, cache_dir):
    monkeypatch.setenv("FORGE_CONTROL_URL", "http://forge.test")
    monkeypatch.setenv("FORGE_CONTROL_SECRET", SECRET)
    monkeypatch.setenv("PLAY_RESULTS_URL", "http://tracker.test")
    from mtgtracker.play import routes as play
    play._IMPORTED.clear()
    fake = FakeGameServer()
    install_transport(monkeypatch, httpx.MockTransport(fake))
    return fake


def _deck(db_path, name="Reign of Dragons"):
    conn = db.connect(db_path)
    row = conn.execute("SELECT id, commander FROM decks WHERE name = ?", (name,)).fetchone()
    conn.close()
    return row


def _start(client, deck_id, *opponents):
    return client.post(
        "/play/start", data={"deck_id": str(deck_id), "opponent": list(opponents)}, follow_redirects=False
    )


def _error(response):
    return parse_qs(urlsplit(response.headers["location"]).query).get("error", [""])[0]


def test_play_page_lists_your_decks_and_the_roster(client, forge):
    page = client.get("/play")
    assert page.status_code == 200
    assert "Reign of Dragons" in page.text
    assert "Lathliss, Dragon Queen" in page.text
    assert "Neyali" in page.text
    assert "Go-wide aggro" in page.text
    assert 'href="/play" aria-current="page"' in page.text
    assert "Import a deck from a URL" in page.text
    assert "Moxfield" in page.text


def test_import_url_adds_a_deck_you_can_start(client, forge, db_path):
    from mtgtracker.play import routes as play

    bad = client.post("/play/import", data={"url": "https://example.com/bad"}, follow_redirects=False)
    assert "Moxfield" in _error(bad)
    ok = client.post("/play/import", data={"url": "https://moxfield.com/decks/abc123"}, follow_redirects=False)
    assert ok.status_code == 303
    page = client.get("/play")
    assert "Moxfield Dragons" in page.text
    assert "Imported from moxfield.com" in page.text
    did = next(iter(play._IMPORTED))
    res = _start(client, did, "neyali")
    assert res.status_code == 303
    assert forge.specs[-1]["seats"][0]["deck"]["name"] == "Moxfield Dragons"
    assert forge.specs[-1]["seats"][0]["deck"]["list"].startswith("Commander\n1 Miirym")


def test_play_page_without_a_game_server(client, cache_dir, monkeypatch):
    monkeypatch.delenv("FORGE_CONTROL_URL", raising=False)
    page = client.get("/play")
    assert "No game server is set up" in page.text
    assert _error(_start(client, _deck(client.app.state.db_path)["id"], "neyali")).startswith(
        "No game server"
    )


def test_start_hands_the_game_server_your_decklist_and_sends_you_to_the_table(client, forge, db_path):
    deck = _deck(db_path)
    res = _start(client, deck["id"], "neyali", "jeleva")
    assert res.status_code == 303
    assert res.headers["location"] == "/play/table/m1/?ticket=" + "t10" * 4
    spec = forge.specs[0]
    assert spec["version"] == 1
    assert spec["format"] == "commander"
    human, *ais = spec["seats"]
    assert human["person"] == "1"
    assert human["name"] == "Owner"
    assert human["deck"]["name"] == "Reign of Dragons"
    assert human["deck"]["list"].startswith("Commander\n1 Lathliss, Dragon Queen")
    assert ais == [{"kind": "ai", "opponent": "neyali"}, {"kind": "ai", "opponent": "jeleva"}]
    assert spec["returnUrl"] == "/play"
    assert spec["resultsUrl"] == "http://tracker.test/play/results"
    assert spec["replayUrl"] == "http://tracker.test/play/replays"
    conn = db.connect(db_path)
    row = conn.execute("SELECT * FROM play_games").fetchone()
    assert (row["match_id"], row["host_id"], row["status"], row["ticket"]) == ("m1", 1, "playing", "t10" * 4)
    assert [o["deck"] for o in json.loads(row["opponents"])] == ["Rebellion Rising", "Mind Seize"]
    audit = conn.execute("SELECT summary_json FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert "Started a game: Reign of Dragons" in audit["summary_json"]
    conn.close()
    # The play page now offers the way back to the table, and to end it.
    page = client.get("/play")
    assert "Back to the table" in page.text
    assert "/play/table/m1/?ticket=" in page.text
    assert "t10" * 4 in page.text
    assert "End this game" in page.text


def test_start_refusals(client, forge, db_path):
    deck = _deck(db_path)
    assert _error(_start(client, deck["id"])) == "Pick one to 3 opponents"
    assert (
        _error(_start(client, deck["id"], "neyali", "jeleva", "neyali", "jeleva"))
        == "Pick one to 3 opponents"
    )
    assert _error(_start(client, deck["id"], "nobody")) == "Unknown opponent nobody"
    conn = db.connect(db_path)
    conn.execute("UPDATE decks SET commander = NULL WHERE id = ?", (deck["id"],))
    conn.commit()
    conn.close()
    assert "has no commander" in _error(_start(client, deck["id"], "neyali"))
    assert forge.specs == []


def test_a_busy_table_names_who_is_playing(client, forge, db_path):
    deck = _deck(db_path)
    _start(client, deck["id"], "neyali")
    second = _start(client, deck["id"], "jeleva")
    assert _error(second) == "Owner is playing on the game server right now. It hosts one game at a time."


def test_someone_elses_deck_is_refused(db_path, forge):
    conn = db.connect(db_path)
    friend = users.create(conn, "Alex", "alex@example.com")
    conn.close()
    client = TestClient(create_app(db_path, auth=AuthConfig(mode="dev")))
    client.cookies.set(DEV_COOKIE, str(friend.id))
    assert _start(client, _deck(db_path)["id"], "neyali").status_code == 403


def test_results_complete_the_game_once(client, forge, db_path):
    _start(client, _deck(db_path)["id"], "neyali")
    result = {
        "matchId": "m1",
        "status": "finished",
        "winner": "Owner",
        "turn": 9,
        "summary": "Owner won on turn 9",
        "endedAt": "2026-10-31T21:00:00Z",
        "log": ["Turn 1", "Turn 9"],
        "seats": [],
    }
    assert (
        client.post("/play/results", json=result, headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    assert client.post("/play/results", json=result).status_code == 401  # TestClient is not loopback
    ok = client.post("/play/results", json=result, headers={"Authorization": f"Bearer {SECRET}"})
    assert ok.status_code == 204
    again = client.post(
        "/play/results", json={**result, "winner": "Neyali"}, headers={"Authorization": f"Bearer {SECRET}"}
    )
    assert again.status_code == 204
    game = games.get(db.connect(db_path), 1)
    assert (game["status"], game["winner"], game["turn"], game["ticket"]) == ("finished", "Owner", 9, None)
    assert game["log"] == ["Turn 1", "Turn 9"]
    assert "Owner won on turn 9" in client.get("/play").text
    unknown = client.post(
        "/play/results",
        json={"matchId": "zzz", "status": "finished"},
        headers={"Authorization": f"Bearer {SECRET}"},
    )
    assert unknown.status_code == 204


def test_results_need_no_sign_in_but_everything_else_does(db_path, forge, signer):  # noqa: F811
    client = _cf_client(db_path, signer)
    assert client.get("/play").status_code == 401
    res = client.post(
        "/play/results",
        json={"matchId": "m9", "status": "finished"},
        headers={"Authorization": f"Bearer {SECRET}"},
    )
    assert res.status_code == 204


def test_ending_your_game_and_settling_stale_ones(client, forge, db_path):
    _start(client, _deck(db_path)["id"], "neyali")
    end = client.post("/play/end", data={"match_id": "m1"}, follow_redirects=False)
    assert "notice=The+game+is+ending" in end.headers["location"]
    assert ("DELETE", "/match/m1", f"Bearer {SECRET}") in forge.calls
    # The result never arrived (no resultsUrl reached us): the next start takes it from the game server.
    _start(client, _deck(db_path)["id"], "jeleva")
    conn = db.connect(db_path)
    statuses = [tuple(r) for r in conn.execute("SELECT match_id, status FROM play_games ORDER BY id")]
    assert statuses == [("m1", "ended"), ("m2", "playing")]
    # The game server restarted: m2 is gone without a result, so it is abandoned.
    forge.match, forge.result = None, None
    stale = client.post("/play/end", data={"match_id": "m2"}, follow_redirects=False)
    assert "already+ended" in stale.headers["location"]
    assert conn.execute("SELECT status FROM play_games WHERE match_id = 'm2'").fetchone()[0] == "abandoned"
    conn.close()


def test_only_the_host_or_an_admin_ends_a_game(db_path, forge):
    owner = TestClient(create_app(db_path, auth=AuthConfig(mode="dev")))
    owner.cookies.set(DEV_COOKIE, "1")
    _start(owner, _deck(db_path)["id"], "neyali")
    conn = db.connect(db_path)
    friend = users.create(conn, "Alex", "alex@example.com")
    conn.close()
    other = TestClient(create_app(db_path, auth=AuthConfig(mode="dev")))
    other.cookies.set(DEV_COOKIE, str(friend.id))
    page = other.get("/play")
    assert "Owner is playing." in page.text
    assert "End their game" not in page.text
    assert other.post("/play/end", data={"match_id": "m1"}, follow_redirects=False).status_code == 403
    assert forge.match == "m1"


def test_usernames_on_the_profile_and_in_the_people_list(client, forge, db_path):
    conn = db.connect(db_path)
    users.create(conn, "Alex", "alex@example.com")
    users.set_username(conn, 2, "alex")
    conn.close()
    saved = client.post(
        "/profile", data={"display_name": "Matt", "username": "Matt_B"}, follow_redirects=False
    )
    assert "notice=Saved" in saved.headers["location"]
    taken = client.post("/profile", data={"display_name": "Matt", "username": "ALEX"}, follow_redirects=False)
    assert "is+taken" in taken.headers["location"]
    bad = client.post(
        "/profile", data={"display_name": "Matt", "username": "no spaces"}, follow_redirects=False
    )
    assert "usernames+are" in bad.headers["location"]
    people = client.get("/api/v1/people").json()["people"]
    assert [(p["name"], p["username"]) for p in people] == [("Matt", "matt_b"), ("Alex", "alex")]
    assert all("email" not in p for p in people)
    me = client.get("/api/v1/me").json()["principal"]
    assert me["username"] == "matt_b"
    cleared = client.put("/api/v1/profile", json={"display_name": "Matt", "username": ""}).json()
    assert cleared["profile"]["username"] is None


def test_import_the_game_servers_old_history(db_path):
    conn = db.connect(db_path)
    conn.execute("UPDATE users SET email = 'me@example.com' WHERE id = 1")
    conn.commit()
    data = {
        "version": 1,
        "games": [
            {
                "id": "a",
                "email": "me@example.com",
                "deckName": "Reign of Dragons",
                "opponentDeckName": "Neyali, Jeleva",
                "format": "commander",
                "status": "finished",
                "winner": "Neyali",
                "turn": 8,
                "summary": "Neyali won on turn 8",
                "startedAt": "2026-10-30T01:00:00Z",
                "endedAt": "2026-10-30T01:30:00Z",
                "log": ["x"],
            },
            {"id": "b", "deckName": "Random RG", "status": "finished", "startedAt": "2026-10-29T01:00:00Z"},
            {
                "id": "c",
                "email": "stranger@example.com",
                "deckName": "?",
                "status": "playing",
                "startedAt": "2026-10-29",
            },
        ],
    }
    by_email = lambda e: users.by_email(conn, e).id if e and users.by_email(conn, e) else None  # noqa: E731
    assert games.import_history(conn, data, owner_by_email=by_email) == (1, 2)
    assert games.import_history(conn, data, owner_by_email=by_email) == (0, 3)
    [game] = games.recent(conn, 1)
    assert (game["match_id"], game["deck_name"], game["status"]) == (
        "legacy:a",
        "Reign of Dragons",
        "finished",
    )
    assert [o["name"] for o in game["opponents"]] == ["Neyali", "Jeleva"]
    conn.close()


REPLAY = gzip.compress(b'{"replay":1,"matchId":"0123456789abcdef","seat":0}\n{"ms":5,"m":{"t":"state"}}\n')


def _upload(client, path, body=REPLAY, secret=SECRET):
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    return client.post(path, content=body, headers=headers)


def test_a_replay_is_kept_and_its_host_can_download_it(client, forge, db_path):
    conn = db.connect(db_path)
    games.record_start(
        conn, match_id="0123456789abcdef", host_id=1, format="commander", deck_id=None,
        deck_name="Reign of Dragons", opponents=[{"name": "Neyali", "deck": "Rebellion Rising"}], ticket=None,
    )
    conn.close()
    assert _upload(client, "/play/replays/0123456789abcdef/0").status_code == 204
    assert (db_path.parent / "replays" / "0123456789abcdef-0.jsonl.gz").read_bytes() == REPLAY
    page = client.get("/play")
    assert 'href="/play/games/0123456789abcdef/replay/0"' in page.text
    got = client.get("/play/games/0123456789abcdef/replay/0")
    assert got.status_code == 200
    assert got.content == REPLAY
    assert client.get("/play/games/0123456789abcdef/replay/1").status_code == 404


def test_replay_uploads_are_checked(client, forge, db_path):
    assert _upload(client, "/play/replays/0123456789abcdef/0", secret="wrong").status_code == 401
    assert _upload(client, "/play/replays/0123456789abcdef/0", secret=None).status_code == 401
    assert _upload(client, "/play/replays/..%2F..%2Fmtgtracker/0").status_code in (400, 404)
    assert _upload(client, "/play/replays/0123456789ABCDEF/0").status_code == 400
    assert _upload(client, "/play/replays/0123456789abcdef/9").status_code == 400
    assert _upload(client, "/play/replays/0123456789abcdef/0", body=b"not gzip").status_code == 400
    assert not (db_path.parent / "replays").exists()


def test_someone_elses_replay_is_not_found(db_path, forge):
    conn = db.connect(db_path)
    games.record_start(
        conn, match_id="0123456789abcdef", host_id=1, format="commander", deck_id=None,
        deck_name="Reign of Dragons", opponents=[], ticket=None,
    )
    friend = users.create(conn, "Alex", "alex@example.com")
    conn.close()
    games.save_replay(db_path, "0123456789abcdef", 0, REPLAY)
    other = TestClient(create_app(db_path, auth=AuthConfig(mode="dev")))
    other.cookies.set(DEV_COOKIE, str(friend.id))
    assert other.get("/play/games/0123456789abcdef/replay/0").status_code == 404


def test_replays_need_no_sign_in(db_path, forge, signer):  # noqa: F811
    client = _cf_client(db_path, signer)
    assert _upload(client, "/play/replays/0123456789abcdef/0").status_code == 204
    assert client.get("/play/games/0123456789abcdef/replay/0").status_code == 401
