"""System users: service credentials, principal vs. actor (X-Act-For),
confirmed dry runs for risky actions, and the audit log."""

import base64
import json
import sqlite3
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from mtgtracker import audit, confirm, db, users
from mtgtracker.confirm import risky  # noqa: F401  (re-exported for the registry test)
from mtgtracker.web import middleware
from mtgtracker.web.app import create_app
from mtgtracker.web.auth import DEV_COOKIE, AuthConfig
from tests.test_multiuser import AUD, TEAM, _FakeJWKS, two_users  # noqa: F401

CLIENT_ID = "abc123.access"
ADD = {"display_name": "Sam", "email": "sam@example.com"}


@pytest.fixture
def world(db_path, cache_dir, two_users):  # noqa: F811
    conn = db.connect(db_path)
    claude = users.create_system(conn, "Claude")
    users.add_credential(conn, claude.id, CLIENT_ID, "test token")
    conn.close()
    return {**two_users, "claude": claude, "db_path": db_path}


def _dev(world, **headers):
    """A dev-mode client speaking as the system user (X-Dev-Service)."""
    client = TestClient(create_app(world["db_path"], auth=AuthConfig(mode="dev")))
    client.headers.update({"X-Dev-Service": CLIENT_ID, **headers})
    return client


def _count(world, sql):
    conn = db.connect(world["db_path"])
    n = conn.execute(sql).fetchone()[0]
    conn.close()
    return n


def _people_count(world):
    return _count(world, "SELECT COUNT(*) FROM users")


# --- migration -------------------------------------------------------------

def test_migration_0008_on_a_populated_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "v7.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    for index, path in enumerate(db._migration_files()[:7], start=1):
        conn.executescript(path.read_text())
        conn.execute(f"PRAGMA user_version = {index}")
    conn.execute(
        "INSERT INTO users (id, email, display_name, created_at)"
        " VALUES (2, 'a@example.com', 'A', '2026-01-01')"
    )
    conn.execute("INSERT INTO decks (name, format, created_at, owner_id) VALUES ('D', 'Commander', 't', 2)")
    conn.commit()

    assert db.migrate(conn) == len(db._migration_files())
    rows = conn.execute("SELECT id, kind, disabled_at FROM users ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(1, "person", None), (2, "person", None)]
    assert conn.execute("SELECT owner_id FROM decks").fetchone()[0] == 2
    assert users.by_email(conn, "a@example.com").kind == "person"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    # The new tables are STRICT and the system-user rules hold in the schema too.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO users (email, display_name, created_at, kind)"
                     " VALUES ('x@y.zz', 'X', 't', 'system')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO users (display_name, created_at, kind) VALUES ('X', 't', 'robot')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO audit_log (at, actor_id, principal_id, method, path, action)"
                     " VALUES ('t', 1, 99, 'POST', '/', 'x')")
    conn.close()


def test_users_module_rules(db_path):
    conn = db.connect(db_path)
    claude = users.create_system(conn, "Claude")
    assert (claude.kind, claude.email, claude.is_admin, claude.disabled) == ("system", None, False, False)
    assert [u.id for u in users.list_users(conn)] == [1]  # people only
    with pytest.raises(users.UserError):
        users.add_credential(conn, 1, "x", "a person can't have one")
    with pytest.raises(users.UserError):
        users.add_credential(conn, claude.id, "  ", "label")
    cred = users.add_credential(conn, claude.id, "cid", "label")
    with pytest.raises(users.UserError):
        users.add_credential(conn, claude.id, "cid", "dup")
    assert users.credential_user(conn, "cid").id == claude.id
    assert users.list_credentials(conn)[0].last_used_at is not None
    users.set_disabled(conn, claude.id, True)
    assert users.credential_user(conn, "cid") is None
    users.set_disabled(conn, claude.id, False)
    users.revoke_credential(conn, cred.id)
    assert users.credential_user(conn, "cid") is None
    with pytest.raises(users.UserError):
        users.set_disabled(conn, 1, True)
    # Never reachable through the email path, even if a row were forced.
    conn.execute("DROP TRIGGER users_system_no_email_update")
    conn.execute("UPDATE users SET email = 'bot@example.com' WHERE id = ?", (claude.id,))
    assert users.by_email(conn, "bot@example.com") is None
    conn.close()


