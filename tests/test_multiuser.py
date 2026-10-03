import re
import sqlite3
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from mtgtracker import db, users
from mtgtracker.web.app import create_app
from mtgtracker.web.auth import DEV_COOKIE, AuthConfig

TEAM = "example.cloudflareaccess.com"
AUD = "test-aud"


@pytest.fixture
def two_users(db_path, cache_dir):
    """Your seeded data (user 1) plus a friend with two cards and a deck."""
    conn = db.connect(db_path)
    friend = users.create(conn, "Alex", "alex@example.com")
    lots = []
    for name in ("Rhystic Study", "Counterspell"):
        cur = conn.execute(
            "INSERT INTO lots (owner_id, card_name, acquired_date, allocated_cost_cents)"
            " VALUES (?, ?, '2026-10-01', 999)",
            (friend.id, name),
        )
        lots.append(cur.lastrowid)
    cur = conn.execute(
        "INSERT INTO decks (name, format, created_at, owner_id)"
        " VALUES ('Alex Blue', 'Commander', '2026-10-01', ?)",
        (friend.id,),
    )
    friend_deck = cur.lastrowid
    conn.execute(
        "INSERT INTO deck_cards (deck_id, lot_id, equipped) VALUES (?, ?, 1)", (friend_deck, lots[0])
    )
    conn.commit()
    keen = conn.execute("SELECT id FROM decks WHERE name = 'Keen Engineering'").fetchone()[0]
    my_lot = conn.execute(
        "SELECT lot_id FROM deck_cards WHERE deck_id = ? ORDER BY lot_id LIMIT 1", (keen,)
    ).fetchone()[0]
    conn.close()
    return {"friend": friend, "friend_lots": lots, "friend_deck": friend_deck, "keen": keen, "my_lot": my_lot}


def _client(db_path, user_id):
    client = TestClient(create_app(db_path, auth=AuthConfig(mode="dev")))
    client.cookies.set(DEV_COOKIE, str(user_id))
    return client


