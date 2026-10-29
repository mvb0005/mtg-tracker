import json
import sqlite3

import httpx
import pytest

from mtgtracker import db, deckmods, scryfall
from mtgtracker.deckmods import ChangeError
from tests.conftest import install_transport


@pytest.fixture
def conn(db_path, cache_dir):
    conn = db.connect(db_path)
    yield conn
    conn.close()


def _deck(conn, name):
    return conn.execute("SELECT * FROM decks WHERE name = ?", (name,)).fetchone()


def _lot(conn, deck_id, card_name):
    return conn.execute(
        "SELECT lots.id FROM lots JOIN deck_cards ON deck_cards.lot_id = lots.id"
        " WHERE deck_cards.deck_id = ? AND lots.card_name = ? ORDER BY lots.id",
        (deck_id, card_name),
    ).fetchone()[0]


def _list(conn, deck_id):
    return {row[0] for row in conn.execute("SELECT lot_id FROM deck_cards WHERE deck_id = ?", (deck_id,))}


def test_seeded_decks_start_at_v1_with_a_full_snapshot(conn):
    keen = _deck(conn, "Keen Engineering")
    assert keen["version"] == 1
    [v1] = deckmods.versions(conn, keen["id"])
    assert (v1["version"], v1["card_count"], v1["note"]) == (1, 100, "Initial list")
    assert set(deckmods.version_lot_ids(conn, keen["id"], 1)) == _list(conn, keen["id"])


def test_migration_backfills_existing_decks_as_v1():
    conn = sqlite3.connect(":memory:")
    files = sorted(db.MIGRATIONS_DIR.glob("*.sql"))
    for sql in files[:2]:
        conn.executescript(sql.read_text())
    conn.execute("PRAGMA user_version = 2")
    conn.execute("INSERT INTO transactions (id, vendor, created_at, updated_at, subtotal_cents, tax_cents,"
                 " total_cents, source) VALUES (1, 'x', 'd', 'd', 0, 0, 0, 'manual')")
    conn.execute("INSERT INTO decks (id, name, created_at) VALUES (7, 'Old Deck', '2026-01-01')")
    for lot_id in (1, 2, 3):
        conn.execute(
            "INSERT INTO lots (id, transaction_id, card_name, acquired_date) VALUES (?, 1, 'C', 'd')",
            (lot_id,),
        )
        conn.execute("INSERT INTO deck_cards (deck_id, lot_id, equipped) VALUES (7, ?, 1)", (lot_id,))

    assert db.migrate(conn) >= 3
    assert conn.execute("SELECT version FROM decks WHERE id = 7").fetchone()[0] == 1
    assert sorted(r[0] for r in conn.execute(
        "SELECT lot_id FROM deck_version_cards JOIN deck_versions ON deck_versions.id = version_id"
        " WHERE deck_id = 7 AND version = 1"
    )) == [1, 2, 3]


def test_swap_a_card_from_another_deck(conn):
    keen, reign = _deck(conn, "Keen Engineering")["id"], _deck(conn, "Reign of Dragons")["id"]
    incoming = _lot(conn, reign, "Blasphemous Act")
    outgoing = _lot(conn, keen, "Counterspell")

    change = deckmods.add_owned(conn, keen, incoming)
    deckmods.set_cut(conn, keen, change, outgoing)
    [pending] = deckmods.pending_changes(conn, keen)
    assert pending["ready"]
    assert pending["add_from_deck_name"] == "Reign of Dragons"
    assert pending["cut_card_name"] == "Counterspell"

    assert deckmods.apply_changes(conn, keen, "Swap in a wrath") == 2

    assert incoming in _list(conn, keen)
    assert outgoing not in _list(conn, keen)
    # Borrowed: equipped in Keen, still on Reign's list (unequipped).
    holder = conn.execute("SELECT deck_id FROM deck_cards WHERE lot_id = ? AND equipped = 1", (incoming,))
    assert holder.fetchone()[0] == keen
    assert incoming in _list(conn, reign)
    # The cut card went back to the collection: on no deck's list.
    assert conn.execute("SELECT COUNT(*) FROM deck_cards WHERE lot_id = ?", (outgoing,)).fetchone()[0] == 0
    # Version history and diff.
    v2, v1 = deckmods.versions(conn, keen)
    assert (v2["version"], v2["note"], v2["card_count"]) == (2, "Swap in a wrath", 100)
    assert v2["added"] == [("Blasphemous Act", 1)]
    assert v2["removed"] == [("Counterspell", 1)]
    assert v1["added"] == []
    assert deckmods.pending_changes(conn, keen) == []
    assert _deck(conn, "Keen Engineering")["version"] == 2


