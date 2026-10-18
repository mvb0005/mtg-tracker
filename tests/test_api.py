"""The JSON API under /api/v1: errors, reads, the one-request deck edit,
dry runs that leave no trace, imports and their undo, OpenAPI."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from mtgtracker import db, restore
from mtgtracker.web.app import create_app
from mtgtracker.web.auth import DEV_COOKIE, AuthConfig
from tests.conftest import install_transport
from tests.test_collection_csv import MANABOX
from tests.test_commanders import typed_cache  # noqa: F401
from tests.test_multiuser import AUD, TEAM, _FakeJWKS, two_users  # noqa: F401
from tests.test_system_user import CLIENT_ID
from tests.test_system_user import world as _world

API = "/api/v1"
world = _world  # the system-user world: two users, a friend's deck, Claude with a credential

RHYSTIC = {"id": "planned-1", "name": "Rhystic Study", "set": "pcy", "collector_number": "45",
           "finishes": ["nonfoil"], "type_line": "Enchantment", "cmc": 3, "colors": ["U"],
           "color_identity": ["U"], "image_uris": {"normal": "https://img/rhystic.jpg"}}
SAHEELI = {"id": "sah", "name": "Saheeli, the Sun's Brilliance", "set": "mkc", "collector_number": "1",
           "type_line": "Legendary Creature — Human Artificer", "cmc": 2, "colors": ["U", "R"],
           "color_identity": ["U", "R"], "mana_cost": "{U}{R}", "image_uris": {"normal": "https://img/sah.jpg"}}
ISLAND = {"id": "isl-id", "name": "Island", "set": "fdc", "collector_number": "9", "finishes": ["nonfoil"],
          "type_line": "Basic Land — Island", "cmc": 0, "colors": [], "color_identity": []}
CARDS = {c["name"].lower(): c for c in (RHYSTIC, SAHEELI, ISLAND)}


@pytest.fixture
def scryfall_api(monkeypatch, cache_dir):
    """Scryfall: /cards/named and /cards/collection answer for the two cards
    above, 404 for anything else."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cards/collection":
            idents = json.loads(request.content)["identifiers"]
            found = [CARDS[i["name"].lower()] for i in idents if i.get("name", "").lower() in CARDS]
            return httpx.Response(200, json={"data": found, "not_found": []})
        if request.url.path == "/cards/named":
            card = CARDS.get(request.url.params["fuzzy"].lower())
            if card:
                return httpx.Response(200, json=card)
        return httpx.Response(404, json={"object": "error", "details": "No cards found"})

    install_transport(monkeypatch, httpx.MockTransport(handler))


def person(world, user_id=1):
    client = TestClient(create_app(world["db_path"], auth=AuthConfig(mode="dev")))
    client.cookies.set(DEV_COOKIE, str(user_id))
    return client


def system(world, act_for=1, **headers):
    client = TestClient(create_app(world["db_path"], auth=AuthConfig(mode="dev")))
    client.headers.update({"X-Dev-Service": CLIENT_ID, "X-Act-For": str(act_for), **headers})
    return client


def snapshot(world, *, skip=(), restore_dir=True):
    """Every row of every table, plus the restore-point files: equal before
    and after means nothing at all was written. (A write that fails after
    its first change has still taken a restore point, so those tests pass
    restore_dir=False.)"""
    conn = db.connect(world["db_path"])
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    state = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]
             for t in tables if t not in skip}
    if "changelog" in state:  # a skipped table's own write-log rows go with it
        state["changelog"] = [r for r in state["changelog"] if r[3] not in skip]
    conn.close()
    if restore_dir:
        state["restore dir"] = sorted(p.name for p in restore.restore_dir(world["db_path"]).glob("*"))
    return state