# --- Cloudflare service tokens ----------------------------------------------

@pytest.fixture
def jwt_signer():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def token(*, aud=AUD, iss=f"https://{TEAM}", exp_in=300, signing_key=None, **claims):
        body = {"aud": aud, "iss": iss, "exp": int(time.time()) + exp_in, **claims}
        return jwt.encode(body, signing_key or key, algorithm="RS256")

    token.public_key = key.public_key()
    return token


def _cf(world, signer):
    config = AuthConfig(mode="cloudflare", team_domain=TEAM, audience=AUD)
    return TestClient(create_app(world["db_path"], auth=config, jwks_client=_FakeJWKS(signer.public_key)),
                      client=("10.0.0.5", 50000))


def _hdr(token, act_for=None):
    out = {"Cf-Access-Jwt-Assertion": token}
    if act_for is not None:
        out["X-Act-For"] = str(act_for)
    return out


def test_service_token_is_a_system_actor(world, jwt_signer):
    client = _cf(world, jwt_signer)
    token = jwt_signer(common_name=CLIENT_ID, sub="")
    resp = client.get("/decks", headers=_hdr(token, 1))
    assert resp.status_code == 200
    assert "Keen Engineering" in resp.text
    resp = client.get("/decks", headers=_hdr(token))  # must say who it acts for
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "act_for_required"


def test_service_token_rejections(world, jwt_signer):
    client = _cf(world, jwt_signer)
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    good = {"common_name": CLIENT_ID}
    for token in (
        jwt_signer(common_name="unknown-client"),
        jwt_signer(),  # neither an email nor a common_name
        jwt_signer(common_name=""),
        jwt_signer(common_name="  "),
        jwt_signer(aud="someone-else", **good),
        jwt_signer(iss="https://evil.cloudflareaccess.com", **good),
        jwt_signer(exp_in=-60, **good),
        jwt_signer(signing_key=other_key, **good),  # forged signature
    ):
        resp = client.get("/decks", headers=_hdr(token, 1))
        assert resp.status_code == 401, token
        assert "Not signed in" in resp.text


def test_revoked_credential_and_disabled_system_user(world, jwt_signer):
    client = _cf(world, jwt_signer)
    token = jwt_signer(common_name=CLIENT_ID)
    conn = db.connect(world["db_path"])
    users.set_disabled(conn, world["claude"].id, True)
    assert client.get("/decks", headers=_hdr(token, 1)).status_code == 401
    users.set_disabled(conn, world["claude"].id, False)
    assert client.get("/decks", headers=_hdr(token, 1)).status_code == 200
    users.revoke_credential(conn, users.list_credentials(conn)[0].id)
    assert client.get("/decks", headers=_hdr(token, 1)).status_code == 401
    conn.close()


def test_token_with_email_and_common_name_is_the_person(world, jwt_signer):
    client = _cf(world, jwt_signer)
    token = jwt_signer(email="alex@example.com", common_name=CLIENT_ID)
    page = client.get("/decks", headers=_hdr(token))  # no X-Act-For needed: a person
    assert page.status_code == 200
    assert "Alex Blue" in page.text
    assert client.get("/decks", headers=_hdr(token, 1)).status_code == 403  # and can't act for others


