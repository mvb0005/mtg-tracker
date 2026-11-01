"""Discord (mtgtracker/discord): the Activity's sign-in through a fake Discord, guild membership, sessions,
the shared lobby per Activity instance (players, decks, AI opponents, starting a game for everyone), and
/discord being reachable without Cloudflare Access while nothing else is."""

import json
import time

import httpx
import pytest

from mtgtracker import db
from mtgtracker.discord import auth, rooms

from .conftest import install_transport
from .test_multiuser import _cf_client, signer  # noqa: F401 (fixture)
from .test_play import SECRET, FakeGameServer, _deck

GUILD = "616087028823621642"
INSTANCE = "i-1566323350148616243-gc-616087028823621642-616087028823621653"


class FakeDiscord:
    """discord.com: the code exchange, /users/@me and guild membership."""

    def __init__(self):
        self.codes = {}  # code -> user id
        self.users = {}  # user id -> {id, username, global_name}
        self.members = set()
        self.exchanges = []

    def add(self, user_id, name, *, member=True):
        self.users[user_id] = {"id": user_id, "username": name.lower(), "global_name": name}
        if member:
            self.members.add(user_id)
        code = f"code-{user_id}"
        self.codes[code] = user_id
        return code

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v10")
        if path == "/oauth2/token":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            self.exchanges.append(form)
            assert form["client_secret"] == "client-secret"
            user_id = self.codes.pop(form.get("code"), None)  # a code works once
            if user_id is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": f"tok-{user_id}", "token_type": "Bearer"})
        token = request.headers.get("authorization", "").removeprefix("Bearer tok-")
        if token not in self.users:
            return httpx.Response(401, json={"message": "401: Unauthorized"})
        if path == "/users/@me":
            return httpx.Response(200, json=self.users[token])
        if path == f"/users/@me/guilds/{GUILD}/member":
            if token in self.members:
                return httpx.Response(200, json={"nick": None, "user": self.users[token]})
            return httpx.Response(404, json={"message": "Unknown Guild"})
        return httpx.Response(404, json={"message": "Not found"})


@pytest.fixture
def discord(monkeypatch, cache_dir):
    monkeypatch.setenv("DISCORD_CLIENT_ID", "662512381221535764")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("DISCORD_GUILD_ID", GUILD)
    monkeypatch.setenv("FORGE_CONTROL_URL", "http://forge.test")
    monkeypatch.setenv("FORGE_CONTROL_SECRET", SECRET)
    monkeypatch.setenv("PLAY_RESULTS_URL", "http://tracker.test")
    monkeypatch.setattr(auth, "EXCHANGES", auth.Limiter())
    rooms.clear()
    fake, forge = FakeDiscord(), FakeGameServer()
    fake.forge = forge
    install_transport(
        monkeypatch,
        httpx.MockTransport(lambda r: fake(r) if r.url.host == "discord.com" else forge(r)),
    )
    yield fake
    rooms.clear()


def _sign_in(client, code):
    return client.post("/discord/api/session", json={"code": code})


def _bearer(session):
    return {"Authorization": f"Bearer {session}"}


def _session(client, discord, user_id, name):
    return _sign_in(client, discord.add(user_id, name)).json()["session"]


def _room(client, session, instance=INSTANCE, **change):
    return client.post("/discord/api/room", json={"instanceId": instance, **change}, headers=_bearer(session))


def test_the_activity_page_loads_its_own_files(client, discord):
    page = client.get("/discord/")
    assert page.status_code == 200
    assert 'data-client-id="662512381221535764"' in page.text
    assert 'href="static/activity.css?v=' in page.text
    assert 'src="static/activity.js?v=' in page.text
    assert page.headers["cache-control"] == "no-cache"
    assert client.get("/discord", follow_redirects=False).headers["location"] == "/discord/"
    script = client.get("/discord/static/activity.js?v=x")
    assert script.status_code == 200
    assert "immutable" in script.headers["cache-control"]
    assert client.get("/discord/static/activity.js").headers["cache-control"] == "no-cache"
    sdk = client.get("/discord/static/discord-sdk.js")
    assert sdk.status_code == 200
    assert "DiscordSDK" in sdk.text