def test_apply_is_all_or_nothing(conn):
    keen, reign = _deck(conn, "Keen Engineering")["id"], _deck(conn, "Reign of Dragons")["id"]
    first = deckmods.add_owned(conn, keen, _lot(conn, reign, "Blasphemous Act"))
    deckmods.set_cut(conn, keen, first, _lot(conn, keen, "Counterspell"))
    doomed = _lot(conn, reign, "Chaos Warp")
    deckmods.add_owned(conn, keen, doomed)
    # The second card is sold before the batch is applied.
    conn.execute("INSERT INTO dispositions (lot_id, disposed_at, disposition_type) VALUES (?, 'd', 'sold')",
                 (doomed,))
    conn.commit()
    before = _list(conn, keen)

    with pytest.raises(ChangeError, match="no longer in your collection"):
        deckmods.apply_changes(conn, keen)
    assert _list(conn, keen) == before
    assert _deck(conn, "Keen Engineering")["version"] == 1
    assert len(deckmods.pending_changes(conn, keen)) == 2


def test_same_copy_cannot_be_used_twice(conn):
    keen, reign = _deck(conn, "Keen Engineering")["id"], _deck(conn, "Reign of Dragons")["id"]
    lot = _lot(conn, reign, "Blasphemous Act")
    deckmods.add_owned(conn, keen, lot)
    with pytest.raises(ChangeError, match="already in the change list"):
        deckmods.add_owned(conn, keen, lot)
    with pytest.raises(ChangeError, match="already on this deck's list"):
        deckmods.add_owned(conn, keen, _lot(conn, keen, "Counterspell"))

    a = deckmods.add_owned(conn, keen, _lot(conn, reign, "Chaos Warp"))
    b = deckmods.add_owned(conn, keen, _lot(conn, reign, "Lightning Bolt"))
    cut = _lot(conn, keen, "Counterspell")
    deckmods.set_cut(conn, keen, a, cut)
    with pytest.raises(ChangeError, match="already being replaced"):
        deckmods.set_cut(conn, keen, b, cut)
    with pytest.raises(ChangeError, match="isn't on this deck's list"):
        deckmods.set_cut(conn, keen, b, _lot(conn, reign, "Anger"))
    # The cut picker offers each name once, skipping copies already taken.
    names = [o["card_name"] for o in deckmods.cut_options(conn, keen)]
    assert "Counterspell" not in names
    assert "Island" in names


def test_replacing_the_commander_clears_it(conn):
    keen, reign = _deck(conn, "Keen Engineering")["id"], _deck(conn, "Reign of Dragons")["id"]
    change = deckmods.add_owned(conn, keen, _lot(conn, reign, "Lathliss, Dragon Queen"))
    deckmods.set_cut(conn, keen, change, _lot(conn, keen, "Sai, Master Thopterist"))
    deckmods.apply_changes(conn, keen)
    assert _deck(conn, "Keen Engineering")["commander"] is None


def _scryfall(monkeypatch, handler):
    install_transport(monkeypatch, httpx.MockTransport(handler))