def test_disabled_person_is_refused_at_sign_in(world, jwt_signer):
    conn = db.connect(world["db_path"])
    users.set_disabled(conn, world["friend"].id, True)
    conn.close()
    client = _cf(world, jwt_signer)
    resp = client.get("/decks", headers=_hdr(jwt_signer(email="alex@example.com")))
    assert resp.status_code == 401
    assert "access has been turned off" in resp.text
    # The dev cookie is refused the same way.
    dev = TestClient(create_app(world["db_path"], auth=AuthConfig(mode="dev")))
    dev.cookies.set(DEV_COOKIE, str(world["friend"].id))
    assert dev.get("/decks").status_code == 401


# --- X-Act-For ---------------------------------------------------------------

def test_act_for_validation(world):
    conn = db.connect(world["db_path"])
    ghost = users.create(conn, "Ghost", "ghost@example.com")
    users.set_disabled(conn, ghost.id, True)
    other_bot = users.create_system(conn, "Other bot")
    conn.close()
    client = _dev(world)
    assert client.get("/decks").status_code == 400
    for bad in ("", "abc", "-1", "1.5", "99999", "9" * 40, "0x1"):
        resp = client.get("/decks", headers={"X-Act-For": bad})
        assert resp.status_code == 400, bad
        assert resp.json()["error"]["code"] == "bad_act_for"
    for forbidden in (ghost.id, other_bot.id, world["claude"].id):
        resp = client.get("/decks", headers={"X-Act-For": str(forbidden)})
        assert resp.status_code == 403, forbidden
    ok = client.get("/decks", headers={"X-Act-For": str(world["friend"].id)})
    assert ok.status_code == 200
    assert "Alex Blue" in ok.text
    assert "Keen Engineering" not in ok.text


def test_a_person_may_not_send_act_for(world, db_path):
    for config in (AuthConfig(mode="single"), AuthConfig(mode="dev")):
        client = TestClient(create_app(db_path, auth=config))
        resp = client.get("/decks", headers={"X-Act-For": "2"})
        assert resp.status_code == 403
        assert "X-Act-For is for system users" in resp.text


def test_a_person_may_not_send_act_for_in_cloudflare_mode(world, jwt_signer):
    client = _cf(world, jwt_signer)
    token = jwt_signer(email="alex@example.com")
    assert client.get("/decks", headers=_hdr(token, 1)).status_code == 403
    loopback = TestClient(create_app(world["db_path"], auth=AuthConfig(
        mode="cloudflare", team_domain=TEAM, audience=AUD), jwks_client=_FakeJWKS(jwt_signer.public_key)),
        client=("127.0.0.1", 1))
    assert loopback.get("/decks").status_code == 200
    assert loopback.get("/decks", headers={"X-Act-For": "2"}).status_code == 403


# --- guards apply to the principal -------------------------------------------

def test_system_user_edits_only_the_principals_things(world):
    keen, alex_deck = world["keen"], world["friend_deck"]
    client = _dev(world, **{"X-Act-For": "1"})
    resp = client.post(f"/decks/{keen}/notes", data={"notes": "plan: durdle"}, follow_redirects=False)
    assert resp.status_code == 303
    assert "error=" not in resp.headers["location"]
    assert client.get(f"/decks/{keen}/notes.txt").text == "plan: durdle"
    assert client.post(f"/decks/{alex_deck}/notes", data={"notes": "x"}).status_code == 403
    assert client.get(f"/decks/{alex_deck}/notes.txt").status_code == 403
    theirs = world["friend_lots"][1]
    assert client.post(f"/decks/{keen}/swaps", data={"lot_id": str(theirs), "want_id": "1"},
                       follow_redirects=False).status_code == 403
    # ...and the other way round: acting for Alex, Keen is off limits.
    alex = _dev(world, **{"X-Act-For": str(world["friend"].id)})
    assert alex.post(f"/decks/{keen}/notes", data={"notes": "x"}).status_code == 403
    mine = alex.post(f"/decks/{alex_deck}/notes", data={"notes": "ok"}, follow_redirects=False)
    assert mine.status_code == 303