def rows(world, sql, *args):
    conn = db.connect(world["db_path"])
    try:
        return [tuple(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def lot_of(world, deck_name, card):
    return rows(world, "SELECT lots.id FROM deck_cards JOIN lots ON lots.id = deck_cards.lot_id"
                " JOIN decks ON decks.id = deck_cards.deck_id WHERE decks.name = ? AND lots.card_name = ?"
                " ORDER BY lots.id", deck_name, card)[0][0]


def error(resp):
    assert set(resp.json()) >= {"error"}, resp.text
    return resp.json()["error"]


# --- JSON errors, only under /api/ -------------------------------------------------

def test_api_errors_are_json(world):
    client = person(world)
    keen = world["keen"]
    for resp, status, code in [
        (client.get(f"{API}/nope"), 404, "not_found"),
        (client.get(f"{API}/decks/99999"), 404, "not_found"),
        (client.post(f"{API}/me"), 405, "method_not_allowed"),
        (client.get(f"{API}/decks/abc"), 422, "validation_error"),
        (client.post(f"{API}/decks", json={"name": "x"}), 422, "validation_error"),
        (person(world, world["friend"].id).get(f"{API}/decks/{keen}/notes"), 403, "forbidden"),
    ]:
        assert resp.status_code == status, resp.text
        err = error(resp)
        assert err["code"] == code
        assert err["message"]
    assert client.post(f"{API}/me").headers["allow"] == "GET"


def test_html_errors_are_unchanged(world):
    client = person(world)
    assert client.get("/nope").json() == {"detail": "Not Found"}
    assert client.get("/decks/99999").json() == {"detail": "No such deck"}
    assert client.get("/decks/abc").json()["detail"][0]["type"] == "int_parsing"
    cross = client.post("/", headers={"Origin": "https://evil.example"})
    assert cross.text == "Cross-origin request rejected"


def test_cross_origin_post_is_a_json_403_but_scripts_without_origin_pass(world):
    client = person(world)
    bad = client.post(f"{API}/decks/{world['keen']}/notes", json={"notes": "x"})  # PUT only: 405, not 403
    assert bad.status_code == 405
    resp = client.put(f"{API}/decks/{world['keen']}/notes", json={"notes": "x"},
                      headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403
    assert error(resp)["code"] == "cross_origin"
    assert client.put(f"{API}/decks/{world['keen']}/notes", json={"notes": "x"}).status_code == 200


def test_signed_out_api_request_is_a_json_401(world):
    app = create_app(world["db_path"], auth=AuthConfig(mode="cloudflare", team_domain=TEAM, audience=AUD),
                     jwks_client=_FakeJWKS(None))
    client = TestClient(app)
    api = client.get(f"{API}/me")
    assert api.status_code == 401
    assert error(api)["code"] == "unauthorized"
    assert client.get(f"{API}/openapi.json").status_code == 401
    page = client.get("/")
    assert page.status_code == 401
    assert "text/html" in page.headers["content-type"]


def test_openapi_lists_the_api_paths(world):
    resp = person(world).get(f"{API}/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    assert {f"{API}/me", f"{API}/decks/{{deck_id}}/changes", f"{API}/cards/owners",
            f"{API}/imports/{{import_id}}"} <= set(paths)
    assert all(p.startswith(API) for p in paths)  # none of the HTML pages
    assert person(world).get("/docs").status_code == 404


# --- identity -----------------------------------------------------------------------

def test_me_for_a_person_and_a_system_actor(world):
    me = person(world).get(f"{API}/me").json()
    assert me["principal"] == {"id": 1, "name": "Owner", "kind": "person", "is_admin": True}
    assert me["actor"] == me["principal"]
    sys_me = system(world, act_for=world["friend"].id).get(f"{API}/me").json()
    assert sys_me["principal"]["id"] == world["friend"].id
    assert sys_me["principal"]["kind"] == "person"
    assert sys_me["actor"]["kind"] == "system"
    assert sys_me["actor"]["name"] == "Claude"
    assert system(world).get(f"{API}/me").json()["principal"]["is_admin"] is True


def test_system_actor_acts_only_within_its_principal(world):
    keen, alex = world["keen"], world["friend_deck"]
    claude = system(world, act_for=1)
    assert claude.get(f"{API}/decks/{keen}").status_code == 200
    assert claude.put(f"{API}/decks/{keen}/notes", json={"notes": "mine"}).status_code == 200
    assert claude.put(f"{API}/decks/{alex}/notes", json={"notes": "no"}).status_code == 403
    body = {"adds": [{"name": "x"}], "expect_version": 1}
    assert claude.post(f"{API}/decks/{alex}/changes", json=body).status_code == 403
    as_alex = system(world, act_for=world["friend"].id)
    assert as_alex.put(f"{API}/decks/{keen}/notes", json={"notes": "no"}).status_code == 403
    assert as_alex.put(f"{API}/decks/{alex}/notes", json={"notes": "ok"}).status_code == 200


# --- reads -----------------------------------------------------------------------------

def test_deck_list_and_detail(world):
    keen = world["keen"]
    decks = person(world).get(f"{API}/decks").json()["decks"]
    deck = next(d for d in decks if d["id"] == keen)
    assert deck["name"] == "Keen Engineering"
    assert deck["owner_id"] == 1
    assert deck["version"] == 1
    counts = deck["counts"]
    assert counts["list"] == counts["here"] + counts["away"] + counts["wanted"]
    assert "notes" not in deck
    friend_view = person(world, world["friend"].id).get(f"{API}/decks?owner=1").json()
    assert {d["id"] for d in friend_view["decks"]} == {d["id"] for d in decks}
    assert person(world).get(f"{API}/decks?owner=999").status_code == 404

    detail = person(world).get(f"{API}/decks/{keen}").json()
    assert detail["id"] == keen
    assert detail["commander"]
    assert detail["pending_changes"] == []
    assert len(detail["cards"]) == deck["counts"]["list"]
    card = detail["cards"][0]
    assert {"lot_id", "name", "scryfall_id", "set", "collector_number", "finish", "equipped_here",
            "equipped_in", "cost_cents"} <= set(card)
    assert all(c["equipped_here"] and c["equipped_in"]["id"] == keen for c in detail["cards"])
    expected = dict(rows(world, "SELECT id, allocated_cost_cents FROM lots WHERE id IN"
                         " (SELECT lot_id FROM deck_cards WHERE deck_id = ?)", keen))
    assert {c["lot_id"]: c["cost_cents"] for c in detail["cards"]} == expected
    assert any(v for v in expected.values())


def test_a_friend_reads_a_deck_without_money_notes_or_the_queue(world):
    keen = world["keen"]
    person(world).put(f"{API}/decks/{keen}/notes", json={"notes": "secret plan"})
    friend = person(world, world["friend"].id)
    detail = friend.get(f"{API}/decks/{keen}")
    assert detail.status_code == 200
    assert "cost" not in detail.text
    assert "secret plan" not in detail.text
    assert detail.json()["pending_changes"] is None
    assert friend.get(f"{API}/decks/{keen}/notes").status_code == 403
    assert person(world).get(f"{API}/decks/{keen}/notes").json()["notes"] == "secret plan"
    # Own lots carry their cost; the friend's deck read by its owner shows it too.
    mine = friend.get(f"{API}/decks/{world['friend_deck']}").json()
    assert mine["cards"][0]["cost_cents"] == 999
    assert "cost_cents" not in person(world).get(f"{API}/decks/{world['friend_deck']}").json()["cards"][0]


def test_deck_shows_friends_copies_of_wanted_cards(world, scryfall_api):
    created = person(world).post(f"{API}/decks", json={
        "name": "Wishlist", "decklist": "Commander\n1 Saheeli, the Sun's Brilliance\nDeck\n1 Rhystic Study"})
    assert created.status_code == 201, created.text
    wants = {w["name"]: w for w in created.json()["wants"]}
    assert wants["Rhystic Study"]["friends"][0]["name"] == "Alex"


def test_card_owners_match_by_front_face_and_carry_no_money(world):
    conn = db.connect(world["db_path"])
    conn.execute("INSERT INTO lots (owner_id, card_name, acquired_date, allocated_cost_cents)"
                 " VALUES (?, 'Delver of Secrets // Insectile Aberration', '2026-10-01', 500)",
                 (world["friend"].id,))
    conn.commit()
    conn.close()
    resp = person(world).get(f"{API}/cards/owners", params=[
        ("name", "Delver of Secrets"), ("name", "Counterspell"), ("name", "No Such Card")])
    assert resp.status_code == 200
    by_name = {c["name"]: c for c in resp.json()["cards"]}
    assert [(c["owner"], c["where"]) for c in by_name["Delver of Secrets"]["copies"]] == [
        ("Alex", "not in a deck")]
    spell = {(c["owner"], c["where"]): c["qty"] for c in by_name["Counterspell"]["copies"]}
    assert spell[("Alex", "not in a deck")] == 1
    assert any(o == "Owner" for o, _ in spell)
    assert by_name["No Such Card"]["copies"] == []
    assert "cost" not in resp.text
    assert "cents" not in resp.text
    # A DFC asked for by its full name finds a lot recorded by front face only.
    front = person(world).get(f"{API}/cards/owners", params={"name": "Rhystic Study // Anything"})
    assert front.json()["cards"][0]["total"] == 1


def test_collection_costs_are_private(world):
    own = person(world).get(f"{API}/collection").json()
    assert own["count"] > 50
    assert all("cost_cents" in lot for lot in own["lots"])
    assert {"lot_id", "name", "set", "finish", "where", "import_id"} <= set(own["lots"][0])
    theirs = person(world, world["friend"].id).get(f"{API}/collection?owner=1")
    assert theirs.status_code == 200
    assert "cost" not in theirs.text


def test_audit_and_restore_points(world):
    owner = person(world)
    owner.put(f"{API}/decks/{world['keen']}/notes", json={"notes": "hello"})
    activity = owner.get(f"{API}/audit?limit=5").json()["activity"]
    assert activity[0]["action"] == "put_notes"
    assert activity[0]["summary"]["notice"] == "Notes saved."
    assert activity[0]["target"] == {"deck_id": str(world["keen"])}
    friend = person(world, world["friend"].id)
    assert friend.get(f"{API}/audit").json()["activity"] == []
    points = owner.get(f"{API}/restore-points")
    assert points.status_code == 200
    assert points.json()["restore_points"][0]["name"].endswith(".db.gz")
    assert friend.get(f"{API}/restore-points").status_code == 403
    assert system(world, act_for=1).get(f"{API}/restore-points").status_code == 200
    assert system(world, act_for=world["friend"].id).get(f"{API}/restore-points").status_code == 403


def test_exports_and_versions(world):
    keen = world["keen"]
    friend = person(world, world["friend"].id)
    export = friend.get(f"{API}/decks/{keen}/export/decklist").json()
    assert export["kind"] == "decklist"
    assert "Commander" in export["text"]
    assert friend.get(f"{API}/decks/{keen}/export/nope").status_code == 404
    version = friend.get(f"{API}/decks/{keen}/versions/1").json()
    assert version["version"] == 1
    assert version["card_count"] == len(version["cards"])
    assert "cost" not in json.dumps(version)
    assert friend.get(f"{API}/decks/{keen}/versions/9").status_code == 404


# --- the one-request edit -----------------------------------------------------------------

def edit_body(world, **over):
    reign_card = rows(world, "SELECT lots.id, lots.card_name FROM deck_cards JOIN lots ON lots.id ="
                      " deck_cards.lot_id JOIN decks ON decks.id = deck_cards.deck_id"
                      " WHERE decks.name = 'Reign of Dragons' AND lots.card_name = 'Chaos Warp'")[0]
    body = {"adds": [{"name": "Rhystic Study"}, {"lot_id": reign_card[0]}],
            "cuts": [{"name": "Counterspell"}, {"name": "Island"}], "note": "swap two",
            "expect_version": 1}
    return {**body, **over}, reign_card[1]


def test_changes_dry_run_equals_the_real_run_and_writes_nothing(world, scryfall_api):
    client, keen = person(world), world["keen"]
    body, lot_name = edit_body(world)
    before = snapshot(world)
    dry = client.post(f"{API}/decks/{keen}/changes?dry_run=1", json=body)
    assert dry.status_code == 200, dry.text
    assert snapshot(world) == before  # no data, audit row, write log or restore point
    real = client.post(f"{API}/decks/{keen}/changes", json=body)
    assert real.status_code == 200, real.text
    dry_body = dry.json()
    assert dry_body.pop("dry_run") is True
    assert dry_body == real.json()
    result = real.json()
    assert result["version"] == 2
    assert result["previous_version"] == 1
    assert result["added"] == sorted([lot_name, "Rhystic Study"])
    assert result["removed"] == ["Counterspell", "Island"]
    assert result["counts"]["wanted"] == 1
    # one new version, and the write log shares the request's audit batch
    assert rows(world, "SELECT version FROM decks WHERE id = ?", keen) == [(2,)]
    [(audit_id,)] = rows(world, "SELECT MAX(id) FROM audit_log")
    last_seq = max(r[0] for r in before["changelog"])
    batches = {b for (b,) in rows(world, "SELECT DISTINCT batch_id FROM changelog WHERE seq > ?"
                                  " AND tbl != 'audit_log'", last_seq)}
    assert batches == {audit_id}  # the audit row's own log lines carry no batch
    assert rows(world, "SELECT path, action FROM audit_log WHERE id = ?", audit_id) == [
        (f"{API}/decks/{keen}/changes", "apply_changes")]
    assert len(restore.list_points(restore.restore_dir(world["db_path"]))) == 1
    assert len(rows(world, "SELECT 1 FROM deck_versions WHERE deck_id = ?", keen)) == 2


def test_a_system_actors_dry_run_leaves_nothing_either(world, scryfall_api):
    body, _ = edit_body(world)
    before = snapshot(world, skip=("service_credentials",))  # last_used_at is identity bookkeeping
    dry = system(world).post(f"{API}/decks/{world['keen']}/changes?dry_run=true", json=body)
    assert dry.status_code == 200
    assert dry.json()["dry_run"] is True
    assert snapshot(world, skip=("service_credentials",)) == before


def test_changes_refusals(world, scryfall_api):
    client, keen = person(world), world["keen"]
    body, _ = edit_body(world)
    before = snapshot(world)

    stale = client.post(f"{API}/decks/{keen}/changes", json={**body, "expect_version": 7})
    assert stale.status_code == 409
    assert error(stale)["code"] == "version_conflict"
    assert error(stale)["current_version"] == 1

    pure = client.post(f"{API}/decks/{keen}/changes", json={
        "adds": [{"name": "Rhystic Study"}], "cuts": [{"name": "Island"}, {"name": "Island"}],
        "expect_version": 1})
    assert pure.status_code == 422
    assert error(pure)["code"] == "pure_cuts_unsupported"
    assert "insert" in error(pure)["message"]

    other = client.post(f"{API}/decks/{keen}/changes", json={
        "adds": [{"lot_id": world["friend_lots"][1]}], "expect_version": 1})
    assert other.status_code == 403
    cut_other = client.post(f"{API}/decks/{keen}/changes", json={
        "adds": [{"name": "Rhystic Study"}], "cuts": [{"lot_id": world["friend_lots"][0]}],
        "expect_version": 1})
    assert cut_other.status_code == 403

    for bad in (
        {"adds": [{}], "expect_version": 1},
        {"adds": [{"name": "a", "lot_id": 1}], "expect_version": 1},
        {"adds": [{"name": "Rhystic Study"}], "cuts": [{"name": "Nonexistent Card"}], "expect_version": 1},
    ):
        assert client.post(f"{API}/decks/{keen}/changes", json=bad).status_code == 422
    assert snapshot(world) == before


def test_changes_are_all_or_nothing(world, scryfall_api):
    """The second add can't be found on Scryfall: the first stays unqueued and
    the deck is untouched."""
    client, keen = person(world), world["keen"]
    before = snapshot(world, restore_dir=False)
    resp = client.post(f"{API}/decks/{keen}/changes", json={
        "adds": [{"name": "Rhystic Study"}, {"name": "Totally Made Up"}],
        "cuts": [{"name": "Island"}, {"name": "Island"}], "expect_version": 1})
    assert resp.status_code == 422
    assert "Totally Made Up" in error(resp)["message"]
    assert snapshot(world, restore_dir=False) == before


def test_pending_modify_page_changes_are_never_applied_silently(world, scryfall_api):
    client, keen = person(world), world["keen"]
    queued = client.post(f"/decks/{keen}/changes", data={"planned_name": "Rhystic Study"},
                         follow_redirects=False)
    assert queued.status_code == 303
    detail = client.get(f"{API}/decks/{keen}").json()
    assert detail["pending_changes"][0]["add"]["name"] == "Rhystic Study"
    assert detail["pending_changes"][0]["add"]["owned"] is False
    assert "cost" not in json.dumps(detail["pending_changes"])
    body, _ = edit_body(world)
    before = snapshot(world)
    resp = client.post(f"{API}/decks/{keen}/changes", json=body)
    assert resp.status_code == 409
    assert error(resp)["code"] == "pending_changes"
    assert snapshot(world) == before


def test_name_cuts_prefer_wants_then_away_copies_and_never_repeat(world, scryfall_api):
    client = person(world)
    deck = client.post(f"{API}/decks", json={
        "name": "Mixed", "decklist": "Deck\n1 Rhystic Study\n2 Island"}).json()
    assert "Rhystic Study" in [w["name"] for w in deck["wants"]]
    res = client.post(f"{API}/decks/{deck['id']}/changes", json={
        "adds": [{"name": "Saheeli, the Sun's Brilliance"}, {"name": "Saheeli, the Sun's Brilliance"}],
        "cuts": [{"name": "Rhystic Study"}, {"name": "Island"}], "expect_version": 1})
    assert res.status_code == 200, res.text
    assert res.json()["removed"] == ["Island", "Rhystic Study"]
    again = client.post(f"{API}/decks/{deck['id']}/changes", json={
        "adds": [{"name": "Rhystic Study"}], "cuts": [{"name": "Rhystic Study"}], "expect_version": 2})
    assert again.status_code == 422  # no copy left to cut


# --- decks, notes, commander -------------------------------------------------------------------

WISHLIST = "Commander\n1 Saheeli, the Sun's Brilliance\nDeck\n1 Counterspell\n2 Island"


def test_create_deck_from_a_list(world, scryfall_api):
    client = person(world)
    before = snapshot(world)
    dry = client.post(f"{API}/decks?dry_run=1", json={
        "name": "Wishlist", "decklist": WISHLIST})
    assert dry.status_code == 201
    assert dry.json()["dry_run"] is True
    assert snapshot(world) == before
    resp = client.post(f"{API}/decks", json={
        "name": "Wishlist", "decklist": WISHLIST})
    assert resp.status_code == 201, resp.text
    deck = resp.json()
    assert (deck["name"], deck["version"]) == ("Wishlist", 1)
    assert deck["commander"] == "Saheeli, the Sun's Brilliance"
    assert "Saheeli, the Sun's Brilliance" in [w["name"] for w in deck["wants"]]
    assert len(deck["cards"]) + len(deck["wants"]) == 4
    assert all(not c["equipped_here"] for c in deck["cards"])  # linked, not moved
    assert client.get(f"{API}/decks/{deck['id']}").json()["counts"] == deck["counts"]
    assert client.post(f"{API}/decks", json={"name": " ", "decklist": "1 Island"}).status_code == 422


def test_notes_put_and_get(world):
    client, keen = person(world), world["keen"]
    before = snapshot(world)
    dry = client.put(f"{API}/decks/{keen}/notes?dry_run=1", json={"notes": "  plan \r\n"})
    assert dry.json() == {"deck_id": keen, "notes": "plan", "notice": "Notes saved.", "dry_run": True}
    assert snapshot(world) == before
    assert client.put(f"{API}/decks/{keen}/notes", json={"notes": "plan"}).json()["notes"] == "plan"
    assert client.get(f"{API}/decks/{keen}/notes").json()["notes"] == "plan"
    too_long = client.put(f"{API}/decks/{keen}/notes", json={"notes": "x" * 20_001})
    assert too_long.status_code == 422
    assert "limited" in error(too_long)["message"]
    assert client.put(f"{API}/decks/{keen}/notes", json={"notes": ""}).json()["notice"] == "Notes cleared."
    assert client.get(f"{API}/decks/{keen}/notes").json()["notes"] == ""
    assert person(world, world["friend"].id).put(
        f"{API}/decks/{keen}/notes", json={"notes": "x"}).status_code == 403


def test_commander_set_clear_and_refusals(world, typed_cache):  # noqa: F811
    client, keen = person(world), world["keen"]
    sai = "Sai, Master Thopterist"
    assert client.post(f"{API}/decks/{keen}/commander", json={"name": None}).json()["commander"] is None
    assert client.get(f"{API}/decks/{keen}").json()["commander"] is None
    assert client.post(f"{API}/decks/{keen}/commander", json={"name": sai}).json()["commander"] == sai
    client.post(f"{API}/decks/{keen}/commander", json={"name": None})
    lot = lot_of(world, "Keen Engineering", sai)
    assert client.post(f"{API}/decks/{keen}/commander", json={"lot_id": lot}).json()["commander"] == sai
    assert client.post(f"{API}/decks/{keen}/commander", json={"name": "Island"}).status_code == 422
    assert client.post(f"{API}/decks/{keen}/commander", json={}).status_code == 422
    theirs = client.post(f"{API}/decks/{keen}/commander", json={"lot_id": world["friend_lots"][0]})
    assert theirs.status_code == 403
    assert person(world, world["friend"].id).post(
        f"{API}/decks/{keen}/commander", json={"name": None}).status_code == 403


# --- collection imports -------------------------------------------------------------------------

def test_import_by_multipart_and_by_json(world, scryfall_api):
    client = person(world)
    csv_one = MANABOX
    csv_two = MANABOX.replace(",3,1,", ",1,1,")  # a different file
    before = snapshot(world)
    dry = client.post(f"{API}/people/1/imports?dry_run=1", files={"file": ("a.csv", csv_one, "text/csv")})
    assert dry.status_code == 201
    assert dry.json()["dry_run"] is True
    assert snapshot(world) == before
    first = client.post(f"{API}/people/1/imports", files={"file": ("a.csv", csv_one, "text/csv")})
    assert first.status_code == 201, first.text
    assert first.json()["copies"] == 4
    assert first.json()["cost_cents"] == 3 * 35 + 110
    second = client.post(f"{API}/people/1/imports", json={"csv": csv_two, "filename": "b.csv"})
    assert second.status_code == 201
    assert second.json()["copies"] == 2
    dupe = client.post(f"{API}/people/1/imports", json={"csv": csv_two, "filename": "b.csv"})
    assert dupe.status_code == 422
    assert "already imported" in error(dupe)["message"]
    listing = client.get(f"{API}/people/1/imports").json()["imports"]
    assert [i["filename"] for i in listing] == ["b.csv", "a.csv"]
    assert client.post(f"{API}/people/1/imports", json={"nothing": 1}).status_code == 422
    assert client.post(f"{API}/people/1/imports", files={"other": ("a.csv", "x")}).status_code == 422
    # people may only import into their own collection; a system actor may act for anyone
    assert person(world, world["friend"].id).post(
        f"{API}/people/1/imports", json={"csv": "Name\nIsland\n"}).status_code == 403
    alex = system(world, act_for=1).post(
        f"{API}/people/{world['friend'].id}/imports", json={"csv": "Name\nIsland\n", "filename": "i.csv"})
    assert alex.status_code == 201
    assert alex.json()["owner_id"] == world["friend"].id
    assert "cost_cents" not in alex.json()
    imported = rows(world, "SELECT owner_id, imported_by FROM collection_imports"
                    " WHERE original_filename = 'i.csv'")
    assert imported == [(world["friend"].id, world["claude"].id)]


def _import_id(client, csv=MANABOX, filename="a.csv", person_id=1):
    resp = client.post(f"{API}/people/{person_id}/imports", json={"csv": csv, "filename": filename})
    assert resp.status_code == 201, resp.text
    return resp.json()["import_id"]


def test_a_person_undoes_an_unused_import(world):
    client = person(world)
    import_id = _import_id(client)
    lots_before = rows(world, "SELECT COUNT(*) FROM lots")[0][0]
    dry = client.delete(f"{API}/imports/{import_id}?dry_run=1")
    assert dry.status_code == 200
    assert dry.json()["dry_run"] is True
    assert dry.json()["preview"] | {} == {
        "action": "delete_import", "import_id": import_id, "owner_id": 1, "owner": "Owner",
        "filename": "a.csv", "format": "ManaBox", "lots": 4,
        "effect": dry.json()["preview"]["effect"]}
    assert rows(world, "SELECT COUNT(*) FROM lots")[0][0] == lots_before
    done = client.delete(f"{API}/imports/{import_id}")
    assert done.status_code == 200
    assert done.json()["notice"].startswith("Removed the import")
    assert rows(world, "SELECT COUNT(*) FROM lots")[0][0] == lots_before - 4
    assert rows(world, "SELECT COUNT(*) FROM collection_imports WHERE id = ?", import_id) == [(0,)]
    assert client.delete(f"{API}/imports/{import_id}").status_code == 404
    _import_id(client)  # the same file imports again


def test_a_system_actor_must_confirm_deleting_an_import(world):
    claude = system(world)
    import_id = _import_id(claude)
    lots = rows(world, "SELECT COUNT(*) FROM lots")[0][0]
    first = claude.delete(f"{API}/imports/{import_id}")
    assert first.status_code == 409
    assert error(first)["code"] == "confirm_required"
    preview, token = first.json()["preview"], first.json()["confirm"]
    assert (preview["lots"], preview["owner"], preview["filename"]) == (4, "Owner", "a.csv")
    assert rows(world, "SELECT COUNT(*) FROM lots")[0][0] == lots
    # a dry run needs no token and changes nothing
    assert claude.delete(f"{API}/imports/{import_id}?dry_run=1").json()["preview"] == preview
    done = claude.delete(f"{API}/imports/{import_id}", params={"confirm": token})
    assert done.status_code == 200, done.text
    assert rows(world, "SELECT COUNT(*) FROM lots")[0][0] == lots - 4
    [(summary,)] = rows(world, "SELECT summary_json FROM audit_log WHERE action = 'delete_import'")
    assert json.loads(summary)["preview"] == preview
    assert claude.delete(f"{API}/imports/{import_id}", params={"confirm": token}).status_code == 404


def test_only_the_owner_may_undo_and_an_import_in_use_is_refused(world):
    client = person(world)
    import_id = _import_id(client)
    assert person(world, world["friend"].id).delete(f"{API}/imports/{import_id}").status_code == 403
    [(lot,)] = rows(world, "SELECT MIN(id) FROM lots WHERE collection_import_id = ?", import_id)
    conn = db.connect(world["db_path"])
    conn.execute("INSERT INTO deck_cards (deck_id, lot_id, equipped) VALUES (?, ?, 0)", (world["keen"], lot))
    conn.commit()
    conn.close()
    before = snapshot(world, restore_dir=False)  # a failed risky action still took its restore point
    blocked = client.delete(f"{API}/imports/{import_id}")
    assert blocked.status_code == 409
    assert error(blocked)["code"] == "import_in_use"
    assert error(blocked)["blockers"] == {"on_deck_lists": 1}
    assert snapshot(world, restore_dir=False) == before
    sys_blocked = system(world).delete(f"{API}/imports/{import_id}")
    assert error(sys_blocked)["code"] == "import_in_use"  # refused before it asks to confirm


def test_unknown_api_paths_are_json_404s(world):
    resp = person(world).get("/api/v1/")
    assert resp.status_code == 404
    assert error(resp)["code"] == "not_found"