def test_planned_cards_become_wanted_on_apply(conn, monkeypatch, cache_dir):
    card = {"id": "planned-1", "name": "Rhystic Study", "set": "pcy", "collector_number": "45",
            "finishes": ["nonfoil"], "type_line": "Enchantment", "cmc": 3, "colors": ["U"],
            "image_uris": {"normal": "https://img/rhystic.jpg"}}
    _scryfall(monkeypatch, lambda request: httpx.Response(200, json=card))
    keen, reign = _deck(conn, "Keen Engineering")["id"], _deck(conn, "Reign of Dragons")["id"]

    planned = deckmods.add_planned(conn, keen, "rhystic")
    [change] = deckmods.pending_changes(conn, keen)
    assert (change["id"], change["add_card_name"], change["ready"]) == (planned, "Rhystic Study", False)
    assert (cache_dir / "cards" / "planned-1.json").exists()  # cached for images/types
    with pytest.raises(ChangeError, match="not Rhystic Study"):
        deckmods.link_owned(conn, keen, planned, _lot(conn, reign, "Chaos Warp"))

    deckmods.set_cut(conn, keen, planned, _lot(conn, keen, "Counterspell"))
    lots_before = conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0]
    assert deckmods.apply_changes(conn, keen) == 2

    # On the list as wanted -- never a lot, so nothing enters the collection.
    wants = conn.execute(
        "SELECT card_name, scryfall_id FROM deck_wants WHERE deck_id = ?", (keen,)
    ).fetchall()
    assert [tuple(w) for w in wants] == [("Rhystic Study", "planned-1")]
    assert conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0] == lots_before
    assert deckmods.pending_changes(conn, keen) == []
    assert deckmods.list_size(conn, keen) == 100  # 99 owned + 1 wanted
    [v2, _] = deckmods.versions(conn, keen)
    assert v2["added"] == [("Rhystic Study", 1)]
    assert v2["removed"] == [("Counterspell", 1)]
    assert v2["card_count"] == 100


def test_no_changes_to_apply(conn):
    with pytest.raises(ChangeError, match="no changes to apply"):
        deckmods.apply_changes(conn, _deck(conn, "Keen Engineering")["id"])


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"object": "error", "type": "ambiguous", "details": "Too many"}, "matches several cards"),
        ({"object": "error", "details": "No cards found"}, "no card named"),
    ],
)
def test_planned_lookup_errors(conn, monkeypatch, body, message):
    def handler(request):
        if request.url.path == "/cards/collection":  # not an exact name, so the fuzzy lookup decides
            return httpx.Response(200, json={"data": [], "not_found": [{"name": "sol"}]})
        return httpx.Response(404, json=body)

    _scryfall(monkeypatch, handler)
    keen = _deck(conn, "Keen Engineering")["id"]
    with pytest.raises(ChangeError, match=message):
        deckmods.add_planned(conn, keen, "sol")
    assert deckmods.pending_changes(conn, keen) == []


def test_modify_pages_end_to_end(client, db_path):
    conn = db.connect(db_path)
    keen, reign = _deck(conn, "Keen Engineering")["id"], _deck(conn, "Reign of Dragons")["id"]
    incoming, outgoing = _lot(conn, reign, "Blasphemous Act"), _lot(conn, keen, "Counterspell")
    conn.close()

    assert f'href="/decks/{keen}/modify"' in client.get(f"/decks/{keen}").text
    search = client.get(f"/decks/{keen}/modify", params={"q": "blasphemous"}).text
    assert "Blasphemous Act" in search
    assert "1 in Reign of Dragons" in search

    resp = client.post(f"/decks/{keen}/changes", data={"lot_id": str(incoming)}, follow_redirects=False)
    assert resp.status_code == 303
    page = client.get(resp.headers["location"]).text
    assert "takes it from Reign of Dragons" in page
    change_id = int(page.split(f"/decks/{keen}/changes/", 1)[1].split("/", 1)[0])

    client.post(f"/decks/{keen}/changes/{change_id}/cut", data={"cut_lot_id": str(outgoing)})
    page = client.get(f"/decks/{keen}/modify").text
    assert "list 100 → 100 cards" in page
    assert "Apply as v2" in page

    resp = client.post(f"/decks/{keen}/apply", data={"note": "Wrath"}, follow_redirects=False)
    assert "Applied+as+version+2" in resp.headers["location"]
    history = client.get(f"/decks/{keen}/modify").text
    assert "+1 Blasphemous Act" in history
    assert "\N{MINUS SIGN}1 Counterspell" in history

    version = client.get(f"/decks/{keen}/versions/1").text
    assert "Counterspell" in version
    assert client.get(f"/decks/{keen}/versions/9").status_code == 404

    bad = client.post(f"/decks/{keen}/changes/{change_id}/delete", follow_redirects=False)
    assert "error=" in bad.headers["location"]  # already applied -> not pending
    assert client.post(f"/decks/{keen}/apply", headers={"Origin": "https://evil.example"}).status_code == 403