def test_money_is_visible_only_when_acting_for_the_owner(world):
    for_owner = _dev(world, **{"X-Act-For": "1"})
    for_friend = _dev(world, **{"X-Act-For": str(world["friend"].id)})
    assert "$0.48" in for_owner.get("/people/1/cards", params={"view": "list"}).text
    assert "$0.48" not in for_friend.get("/people/1/cards", params={"view": "list"}).text
    assert for_owner.get("/transactions/1").status_code == 200
    assert for_friend.get("/transactions/1").status_code == 404


def test_system_users_are_not_people(world):
    client = _dev(world, **{"X-Act-For": "1"})
    assert "Claude" not in client.get("/people").text.split("System users")[0]
    assert client.get(f"/people/{world['claude'].id}").status_code == 404


# --- human-only routes --------------------------------------------------------

def test_credential_admin_is_human_only(world):
    cid = users.list_credentials(db.connect(world["db_path"]))[0].id
    routes = [
        ("post", f"/system-users/{world['claude'].id}/credentials", {"label": "x", "cf_client_id": "y"}),
        ("post", f"/system-users/credentials/{cid}/revoke", {}),
        ("post", f"/system-users/{world['claude'].id}/disable", {}),
        ("post", f"/system-users/{world['claude'].id}/enable", {}),
        ("get", "/dev/as/2", {}),
    ]
    system = _dev(world, **{"X-Act-For": "1"})  # even acting for the admin
    for method, url, data in routes:
        resp = system.get(url) if method == "get" else system.post(url, data=data, follow_redirects=False)
        assert resp.status_code == 403, url
    conn = db.connect(world["db_path"])
    assert users.list_credentials(conn)[0].revoked_at is None
    assert not users.get(conn, world["claude"].id).disabled
    conn.close()


def test_admin_manages_system_users_on_the_people_page(world):
    admin = TestClient(create_app(world["db_path"], auth=AuthConfig(mode="dev")))
    page = admin.get("/people").text
    assert "System users" in page
    assert "…access" in page  # last 6 chars of the Client ID
    assert CLIENT_ID not in page
    cid = world["claude"].id
    resp = admin.post(f"/system-users/{cid}/credentials", data={"label": "second", "cf_client_id": "zzz999"},
                      follow_redirects=False)
    assert "notice=" in resp.headers["location"]
    dup = admin.post(f"/system-users/{cid}/credentials", data={"label": "second", "cf_client_id": "zzz999"},
                     follow_redirects=False)
    assert "error=" in dup.headers["location"]
    assert "error=" in admin.post("/system-users/1/credentials", data={"label": "a", "cf_client_id": "b"},
                                  follow_redirects=False).headers["location"]
    admin.post(f"/system-users/{cid}/disable")
    assert "disabled" in admin.get("/people").text
    assert _dev(world, **{"X-Act-For": "1"}).get("/decks").status_code == 401
    admin.post(f"/system-users/{cid}/enable")
    creds = users.list_credentials(db.connect(world["db_path"]))
    admin.post(f"/system-users/credentials/{creds[0].id}/revoke")
    assert "revoked" in admin.get("/people").text
    assert _dev(world, **{"X-Act-For": "1"}).get("/decks").status_code == 401
    # A non-admin friend can't.
    friend = TestClient(create_app(world["db_path"], auth=AuthConfig(mode="dev")))
    friend.cookies.set(DEV_COOKIE, str(world["friend"].id))
    assert "System users" not in friend.get("/people").text
    assert friend.post(f"/system-users/{cid}/disable").status_code == 403
    # Single mode: every visitor is the admin, so the web can't grant access.
    single = TestClient(create_app(world["db_path"]))
    grant = single.post(f"/system-users/{cid}/credentials", data={"label": "a", "cf_client_id": "b"})
    assert grant.status_code == 403


# --- confirmed dry runs ---------------------------------------------------------

def _preview(client, data=ADD):
    resp = client.post("/people", data=data, follow_redirects=False)
    assert resp.status_code == 409, resp.text
    return resp.json()


