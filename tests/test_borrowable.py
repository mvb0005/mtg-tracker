import json
import re

from fastapi.testclient import TestClient

from mtgtracker import db, users
from mtgtracker.web import queries
from mtgtracker.web.app import create_app
from mtgtracker.web.auth import DEV_COOKIE, AuthConfig


def _setup(db_path):
    """Your Keen Engineering deck wants three cards; Alex owns two of them
    (one as a double-faced card listed by its full name, plus a copy he's
    sold), Sam owns one."""
    conn = db.connect(db_path)
    keen = conn.execute("SELECT id FROM decks WHERE name = 'Keen Engineering'").fetchone()[0]
    for name in ("Swan Song", "Swan Song", "Delver of Secrets", "Mox Opal"):
        conn.execute(
            "INSERT INTO deck_wants (deck_id, card_name, created_at) VALUES (?, ?, '2026-10-01')",
            (keen, name),
        )
    alex = users.create(conn, "Alex", "alex@example.com")
    sam = users.create(conn, "Sam", "sam@example.com")

    def lot(owner, name):
        return conn.execute(
            "INSERT INTO lots (owner_id, card_name, acquired_date) VALUES (?, ?, '2026-10-01')", (owner, name)
        ).lastrowid

    lot(alex.id, "Swan Song")
    lot(alex.id, "Delver of Secrets // Insectile Aberration")
    sold = lot(alex.id, "Mox Opal")
    conn.execute(
        "INSERT INTO dispositions (lot_id, disposed_at, disposition_type)"
        " VALUES (?, '2026-10-02', 'sold')",
        (sold,),
    )
    lot(sam.id, "Swan Song")
    lot(sam.id, "Swan Song")
    conn.commit()
    return conn, keen, alex, sam


def _client(db_path, user_id):
    client = TestClient(create_app(db_path, auth=AuthConfig(mode="dev")))
    client.cookies.set(DEV_COOKIE, str(user_id))
    return client


def test_deck_shows_which_friends_have_cards_you_want(db_path, cache_dir):
    conn, keen, alex, sam = _setup(db_path)
    detail = queries.get_deck(conn, keen, viewer_id=1)
    friends = {g["card_name"]: g["friends"] for g in detail["groups"] if g["wanted"]}
    assert friends["Swan Song"] == [
        {"id": sam.id, "name": "Sam", "qty": 2, "where": "2 in no deck"},
        {"id": alex.id, "name": "Alex", "qty": 1, "where": "1 in no deck"},
    ]
    assert [f["name"] for f in friends["Delver of Secrets"]] == ["Alex"]  # front face matches
    assert friends["Mox Opal"] == []  # his only copy was sold
    assert detail["borrowable_count"] == 3  # 2 Swan Song + 1 Delver
    assert [(f["name"], f["cards"]) for f in detail["borrow_from"]] == [
        ("Alex", [("Delver of Secrets", 1), ("Swan Song", 1)]),
        ("Sam", [("Swan Song", 2)]),
    ]
    assert detail["acquire"] == [("Mox Opal", 1)]
    assert queries.borrow_summary(friends["Swan Song"]) == "Owned by 2 friends: Sam (2), Alex (1)"
    conn.close()


def test_deck_page_flags_borrowable_cards(db_path, cache_dir):
    conn, keen, alex, _sam = _setup(db_path)
    conn.close()
    html = _client(db_path, 1).get(f"/decks/{keen}").text
    assert re.search(
        r'class="tile[^"]*wanted borrowable"[^>]*title="Owned by 2 friends: Sam \(2\), Alex \(1\)"', html
    )
    assert ">From 2 friends<" in html
    assert ">From Alex<" in html
    assert ">To acquire<" in html  # Mox Opal: nobody has one
    assert "<dt>Borrowable</dt><dd>3</dd>" in html
    assert "Borrow from friends" in html
    assert "Nobody has these" in html
    panel = next(
        json.loads(m.replace("&#39;", "'"))
        for m in re.findall(r"data-card='([^']*)'", html) if '"name": "Delver of Secrets"' in m
    )
    assert panel["friends"] == [{"id": alex.id, "name": "Alex", "qty": 1, "where": "1 in no deck"}]

    listed = _client(db_path, 1).get(f"/decks/{keen}", params={"view": "list"}).text
    assert 'class="tag borrow">from Alex<' in listed

    modify = _client(db_path, 1).get(f"/decks/{keen}/modify").text
    assert ">From Alex</p>" in modify


def test_your_own_copies_are_never_listed_as_a_friends(db_path, cache_dir):
    """Viewing your deck, Alex sees Sam's copies but not his own."""
    conn, keen, alex, _sam = _setup(db_path)
    conn.close()
    html = _client(db_path, alex.id).get(f"/decks/{keen}").text
    assert 'title="Owned by 1 friend: Sam (2)"' in html
    assert "Alex (1)" not in html