def test_migration_0006_rebuilds_populated_lots(tmp_path):
    """v5 -> v6 on a populated DB: lots is rebuilt in place, every row and id
    survives, everything belongs to user 1, and the tables that reference
    lots still line up (foreign keys on, no violations)."""
    conn = sqlite3.connect(tmp_path / "v5.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    for index, path in enumerate(db._migration_files()[:5], start=1):
        conn.executescript(path.read_text())
        conn.execute(f"PRAGMA user_version = {index}")
    conn.execute(
        "INSERT INTO transactions (id, order_type_identifier, order_identifier, vendor,"
        " created_at, updated_at, subtotal_cents, tax_cents, total_cents, source)"
        " VALUES (7, 't', 'o', 'Shop', '2026-01-01', '2026-01-01', 300, 0, 300, 'manual')"
    )
    conn.execute(
        "INSERT INTO decks (id, name, format, created_at) VALUES (3, 'D', 'Commander', '2026-01-01')"
    )
    for lot_id in (11, 12, 13):
        conn.execute(
            "INSERT INTO lots (id, transaction_id, card_name, allocated_cost_cents, acquired_date)"
            " VALUES (?, 7, ?, 100, '2026-01-01')",
            (lot_id, f"Card {lot_id}"),
        )
    conn.execute("INSERT INTO deck_cards (deck_id, lot_id, equipped) VALUES (3, 11, 1), (3, 12, 0)")
    conn.execute(
        "INSERT INTO dispositions (lot_id, disposed_at, disposition_type) VALUES (13, '2026-02-01', 'sold')"
    )
    conn.execute(
        "INSERT INTO deck_versions (id, deck_id, version, created_at) VALUES (1, 3, 1, '2026-01-01')"
    )
    conn.execute("INSERT INTO deck_version_cards (version_id, lot_id) VALUES (1, 11), (1, 12)")
    conn.execute(
        "INSERT INTO deck_changes (deck_id, add_card_name, add_lot_id, cut_lot_id, created_at)"
        " VALUES (3, 'Card 12', 12, 11, '2026-01-01')"
    )
    conn.commit()

    assert db.migrate(conn) == len(db._migration_files())
    lots = conn.execute(
        "SELECT id, owner_id, transaction_id, allocated_cost_cents FROM lots ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in lots] == [(11, 1, 7, 100), (12, 1, 7, 100), (13, 1, 7, 100)]
    assert conn.execute("SELECT owner_id FROM decks").fetchone()[0] == 1
    assert conn.execute("SELECT owner_id FROM transactions").fetchone()[0] == 1
    for table, n in (("deck_cards", 2), ("dispositions", 1), ("deck_version_cards", 2), ("deck_changes", 1)):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == n
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):  # deck_cards still points at lots
        conn.execute("INSERT INTO deck_cards (deck_id, lot_id, equipped) VALUES (3, 999, 0)")
    conn.close()


def test_migration_allows_cards_without_a_purchase(db_path):
    conn = db.connect(db_path)
    cur = conn.execute("INSERT INTO lots (card_name, acquired_date) VALUES ('Sol Ring', '2026-10-01')")
    row = conn.execute("SELECT owner_id, transaction_id FROM lots WHERE id = ?", (cur.lastrowid,)).fetchone()
    assert tuple(row) == (1, None)
    owner = users.get(conn, 1)
    assert (owner.is_admin, owner.email, owner.signed_in) == (True, None, False)
    conn.close()


def test_each_user_sees_only_their_own(db_path, two_users):
    friend = _client(db_path, two_users["friend"].id)
    decks = friend.get("/decks").text
    assert "Alex Blue" in decks
    assert "Keen Engineering" not in decks
    cards = friend.get("/cards", params={"view": "list"}).text
    assert cards.count("data-card=") == 2
    assert "Thought Monitor" not in cards
    dashboard = friend.get("/").text
    assert "Total spent" in dashboard
    assert "$0.00" in dashboard  # no purchases
    me = _client(db_path, 1)
    assert me.get("/transactions/1").status_code == 200
    assert friend.get("/transactions/1").status_code == 404  # your purchase is private

    assert "Alex Blue" not in me.get("/decks").text
    assert "Rhystic Study" not in me.get("/cards").text


def test_friends_can_view_each_others_decks_read_only(db_path, two_users):
    friend = _client(db_path, two_users["friend"].id)
    html = friend.get(f"/decks/{two_users['keen']}").text
    assert "Keen Engineering" in html
    assert "Owner&#39;s decks" in html or "Owner's decks" in html
    assert "Modify deck" not in html
    assert "<dt>Cost basis</dt>" not in html
    assert 'value="cost"' not in html  # no sorting by someone else's prices
    assert 'class="commander-picker"' not in html
    # Panel data carries no cost for cards they don't own ("—" JSON-escaped).
    assert '"cost": "\\u2014"' in html
    assert '"cost": "$' not in html
    assert "needs printing" not in html
    version = friend.get(f"/decks/{two_users['keen']}/versions/1")
    assert version.status_code == 200
    assert "needs printing" not in version.text
    assert '"cost": "$' not in version.text

    people = friend.get("/people").text
    assert "Alex" in people
    assert "Owner" in people
    assert friend.get("/people/1/cards", params={"view": "list"}).status_code == 200
    me_cards = friend.get("/people/1/cards", params={"view": "list"}).text
    assert "Thought Monitor" in me_cards
    assert "$0.48" not in me_cards  # cost hidden


def _routes(t):
    keen, mine, theirs = t["keen"], t["my_lot"], t["friend_lots"][1]
    return [
        ("get", f"/decks/{keen}/modify", {}),
        ("post", f"/decks/{keen}/commander", {"commander": ""}),
        ("post", f"/decks/{keen}/changes", {"planned_name": "Sol Ring"}),
        ("post", f"/decks/{keen}/import", {"decklist": "1 Sol Ring"}),
        ("post", f"/decks/{keen}/changes/1/cut", {"cut_lot_id": ""}),
        ("post", f"/decks/{keen}/swaps", {"lot_id": str(theirs), "want_id": "1"}),
        ("post", f"/decks/{keen}/changes/1/link", {"lot_id": str(theirs)}),
        ("post", f"/decks/{keen}/changes/1/delete", {}),
        ("post", f"/decks/{keen}/apply", {}),
        ("get", f"/printings/{mine}", {}),
        ("post", f"/printings/{mine}", {"choice": "x|nonfoil"}),
        ("post", f"/printings/{mine}/fetch-set", {"set_code": "fdc"}),
        # Their own deck, but your card: taking it without a loan is refused.
        ("post", f"/decks/{t['friend_deck']}/changes", {"lot_id": str(mine)}),
        ("post", "/people", {"display_name": "Mallory"}),
    ]


def test_every_write_rejects_someone_elses_ids(db_path, two_users):
    friend = _client(db_path, two_users["friend"].id)
    for method, url, data in _routes(two_users):
        resp = friend.get(url) if method == "get" else friend.post(url, data=data, follow_redirects=False)
        assert resp.status_code == 403, (method, url, resp.status_code)


def test_your_change_and_want_ids_are_useless_on_a_friends_deck(db_path, two_users):
    """The deck guard passes (it's their deck), so the change/want id itself
    must be scoped to the deck: your pending change and wanted card survive."""
    conn = db.connect(db_path)
    keen, theirs = two_users["keen"], two_users["friend_lots"][1]
    want = conn.execute(
        "INSERT INTO deck_wants (deck_id, card_name, created_at) VALUES (?, 'Sol Ring', '2026-10-01')",
        (keen,),
    ).lastrowid
    change = conn.execute(
        "INSERT INTO deck_changes (deck_id, add_card_name, created_at)"
        " VALUES (?, 'Arcane Signet', '2026-10-01')",
        (keen,),
    ).lastrowid
    conn.commit()
    friend = _client(db_path, two_users["friend"].id)
    deck = two_users["friend_deck"]
    for url, data in [
        (f"/decks/{deck}/changes/{change}/delete", {}),
        (f"/decks/{deck}/changes/{change}/cut", {"cut_want_id": str(want)}),
        (f"/decks/{deck}/changes/{change}/link", {"lot_id": str(theirs)}),
        (f"/decks/{deck}/swaps", {"lot_id": str(theirs), "want_id": str(want)}),
    ]:
        resp = friend.post(url, data=data, follow_redirects=False)
        assert resp.status_code == 303, url
        assert "error=" in resp.headers["location"], url
    row = conn.execute(
        "SELECT deck_id, add_lot_id, cut_want_id FROM deck_changes WHERE id = ?", (change,)
    ).fetchone()
    assert tuple(row) == (keen, None, None)
    assert conn.execute("SELECT COUNT(*) FROM deck_changes WHERE deck_id = ?", (deck,)).fetchone()[0] == 0
    assert conn.execute("SELECT deck_id FROM deck_wants WHERE id = ?", (want,)).fetchone()[0] == keen
    conn.close()


def test_admin_adds_people(db_path, two_users):
    me = _client(db_path, 1)
    resp = me.post(
        "/people", data={"display_name": "Sam", "email": "sam@example.com"}, follow_redirects=False
    )
    assert "notice=Added+Sam" in resp.headers["location"]
    dup = me.post(
        "/people", data={"display_name": "Sam 2", "email": "SAM@example.com"}, follow_redirects=False
    )
    assert "already+a+user" in dup.headers["location"]
    assert "hasn&#39;t signed in" in me.get("/people").text or "hasn't signed in" in me.get("/people").text


def test_single_mode_cannot_add_people(db_path, cache_dir):
    """In single mode every visitor is the admin, so the web can't touch the
    sign-in allowlist (use `mtg add-user`)."""
    client = TestClient(create_app(db_path))
    assert "Add a person" not in client.get("/people").text
    resp = client.post("/people", data={"display_name": "Eve", "email": "eve@example.com"})
    assert resp.status_code == 403
    conn = db.connect(db_path)
    assert users.by_email(conn, "eve@example.com") is None
    conn.close()


# --- Cloudflare Access sign-in --------------------------------------------------

class _FakeJWKS:
    """Stands in for PyJWKClient: hands back our test public key."""

    def __init__(self, public_key):
        self.public_key = public_key

    def get_signing_key_from_jwt(self, token):
        return type("Key", (), {"key": self.public_key})()


@pytest.fixture
def signer():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def token(email, *, aud=AUD, iss=f"https://{TEAM}", exp_in=300):
        claims = {"email": email, "aud": aud, "iss": iss, "exp": int(time.time()) + exp_in}
        return jwt.encode(claims, key, algorithm="RS256")

    token.public_key = key.public_key()
    return token


def _cf_client(db_path, signer, *, client=("10.0.0.5", 50000), owner_email=None):
    config = AuthConfig(mode="cloudflare", team_domain=TEAM, audience=AUD, owner_email=owner_email)
    app = create_app(db_path, auth=config, jwks_client=_FakeJWKS(signer.public_key))
    return TestClient(app, client=client)


def test_cloudflare_sign_in(db_path, two_users, signer):
    client = _cf_client(db_path, signer, owner_email="me@example.com")
    friend_page = client.get("/decks", headers={"Cf-Access-Jwt-Assertion": signer("alex@example.com")})
    assert friend_page.status_code == 200
    assert "Alex Blue" in friend_page.text
    assert re.search(r'class="whoami"[^>]*>Alex<', friend_page.text)

    # MTG_OWNER_EMAIL claims user 1 on first sign-in; the cookie works too.
    client.cookies.set("CF_Authorization", signer("Me@Example.com"))
    owner = client.get("/decks")
    assert "Keen Engineering" in owner.text
    conn = db.connect(db_path)
    assert users.get(conn, 1).email == "Me@Example.com"
    assert users.get(conn, two_users["friend"].id).signed_in
    conn.close()


@pytest.mark.parametrize(
    "make_token",
    [
        lambda s: None,
        lambda s: "not-a-jwt",
        lambda s: s("alex@example.com", aud="someone-else"),
        lambda s: s("alex@example.com", iss="https://evil.cloudflareaccess.com"),
        lambda s: s("alex@example.com", exp_in=-60),
        lambda s: s("stranger@example.com"),  # valid token, but not a user (allowlist)
    ],
)
def test_cloudflare_rejects(db_path, two_users, signer, make_token):
    client = _cf_client(db_path, signer)
    token = make_token(signer)
    resp = client.get("/decks", headers={"Cf-Access-Jwt-Assertion": token} if token else {})
    assert resp.status_code == 401
    assert "Not signed in" in resp.text
    assert client.get("/static/style.css").status_code == 200  # static needs no identity
    assert client.get("/healthz").text == "ok"  # nor does the kubelet's probe


def test_loopback_acts_as_owner(db_path, two_users, signer):
    client = _cf_client(db_path, signer, client=("127.0.0.1", 50000))
    page = client.get("/decks")
    assert page.status_code == 200
    assert "Keen Engineering" in page.text
    # X-Forwarded-For can't fake loopback (uvicorn runs with proxy_headers=False;
    # the app itself never reads it).
    remote = _cf_client(db_path, signer)
    assert remote.get("/decks", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 401


def test_auth_config_from_env(monkeypatch):
    monkeypatch.setenv("MTG_AUTH", "cloudflare")
    monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
    with pytest.raises(ValueError, match="CF_ACCESS_TEAM_DOMAIN"):
        AuthConfig.from_env()
    monkeypatch.setenv("MTG_AUTH", "bogus")
    with pytest.raises(ValueError, match="single, dev or cloudflare"):
        AuthConfig.from_env()
    monkeypatch.setenv("MTG_AUTH", "")
    assert AuthConfig.from_env().mode == "single"