def test_unconfirmed_system_request_gets_a_preview_and_writes_nothing(world):
    client = _dev(world, **{"X-Act-For": "1"})
    before = _people_count(world)
    body = _preview(client)
    assert body["error"]["code"] == "confirm_required"
    assert body["preview"]["email"] == "sam@example.com"
    assert body["preview"]["action"] == "add_person"
    assert body["confirm"]
    assert _people_count(world) == before


def test_confirm_token_commits_exactly_the_previewed_action(world):
    client = _dev(world, **{"X-Act-For": "1"})
    body = _preview(client)
    resp = client.post("/people", data={**ADD, "confirm": body["confirm"]}, follow_redirects=False)
    assert resp.status_code == 303
    assert "notice=Added+Sam" in resp.headers["location"]
    conn = db.connect(world["db_path"])
    assert users.by_email(conn, "sam@example.com").id == body["preview"]["user_id"]
    row = audit.recent(conn, 1)[0]
    assert row["actor_id"] == world["claude"].id
    assert row["principal_id"] == 1
    assert row["summary"]["preview"] == body["preview"]
    assert row["notice"] == "Added Sam."
    conn.close()


def _token_for(client, data=ADD):
    return _preview(client, data)["confirm"]


def test_confirm_token_is_bound_to_everything(world):
    owner = _dev(world, **{"X-Act-For": "1"})
    token = _token_for(owner)
    # Different params.
    resp = owner.post("/people", data={**ADD, "display_name": "Eve", "confirm": token})
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "confirm_invalid")
    # Different principal: another admin.
    conn = db.connect(world["db_path"])
    other = users.create(conn, "Other admin", "oa@example.com", is_admin=True)
    conn.close()
    resp = _dev(world, **{"X-Act-For": str(other.id)}).post("/people", data={**ADD, "confirm": token})
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "confirm_invalid")
    # Different path (a made-up route's token can't be replayed here): sign for another path.
    forged = confirm.issue(
        world_app(owner).state.confirm_secret,
        confirm.Context(world["claude"].id, 1, "POST", "/elsewhere", confirm.params_of(ADD.items())),
        {"action": "add_person"},
    )
    resp = owner.post("/people", data={**ADD, "confirm": forged})
    assert resp.json()["error"]["code"] == "confirm_invalid"
    # Garbage and tampering.
    for bad in ("garbage", "", token[:-4] + "AAAA", base64.urlsafe_b64encode(b'{"exp":1}').decode()):
        resp = owner.post("/people", data={**ADD, "confirm": bad})
        assert resp.status_code == 409, bad
    raw = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    raw["exp"] += 3600  # extending the life breaks the signature
    tampered = base64.urlsafe_b64encode(json.dumps(raw).encode()).decode()
    resp = owner.post("/people", data={**ADD, "confirm": tampered})
    assert resp.json()["error"]["code"] == "confirm_invalid"
    # Another process's secret.
    other_app = TestClient(create_app(world["db_path"], auth=AuthConfig(mode="dev")))
    other_app.headers.update({"X-Dev-Service": CLIENT_ID, "X-Act-For": "1"})
    resp = other_app.post("/people", data={**ADD, "confirm": token})
    assert resp.json()["error"]["code"] == "confirm_invalid"
    assert users.by_email(db.connect(world["db_path"]), "sam@example.com") is None


def world_app(client):
    return client.app


def test_expired_token(world, monkeypatch):
    client = _dev(world, **{"X-Act-For": "1"})
    token = _token_for(client)
    now = time.time()
    monkeypatch.setattr(confirm, "_now", lambda: now + confirm.TTL_SECONDS + 5)
    resp = client.post("/people", data={**ADD, "confirm": token})
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "confirm_invalid")
    assert users.by_email(db.connect(world["db_path"]), "sam@example.com") is None