def test_modify_page_shows_readable_cards_and_one_shared_picker(client, db_path):
    import json
    import re

    conn = db.connect(db_path)
    keen, reign = _deck(conn, "Keen Engineering")["id"], _deck(conn, "Reign of Dragons")["id"]
    deckmods.add_owned(conn, keen, _lot(conn, reign, "Blasphemous Act"))
    deckmods.add_owned(conn, keen, _lot(conn, reign, "Chaos Warp"))
    conn.close()

    html = client.get(f"/decks/{keen}/modify").text
    assert html.count('id="cut-picker"') == 1  # rendered once, shared by every change
    assert html.count('data-picker="cut-picker"') == 2
    picks = re.findall(r'class="pick-tile"[^>]*>\s*<figure class="[^"]*" data-card=\'([^\']*)\'', html)
    names = [json.loads(p)["name"] for p in picks]
    assert "Counterspell" in names  # the deck's cards, each with hover/panel data
    assert "Blasphemous Act" not in names
    assert 'id="card-panel"' in html
    inserts = re.findall(r'Add</div>\s*<figure class="[^"]*" data-card=\'([^\']*)\'', html)
    assert [json.loads(i)["where"] for i in inserts] == ["In Reign of Dragons (would be borrowed)"] * 2


def test_link_a_planned_change_through_the_web(client, db_path):
    conn = db.connect(db_path)
    keen, reign = _deck(conn, "Keen Engineering")["id"], _deck(conn, "Reign of Dragons")["id"]
    copy = _lot(conn, reign, "Blasphemous Act")
    change = conn.execute(
        "INSERT INTO deck_changes (deck_id, add_card_name, created_at)"
        " VALUES (?, 'Blasphemous Act', '2026-10-01')", (keen,),
    ).lastrowid
    conn.commit()
    url = f"/decks/{keen}/changes/{change}/link"

    missing = client.post(url, data={}, follow_redirects=False)
    assert "error=Pick+a+copy+to+use" in missing.headers["location"]
    resp = client.post(url, data={"lot_id": str(copy)}, follow_redirects=False)
    assert resp.status_code == 303
    assert "notice=Linked+your+copy" in resp.headers["location"]
    assert conn.execute("SELECT add_lot_id FROM deck_changes WHERE id = ?", (change,)).fetchone()[0] == copy
    conn.close()


def test_planned_adds_use_the_cache_then_one_lookup(monkeypatch, cache_dir):
    """A list's worth of planned adds: cached names never reach Scryfall,
    the rest go in one collection request, not one request per card."""
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/cards/collection":
            idents = json.loads(request.content)["identifiers"]
            return httpx.Response(200, json={"data": [], "not_found": idents})
        return httpx.Response(404, json={"object": "error", "details": "No cards found"})

    _scryfall(monkeypatch, handler)
    cached = [c["name"] for c in scryfall.cached_cards()[:30]]
    assert [c["name"] for c in deckmods.resolve_names(cached)] == cached
    assert seen == []
    with pytest.raises(ChangeError, match="no card named"):
        deckmods.resolve_names([*cached, "Not A Real Card"])
    assert seen == ["/cards/collection", "/cards/named"]
