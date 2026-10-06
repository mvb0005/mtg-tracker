from collections import Counter

from mtgtracker.importers.decklist_text import parse
from mtgtracker.web import queries

from .test_borrowable import _client, _setup


def test_decklist_export_round_trips_through_the_list_parser(db_path, cache_dir):
    conn, keen, *_ = _setup(db_path)
    detail = queries.get_deck(conn, keen, viewer_id=1)
    exports = queries.deck_exports(detail)
    text = exports["decklist"]
    # Owned copies carry their printing (and foil); unpinned ones (Sol Ring
    # is ambiguous in the fixture set) don't.
    assert text.startswith("Commander\n1 Sai, Master Thopterist (FDC) 5 *F*\n\nDeck\n")
    assert "1 Whirler Rogue (FDC) 83\n" in text
    assert "1 Sol Ring\n" in text
    assert "1 Delver of Secrets\n" in text  # wanted: front-face name, no printing
    parsed = parse(text)
    exported = Counter()
    for entry in parsed.entries:
        exported[entry.name] += entry.qty
    assert sum(exported.values()) == exports["decklist_count"] == detail["list_count"]
    assert [e.name for e in parsed.entries if e.commander] == ["Sai, Master Thopterist"]
    conn.close()


def test_shopping_list_leaves_out_what_friends_have(db_path, cache_dir):
    """4 Swan Songs wanted, friends have 3 between them: the fourth still
    has to be bought, so it's on the shopping list as well as the borrow lists."""
    conn, keen, *_ = _setup(db_path)
    for _ in range(2):
        conn.execute(
            "INSERT INTO deck_wants (deck_id, card_name, created_at) VALUES (?, 'Swan Song', '2026-10-01')",
            (keen,),
        )
    conn.commit()
    exports = queries.deck_exports(queries.get_deck(conn, keen, viewer_id=1))
    assert exports["shopping"] == "1 Mox Opal\n1 Swan Song\n"
    assert [(b["name"], b["text"]) for b in exports["borrow"]] == [
        ("Alex", "1 Delver of Secrets\n1 Swan Song\n"),
        ("Sam", "2 Swan Song\n"),
    ]
    conn.close()


def test_export_page_and_downloads(db_path, cache_dir):
    conn, keen, alex, _sam = _setup(db_path)
    conn.close()
    client = _client(db_path, 1)
    assert f'href="/decks/{keen}/export">Export</a>' in client.get(f"/decks/{keen}").text
    page = client.get(f"/decks/{keen}/export").text
    for heading in ("Decklist", "Shopping list", "Borrow from Alex"):
        assert heading in page
    resp = client.get(f"/decks/{keen}/export/decklist.txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["content-disposition"] == 'attachment; filename="keen-engineering-decklist.txt"'
    borrow = client.get(f"/decks/{keen}/export/borrow-{alex.id}.txt").text
    assert borrow == "1 Delver of Secrets\n1 Swan Song\n"
    assert client.get(f"/decks/{keen}/export/nope.txt").status_code == 404
    # Friends can export a deck they can view; nothing in it is private.
    assert _client(db_path, alex.id).get(f"/decks/{keen}/export/decklist.txt").status_code == 200