def test_preview_changed_between_preview_and_confirm(world):
    client = _dev(world, **{"X-Act-For": "1"})
    body = _preview(client)
    conn = db.connect(world["db_path"])
    users.create(conn, "Someone else", "else@example.com")  # takes the id the preview promised
    conn.close()
    resp = client.post("/people", data={**ADD, "confirm": body["confirm"]})
    assert resp.status_code == 409
    again = resp.json()
    assert again["error"]["code"] == "preview_changed"
    assert again["preview"]["user_id"] != body["preview"]["user_id"]
    assert users.by_email(db.connect(world["db_path"]), "sam@example.com") is None
    # The fresh token confirms the new preview.
    ok = client.post("/people", data={**ADD, "confirm": again["confirm"]}, follow_redirects=False)
    assert ok.status_code == 303
    assert "notice=" in ok.headers["location"]


def test_a_person_needs_no_token(world):
    admin = TestClient(create_app(world["db_path"], auth=AuthConfig(mode="dev")))
    resp = admin.post("/people", data=ADD, follow_redirects=False)
    assert resp.status_code == 303
    assert "notice=Added+Sam" in resp.headers["location"]


def test_service_errors_still_surface_under_dry_run(world):
    client = _dev(world, **{"X-Act-For": "1"})
    resp = client.post("/people", data={"display_name": "X", "email": "nonsense"}, follow_redirects=False)
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


class Json:
    """A sample sent as a JSON body instead of a form; `{lot_id}` in a string is
    the id of the pulled lot _fill_path made."""

    def __init__(self, body):
        self.body = body

    def filled(self, fills):
        return {k: [v.format(**fills) for v in vs] for k, vs in self.body.items()}


# Every risky route, with a minimal valid form. Adding a @risky route without a
# row here fails test_risky_registry_is_complete.
RISKY_SAMPLES = {
    ("POST", "/people"): ADD,
    ("DELETE", "/api/v1/imports/{import_id}"): {},
    ("DELETE", "/api/v1/purchases/{purchase_id}"): {},
    ("POST", "/api/v1/decks/{deck_id}/assembly/pulls"): Json({"line_ids": ["pull:lot:{lot_id}"]}),
}


def _risky_routes(app):
    """(method, full path template) of every @risky route, through nested
    include_router() wrappers (which keep their prefix in include_context)."""
    found = set()

    def walk(routes, prefix):
        for route in routes:
            nested = getattr(route, "original_router", None)
            if nested is not None:
                walk(nested.routes, prefix + route.include_context.prefix)
            elif getattr(getattr(route, "endpoint", None), "__risky__", False):
                found.update((method, prefix + route.path) for method in route.methods)

    walk(app.routes, "")
    return found


def _fill_path(world, path):
    """Make the sample route's path concrete: an unused import (or manual
    purchase) of user 1, or a deck of theirs with a copy that sits in another
    deck. Returns (path, fills)."""
    fills = {}
    conn = db.connect(world["db_path"])
    if "{deck_id}" in path:
        mine, other = (conn.execute(
            "INSERT INTO decks (name, format, created_at, owner_id) VALUES (?, 'Commander', 't', 1)", (n,)
        ).lastrowid for n in ("Sample mine", "Sample other"))
        lot = conn.execute(
            "INSERT INTO lots (owner_id, card_name, acquired_date) VALUES (1, 'Sample Card', '2026-01-01')"
        ).lastrowid
        conn.execute("INSERT INTO deck_cards (deck_id, lot_id, equipped) VALUES (?, ?, 0)", (mine, lot))
        conn.execute("INSERT INTO deck_cards (deck_id, lot_id, equipped) VALUES (?, ?, 1)", (other, lot))
        path = path.replace("{deck_id}", str(mine))
        fills["lot_id"] = lot
    if "{import_id}" in path:
        cur = conn.execute(
            "INSERT INTO collection_imports (owner_id, sha256, original_filename, format, card_count,"
            " imported_by, imported_at) VALUES (1, 'sample', 'sample.csv', 'ManaBox', 0, 1, 't')"
        )
        path = path.replace("{import_id}", str(cur.lastrowid))
    if "{purchase_id}" in path:
        cur = conn.execute(
            "INSERT INTO transactions (vendor, created_at, updated_at, subtotal_cents, tax_cents,"
            " total_cents, source, owner_id)"
            " VALUES ('Sample', '2026-01-01', '2026-01-01', 0, 0, 0, 'manual', 1)"
        )
        path = path.replace("{purchase_id}", str(cur.lastrowid))
    conn.commit()
    conn.close()
    return path, fills