def test_a_member_signs_in_by_their_discord_id(client, discord):
    res = _sign_in(client, discord.add("111", "Matt"))
    assert res.status_code == 200
    body = res.json()
    assert body["accessToken"] == "tok-111"
    assert (body["id"], body["name"]) == ("111", "Matt")


def test_everyone_in_an_activity_shares_one_room(client, discord, db_path):
    matt = _session(client, discord, "111", "Matt")
    alex = _session(client, discord, "222", "Alex")
    first = _room(client, matt).json()
    assert [m["name"] for m in first["members"]] == ["Matt"]
    assert any(d["name"] == "Reign of Dragons" for d in first["decks"]), "everyone plays the owner's decks"
    assert all(d["commander"] for d in first["decks"])
    assert [o["id"] for o in first["roster"]] == ["neyali", "jeleva"]

    view = _room(client, alex, deckId=_deck(db_path)["id"]).json()
    assert view["you"] == "222"
    assert [(m["name"], m["deck"] and m["deck"]["name"]) for m in view["members"]] == [
        ("Matt", None), ("Alex", "Reign of Dragons")
    ]
    _room(client, matt, opponents=["neyali", "made-up"])
    assert _room(client, alex).json()["opponents"] == ["neyali"], "Alex sees the AI Matt added"
    other = _room(client, alex, instance="another-instance").json()
    assert [m["name"] for m in other["members"]] == ["Alex"], "another Activity is another room"


def test_starting_seats_everyone_with_a_deck(client, discord, db_path):
    matt = _session(client, discord, "111", "Matt")
    alex = _session(client, discord, "222", "Alex")
    sam = _session(client, discord, "333", "Sam")
    deck = _deck(db_path)["id"]
    _room(client, matt, deckId=deck)
    _room(client, alex, deckId=deck)
    _room(client, sam)  # here without a deck: watches
    _room(client, matt, opponents=["neyali"])
    started = _room(client, alex, start=True).json()
    assert started["error"] is None
    assert started["started"] == {"matchId": "m1", "ticket": "t11" * 4}
    spec = discord.forge.specs[-1]
    assert spec["returnUrl"] == "/", "back from the table is the Activity's lobby"
    assert [(s["kind"], s.get("name"), s.get("person")) for s in spec["seats"]] == [
        ("human", "Matt", "discord:111"), ("human", "Alex", "discord:222"), ("ai", None, None)
    ]
    assert _room(client, matt).json()["started"] == {"matchId": "m1", "ticket": "t10" * 4}
    assert _room(client, sam).json()["started"] == {"matchId": "m1", "ticket": None}
    conn = db.connect(db_path)
    row = conn.execute("SELECT host_id, deck_name, opponents, status FROM play_games").fetchone()
    conn.close()
    assert (row["host_id"], row["deck_name"], row["status"]) == (1, "Reign of Dragons", "playing")
    assert [o["name"] for o in json.loads(row["opponents"])] == ["Alex", "Neyali"]
    assert _room(client, matt, start=True).json()["started"]["matchId"] == "m1", "one game per room"


def test_a_room_picks_again_once_its_game_is_over(client, discord, db_path):
    matt = _session(client, discord, "111", "Matt")
    _room(client, matt, deckId=_deck(db_path)["id"], opponents=["neyali"])
    assert _room(client, matt, start=True).json()["started"]["matchId"] == "m1"
    result = {"matchId": "m1", "status": "finished"}
    res = client.post("/play/results", json=result, headers={"Authorization": f"Bearer {SECRET}"})
    assert res.status_code == 204
    assert _room(client, matt).json()["started"] is None


def test_a_room_needs_two_seats_and_a_deck(client, discord, db_path):
    matt = _session(client, discord, "111", "Matt")
    assert _room(client, matt, start=True).json()["error"] == "Pick a deck first"
    _room(client, matt, deckId=_deck(db_path)["id"])
    assert _room(client, matt, start=True).json()["error"].startswith("A game needs 2 to 4 players")
    me = _room(client, matt, deckId=999999).json()["members"][0]
    assert me["deck"]["name"] == "Reign of Dragons", "a deck that isn't offered is ignored"
    assert client.post("/discord/api/room", json={}, headers=_bearer(matt)).status_code == 400
    assert _room(client, matt, instance="../etc").status_code == 400


