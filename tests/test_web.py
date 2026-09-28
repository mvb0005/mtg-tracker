import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mtgtracker import db, scryfall
from mtgtracker.decks import borrow, create_deck
from mtgtracker.importers.order_wallet import import_order_file
from mtgtracker.importers.precon_decklist import seed_precon_deck_for_product
from mtgtracker.web.app import create_app, format_cents

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def seeded_template(tmp_path_factory):
    """A file-backed DB (per-request connections can't share :memory:) with
    the scrubbed order imported and both precons seeded from the committed
    Scryfall fixture -- no network. Built once per module; each test gets a
    copy."""
    tmp = tmp_path_factory.mktemp("web")
    cache_dir = tmp / "scryfall_cache"
    cache_dir.mkdir()
    shutil.copy(FIXTURES / "scryfall_fdc.json", cache_dir / "fdc.json")

    path = tmp / "template.db"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(scryfall, "CACHE_DIR", cache_dir)
        conn = db.connect(path)
        # Seeding commits per card; skip fsync so setup stays fast.
        conn.execute("PRAGMA synchronous = OFF")
        db.migrate(conn)
        result = import_order_file(conn, FIXTURES / "order_scrubbed.order", tmp / "receipts")
        for product in conn.execute(
            "SELECT id FROM products WHERE transaction_id = ?", (result.transaction_id,)
        ).fetchall():
            seed_precon_deck_for_product(conn, product["id"])
        conn.close()
    return path


@pytest.fixture
def db_path(seeded_template, tmp_path):
    path = tmp_path / "test.db"
    shutil.copy(seeded_template, path)
    return path


@pytest.fixture
def client(db_path):
    return TestClient(create_app(db_path))


def test_format_cents():
    assert format_cents(0) == "$0.00"
    assert format_cents(8620) == "$86.20"
    assert format_cents(123456789) == "$1,234,567.89"
    assert format_cents(-5) == "-$0.05"
    assert format_cents(None) == "—"
    assert format_cents(1999, "EUR") == "19.99 EUR"


def test_dashboard_shows_totals_and_decks(client, db_path):
    conn = db.connect(db_path)
    total = conn.execute("SELECT total_cents FROM transactions").fetchone()[0]
    card_count = conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0]
    conn.close()

    resp = client.get("/")
    assert resp.status_code == 200
    assert format_cents(total) in resp.text
    assert str(card_count) in resp.text
    assert "Reign of Dragons" in resp.text
    assert "Keen Engineering" in resp.text
    # Sol Ring / Arcane Signet have two fdc printings -- flagged for the user.
    assert "Needs your input" in resp.text
    assert "Sol Ring" in resp.text


def test_transaction_detail_reconciles(client, db_path):
    conn = db.connect(db_path)
    txn = conn.execute("SELECT id, total_cents, subtotal_cents FROM transactions").fetchone()
    conn.close()

    resp = client.get(f"/transactions/{txn['id']}")
    assert resp.status_code == 200
    assert format_cents(txn["total_cents"]) in resp.text
    assert format_cents(txn["subtotal_cents"]) in resp.text
    assert "Keen Engineering" in resp.text
    assert client.get("/transactions").status_code == 200


def test_receipt_files_are_not_served(client):
    assert client.get("/receipts/").status_code == 404
    assert client.get("/static/../receipts/").status_code == 404


def test_deck_detail_groups_basics_and_shows_reassemble(client, db_path):
    conn = db.connect(db_path)
    keen = conn.execute("SELECT id FROM decks WHERE name = 'Keen Engineering'").fetchone()["id"]
    lot = conn.execute(
        "SELECT lots.id FROM lots JOIN deck_cards ON deck_cards.lot_id = lots.id"
        " WHERE deck_cards.deck_id = ? AND lots.card_name = 'Counterspell'",
        (keen,),
    ).fetchone()["id"]
    new_deck = create_deck(conn, "Blue Pile", "2026-09-28")
    borrow(conn, lot, new_deck)
    conn.close()

    resp = client.get(f"/decks/{keen}")
    assert resp.status_code == 200
    html = resp.text
    # 34 Islands collapse into one grouped row.
    assert re.search(r'<td class="num">34</td>\s*<td class="name">Island', html)
    assert "Pull these 1 card back" in html
    assert "Blue Pile" in html  # where the borrowed Counterspell is now
    assert "needs printing" in html

    resp = client.get(f"/decks/{new_deck}")
    assert resp.status_code == 200
    assert "Every card on the list is in the deck" in resp.text


def test_cards_filter(client):
    resp = client.get("/cards", params={"q": "sol ring"})
    assert resp.status_code == 200
    assert "Sol Ring" in resp.text
    assert "Counterspell" not in resp.text

    # LIKE wildcards in user input are matched literally.
    resp = client.get("/cards", params={"q": "%"})
    assert "No cards match" in resp.text


def test_missing_records_404(client):
    assert client.get("/decks/9999").status_code == 404
    assert client.get("/transactions/9999").status_code == 404


def test_empty_database_renders(tmp_path):
    client = TestClient(create_app(tmp_path / "empty.db"))
    for path in ("/", "/decks", "/cards", "/transactions"):
        resp = client.get(path)
        assert resp.status_code == 200, path
    assert "No decks yet" in client.get("/").text