def test_risky_registry_is_complete(world):
    app = create_app(world["db_path"], auth=AuthConfig(mode="dev"))
    assert _risky_routes(app) == set(RISKY_SAMPLES)


@pytest.mark.parametrize("route", sorted(RISKY_SAMPLES))
def test_every_risky_route_rejects_an_unconfirmed_system_actor(world, route):
    client = _dev(world, **{"X-Act-For": "1"})
    method, path = route
    path, fills = _fill_path(world, path)
    sample = RISKY_SAMPLES[route]
    body = {"json": sample.filled(fills)} if isinstance(sample, Json) else {"data": sample}
    snapshot = _count(world, "SELECT COUNT(*) FROM users") + _count(world, "SELECT COUNT(*) FROM audit_log")
    resp = client.request(method, path, follow_redirects=False, **body)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "confirm_required"
    assert resp.json()["confirm"]
    assert "preview" in resp.json()
    after = _count(world, "SELECT COUNT(*) FROM users") + _count(world, "SELECT COUNT(*) FROM audit_log")
    assert snapshot == after


# --- audit log -------------------------------------------------------------------

def test_successful_write_is_audited(world):
    keen = world["keen"]
    client = _dev(world, **{"X-Act-For": "1"})
    client.post(f"/decks/{keen}/notes", data={"notes": "hello"})
    conn = db.connect(world["db_path"])
    rows = audit.recent(conn, 1)
    assert len(rows) == 1
    row = rows[0]
    assert (row["actor_id"], row["principal_id"], row["method"]) == (world["claude"].id, 1, "POST")
    assert row["path"] == f"/decks/{keen}/notes"
    assert row["action"] == "deck_save_notes"
    assert json.loads(row["target"]) == {"deck_id": str(keen)}
    assert row["restore_point"].endswith("-deck-save-notes.db.gz")
    conn.close()
    # A person's own write: actor == principal.
    me = TestClient(create_app(world["db_path"], auth=AuthConfig(mode="dev")))
    me.post(f"/decks/{keen}/notes", data={"notes": "again"})
    conn = db.connect(world["db_path"])
    newest = audit.recent(conn, 1)[0]
    assert newest["actor_id"] == newest["principal_id"] == 1
    conn.close()


def test_failures_and_reads_are_not_audited(world):
    keen, alex_deck = world["keen"], world["friend_deck"]
    client = _dev(world, **{"X-Act-For": "1"})
    client.get("/decks")
    assert client.post(f"/decks/{alex_deck}/notes", data={"notes": "x"}).status_code == 403
    err = client.post(f"/decks/{keen}/changes", data={"planned_name": ""}, follow_redirects=False)
    assert err.status_code == 303
    assert "error=" in err.headers["location"]
    _preview(client)  # a 409 isn't a success either
    assert _count(world, "SELECT COUNT(*) FROM audit_log") == 0