def test_a_room_forgets_people_who_left(client, discord, monkeypatch):
    matt = _session(client, discord, "111", "Matt")
    alex = _session(client, discord, "222", "Alex")
    _room(client, alex)
    later = time.time() + rooms.STALE + 1
    monkeypatch.setattr(rooms.time, "time", lambda: later)
    assert [m["name"] for m in _room(client, matt).json()["members"]] == ["Matt"]


def test_sign_in_is_refused_without_a_good_code_or_membership(client, discord):
    assert _sign_in(client, "made-up").status_code == 401
    assert _sign_in(client, "").status_code == 401
    res = _sign_in(client, discord.add("222", "Stranger", member=False))
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "not_member"
    code = discord.add("333", "Twice")
    assert _sign_in(client, code).status_code == 200
    assert _sign_in(client, code).status_code == 401, "Discord takes a code once"


def test_the_api_needs_a_session(client, discord):
    assert client.post("/discord/api/room", json={"instanceId": INSTANCE}).status_code == 401
    assert _room(client, "nonsense").status_code == 401
    good = _session(client, discord, "111", "Matt")
    sig = good.split(".")[1]
    forged = auth._b64(json.dumps({"d": "999", "n": "x", "exp": 9_999_999_999}).encode()) + "." + sig
    assert _room(client, forged).status_code == 401


def test_sessions_expire():
    d = auth.Discord("id", "secret", GUILD)
    now = time.time()
    token = d.session("111", "Matt", now=now)
    assert d.read_session(token, now=now + 60)["d"] == "111"
    assert d.read_session(token, now=now + auth.SESSION_SECONDS + 1) is None
    assert auth.Discord("id", "other secret", GUILD).read_session(token, now=now) is None


def test_sign_ins_are_rate_limited(client, discord, monkeypatch):
    monkeypatch.setattr(auth, "EXCHANGES", auth.Limiter(limit=2))
    assert _sign_in(client, "a").status_code == 401
    assert _sign_in(client, "b").status_code == 401
    assert _sign_in(client, "c").status_code == 429
    assert len(discord.exchanges) == 2


def test_only_discord_paths_skip_access(db_path, discord, signer):  # noqa: F811
    client = _cf_client(db_path, signer)
    assert client.get("/discord/").status_code == 200
    assert client.get("/discord/static/activity.js").status_code == 200
    assert client.post("/discord/api/room", json={"instanceId": INSTANCE}).status_code == 401
    session = _session(client, discord, "111", "Matt")
    assert _room(client, session).status_code == 200
    for path in ("/", "/play", "/decks", "/api/v1/me", "/discordx", "/profile"):
        assert client.get(path).status_code == 401, path


def test_the_api_takes_calls_from_discords_proxy_origin(client, discord):
    proxy = {"Origin": "https://662512381221535764.discordsays.com", "Sec-Fetch-Site": "cross-site"}
    res = client.post("/discord/api/session", json={"code": discord.add("111", "Matt")}, headers=proxy)
    assert res.status_code == 200
    headers = {**proxy, **_bearer(res.json()["session"])}
    room = client.post("/discord/api/room", json={"instanceId": INSTANCE}, headers=headers)
    assert room.status_code == 200
    other = client.post("/profile", data={"display_name": "x"}, headers=proxy)
    assert other.status_code == 403, "the rest of the tracker, which rides on cookies, refuses other origins"


def test_without_discord_configured(client, monkeypatch, cache_dir):
    for key in ("DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_GUILD_ID"):
        monkeypatch.delenv(key, raising=False)
    assert 'data-client-id=""' in client.get("/discord/").text
    assert client.post("/discord/api/session", json={"code": "x"}).status_code == 503
    assert client.post("/discord/api/room", json={"instanceId": INSTANCE}).status_code == 401