def test_audit_failure_never_breaks_the_response(world, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("audit down")

    monkeypatch.setattr(middleware.audit, "record", boom)
    client = _dev(world, **{"X-Act-For": "1"})
    resp = client.post(f"/decks/{world['keen']}/notes", data={"notes": "kept"}, follow_redirects=False)
    assert resp.status_code == 303
    assert client.get(f"/decks/{world['keen']}/notes.txt").text == "kept"


def test_activity_is_private_to_the_person_and_admins(world):
    keen = world["keen"]
    _dev(world, **{"X-Act-For": "1"}).post(f"/decks/{keen}/notes", data={"notes": "hello"})
    admin = TestClient(create_app(world["db_path"], auth=AuthConfig(mode="dev")))
    page = admin.get("/people/1").text
    assert "Recent activity" in page
    assert "Claude for Owner" in page
    assert "deck_save_notes" in page
    friend = TestClient(create_app(world["db_path"], auth=AuthConfig(mode="dev")))
    friend.cookies.set(DEV_COOKIE, str(world["friend"].id))
    assert "Recent activity" not in friend.get("/people/1").text
    assert "Recent activity" not in friend.get(f"/people/{world['friend'].id}").text  # nothing yet


# --- dev-mode service header -------------------------------------------------------

def test_dev_service_header_only_in_dev_mode(world):
    headers = {"X-Dev-Service": CLIENT_ID}
    assert TestClient(create_app(world["db_path"], auth=AuthConfig(mode="dev"))).get(
        "/decks", headers=headers).status_code == 400  # a system actor with no X-Act-For
    single = TestClient(create_app(world["db_path"])).get("/decks", headers=headers)
    assert single.status_code == 200
    assert "Keen Engineering" in single.text  # ignored: user 1, a person
    cf = TestClient(
        create_app(world["db_path"], auth=AuthConfig(mode="cloudflare", team_domain=TEAM, audience=AUD),
                   jwks_client=_FakeJWKS(None)), client=("10.0.0.5", 1))
    assert cf.get("/decks", headers=headers).status_code == 401  # ignored: no token
    loop = TestClient(
        create_app(world["db_path"], auth=AuthConfig(mode="cloudflare", team_domain=TEAM, audience=AUD),
                   jwks_client=_FakeJWKS(None)), client=("127.0.0.1", 1))
    assert loop.get("/decks", headers=headers).status_code == 200  # loopback person, not a service
    assert _dev(world, **{"X-Dev-Service": "nope"}).get("/decks").status_code == 401


def test_cli_commands(db_path):
    from typer.testing import CliRunner

    from mtgtracker.cli import app

    runner = CliRunner()
    base = ["--db", str(db_path)]
    assert "Added system user 2" in runner.invoke(app, ["add-system-user", "Claude", *base]).output
    out = runner.invoke(
        app, ["add-credential", "2", "--cf-client-id", "cid.access", "--label", "laptop", *base]
    )
    assert out.exit_code == 0
    person = runner.invoke(app, ["add-credential", "1", "--cf-client-id", "c2", "--label", "no", *base])
    assert person.exit_code != 0
    listing = runner.invoke(app, ["list-credentials", *base]).output
    assert "cid.access" in listing
    assert "active" in listing
    assert runner.invoke(app, ["revoke-credential", "1", *base]).exit_code == 0
    assert "revoked" in runner.invoke(app, ["list-credentials", *base]).output
    assert runner.invoke(app, ["disable-user", "1", *base]).exit_code != 0


def test_a_confirm_token_is_single_use(monkeypatch):
    """Replaying a confirm token after its action committed is refused, even
    when the fresh preview still matches (an action whose preview doesn't
    change once done)."""
    monkeypatch.setattr(confirm, "_spent", {})
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    ctx = confirm.Context(actor_id=9, principal_id=1, method="POST", path="/x", params=())

    def op(c):
        c.execute("INSERT INTO t VALUES (1)")
        return None, {"action": "same every time"}

    secret = b"s" * 32
    with pytest.raises(confirm.ConfirmRequired) as first:
        confirm.run(secret, conn, ctx, None, op)
    token = first.value.token
    confirm.run(secret, conn, ctx, token, op)
    with pytest.raises(confirm.ConfirmRequired) as replay:
        confirm.run(secret, conn, ctx, token, op)
    assert replay.value.code == "confirm_used"
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
