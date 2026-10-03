import json
import re

import httpx
import pytest

from mtgtracker import db, deckmods
from mtgtracker.deckmods import ChangeError
from mtgtracker.web import queries
from tests.conftest import install_transport

CARDS = {
    "saheeli, the sun's brilliance": {
        "id": "sah", "name": "Saheeli, the Sun's Brilliance", "set": "mkc", "collector_number": "1",
        "type_line": "Legendary Creature — Human Artificer", "cmc": 2, "colors": ["U", "R"],
        "color_identity": ["U", "R"], "mana_cost": "{U}{R}", "image_uris": {"normal": "https://img/sah.jpg"},
    },
    "counterspell": {"id": "cs", "name": "Counterspell", "set": "fdc", "collector_number": "1",
                     "colors": ["U"], "color_identity": ["U"], "type_line": "Instant", "cmc": 2},
    "blasphemous act": {"id": "ba", "name": "Blasphemous Act", "set": "fdc", "collector_number": "2",
                        "colors": ["R"], "color_identity": ["R"], "type_line": "Sorcery", "cmc": 9},
    "mystic forge": {"id": "mf", "name": "Mystic Forge", "set": "m20", "collector_number": "3",
                     "colors": [], "color_identity": [], "type_line": "Artifact", "cmc": 4,
                     "image_uris": {"normal": "https://img/mf.jpg"}},
    "island": {"id": "isl", "name": "Island", "set": "fdc", "collector_number": "9",
               "colors": [], "color_identity": [], "type_line": "Basic Land — Island", "cmc": 0},
}

LIST = """Commander
1 Saheeli, the Sun's Brilliance
Deck
1 Counterspell
1 Blasphemous Act
1 Mystic Forge
2 Island
1 Not A Real Card
"""


@pytest.fixture
def scryfall_api(monkeypatch, cache_dir):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cards/collection":
            idents = json.loads(request.content)["identifiers"]
            found = [CARDS[i["name"].lower()] for i in idents if i["name"].lower() in CARDS]
            return httpx.Response(200, json={"data": found, "not_found": []})
        return httpx.Response(404, json={"object": "error", "details": "No cards found"})

    install_transport(monkeypatch, httpx.MockTransport(handler))


def _equipped(conn, name):
    return conn.execute(
        "SELECT COUNT(*) FROM deck_cards JOIN decks ON decks.id = deck_cards.deck_id"
        " WHERE decks.name = ? AND deck_cards.equipped = 1", (name,)
    ).fetchone()[0]


def test_create_deck_from_list_is_unassembled(db_path, scryfall_api):
    conn = db.connect(db_path)
    lots_before = conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0]

    summary = deckmods.create_deck_from_list(conn, "Saheeli's Workshop", LIST)
    deck = conn.execute("SELECT * FROM decks WHERE id = ?", (summary.deck_id,)).fetchone()

    assert deck["commander"] == "Saheeli, the Sun's Brilliance"  # set even though not owned
    assert deck["version"] == 1
    # Owned copies are on the list but stay where they are.
    assert summary.owned == {"Keen Engineering": 3, "Reign of Dragons": 1}  # Counterspell + 2 Islands; Act
    assert conn.execute(
        "SELECT COUNT(*), SUM(equipped) FROM deck_cards WHERE deck_id = ?", (summary.deck_id,)
    ).fetchone()[:] == (4, 0)
    assert _equipped(conn, "Keen Engineering") == 100
    assert _equipped(conn, "Reign of Dragons") == 100
    # Unowned cards are wanted, never lots.
    assert sorted(r[0] for r in conn.execute(
        "SELECT card_name FROM deck_wants WHERE deck_id = ?", (summary.deck_id,)
    )) == ["Mystic Forge", "Saheeli, the Sun's Brilliance"]
    assert conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0] == lots_before
    assert summary.not_found == ["Not A Real Card"]
    assert summary.message().startswith(
        "Created with 6 cards: 4 you own (3 in Keen Engineering, 1 in Reign of Dragons), 2 to acquire."
    )

    detail = queries.get_deck(conn, summary.deck_id)
    assert detail["identity"] == ("U", "R")  # from the wanted commander
    counts = ("list_count", "equipped_count", "elsewhere_count", "wanted_count")
    assert tuple(detail[k] for k in counts) == (6, 0, 4, 2)
    assert [g["deck_name"] for g in detail["pull_from"]] == ["Reign of Dragons", "Keen Engineering"]
    assert detail["acquire"] == [("Mystic Forge", 1), ("Saheeli, the Sun's Brilliance", 1)]
    [v1] = deckmods.versions(conn, summary.deck_id)
    assert v1["card_count"] == 6
    conn.close()


def test_scryfall_failure_writes_nothing(db_path, monkeypatch, cache_dir):
    install_transport(monkeypatch, httpx.MockTransport(lambda request: httpx.Response(500)))
    conn = db.connect(db_path)
    decks_before = conn.execute("SELECT COUNT(*) FROM decks").fetchone()[0]
    with pytest.raises(httpx.HTTPStatusError):
        deckmods.create_deck_from_list(conn, "Broken", LIST)
    assert conn.execute("SELECT COUNT(*) FROM decks").fetchone()[0] == decks_before
    assert conn.execute("SELECT COUNT(*) FROM deck_wants").fetchone()[0] == 0
    conn.close()


def test_create_needs_a_name_and_cards(db_path, scryfall_api):
    conn = db.connect(db_path)
    with pytest.raises(ChangeError, match="name"):
        deckmods.create_deck_from_list(conn, "  ", LIST)
    with pytest.raises(ChangeError, match="no cards found"):
        deckmods.create_deck_from_list(conn, "Empty", "Sideboard\n1 Island\n")
    conn.close()


def test_wanted_commander_survives_apply_and_reimport_skips_wants(db_path, scryfall_api):
    conn = db.connect(db_path)
    deck_id = deckmods.create_deck_from_list(conn, "Saheeli's Workshop", LIST).deck_id
    reign = conn.execute("SELECT id FROM decks WHERE name = 'Reign of Dragons'").fetchone()[0]
    anger = conn.execute(
        "SELECT lots.id FROM lots JOIN deck_cards ON deck_cards.lot_id = lots.id"
        " WHERE deck_cards.deck_id = ? AND card_name = 'Anger'", (reign,)
    ).fetchone()[0]
    deckmods.add_owned(conn, deck_id, anger)
    assert deckmods.apply_changes(conn, deck_id) == 2
    commander = conn.execute("SELECT commander FROM decks WHERE id = ?", (deck_id,)).fetchone()[0]
    assert commander == "Saheeli, the Sun's Brilliance"

    again = deckmods.import_list(conn, deck_id, LIST)
    assert (again.owned, again.planned) == ([], [])
    assert "Mystic Forge" in again.in_deck
    conn.close()


def test_pages_show_the_three_states(client, db_path, scryfall_api):
    conn = db.connect(db_path)
    deck_id = deckmods.create_deck_from_list(conn, "Saheeli's Workshop", LIST).deck_id
    cards_before = client.get("/cards", params={"view": "list"}).text
    conn.close()

    html = client.get(f"/decks/{deck_id}").text
    assert 'data-identity="UR"' in html
    tiles = re.findall(r'<figure class="([^"]*)" data-card=\'([^\']*)\'', html)
    states = {json.loads(data)["name"]: cls.split() for cls, data in tiles}
    assert "wanted" in states["Mystic Forge"]
    assert "away" not in states["Mystic Forge"]
    assert "away" in states["Counterspell"]  # owned, in Keen Engineering
    panel = {json.loads(d)["name"]: json.loads(d) for _, d in tiles}
    assert panel["Mystic Forge"]["where"] == "Not owned yet: need to acquire"
    assert panel["Mystic Forge"]["wanted"] is True
    assert 'class="tile-flag acquire">To acquire<' in html
    # A wanted card has no lot, so no "needs printing" link (which would be /printings/None).
    assert "/printings/None" not in html
    assert "Pull from other decks" in html
    assert "Need to acquire" in html
    assert "To acquire</dt><dd>2</dd>" in html

    listing = client.get(f"/decks/{deck_id}", params={"view": "list"}).text
    assert re.search(r'<li class="deck-row wanted"[^>]*>\s*<span class="deck-row-qty">1</span>\s*'
                     r'<span class="deck-row-name">Mystic Forge <span class="tag acquire">', listing)

    decks = client.get("/decks").text
    assert "4 to pull" in decks
    assert "2 to acquire" in decks
    version = client.get(f"/decks/{deck_id}/versions/1").text
    assert "Mystic Forge" in version

    # Wanted cards never reach the collection or its totals.
    cards_after = client.get("/cards", params={"view": "list"}).text
    assert "Mystic Forge" not in cards_after
    assert cards_after.count("data-card=") == cards_before.count("data-card=")
    assert "Cards owned" in client.get("/").text


def test_new_deck_form(client, scryfall_api):
    resp = client.post(
        "/decks/new", data={"name": "Saheeli's Workshop", "decklist": LIST}, follow_redirects=False
    )
    assert resp.status_code == 303
    page = client.get(resp.headers["location"]).text
    assert "Created with 6 cards" in page
    assert "Saheeli&#39;s Workshop" in page
    bad = client.post("/decks/new", data={"name": "", "decklist": LIST}, follow_redirects=False)
    assert bad.headers["location"].startswith("/decks?error=")


def _workshop(db_path):
    conn = db.connect(db_path)
    deck_id = deckmods.create_deck_from_list(conn, "Saheeli's Workshop", LIST).deck_id
    return conn, deck_id


def _want(conn, deck_id, name):
    return conn.execute(
        "SELECT id FROM deck_wants WHERE deck_id = ? AND card_name = ?", (deck_id, name)
    ).fetchone()[0]


def _lot(conn, deck_name, card_name):
    return conn.execute(
        "SELECT lots.id FROM lots JOIN deck_cards ON deck_cards.lot_id = lots.id"
        " JOIN decks ON decks.id = deck_cards.deck_id"
        " WHERE decks.name = ? AND lots.card_name = ? ORDER BY lots.id",
        (deck_name, card_name),
    ).fetchone()[0]


def test_swap_a_wanted_card_for_one_you_own(db_path, scryfall_api):
    conn, deck_id = _workshop(db_path)
    forge = _want(conn, deck_id, "Mystic Forge")
    sol_ring = _lot(conn, "Keen Engineering", "Sol Ring")

    change = deckmods.add_swap(conn, deck_id, sol_ring, forge)
    [pending] = deckmods.pending_changes(conn, deck_id)
    assert (pending["id"], pending["cut_card_name"], pending["ready"]) == (change, "Mystic Forge", True)
    # The wanted card can't be swapped twice.
    with pytest.raises(ChangeError, match="already being replaced"):
        deckmods.add_swap(conn, deck_id, _lot(conn, "Reign of Dragons", "Sol Ring"), forge)
    groups = deckmods.wanted_groups(conn, deck_id)
    assert [g["want_id"] for g in groups if g["card_name"] == "Mystic Forge"] == [None]

    assert deckmods.apply_changes(conn, deck_id) == 2
    names = {r[0] for r in conn.execute("SELECT card_name FROM deck_wants WHERE deck_id = ?", (deck_id,))}
    assert names == {"Saheeli, the Sun's Brilliance"}
    on_list = {r[0] for r in conn.execute("SELECT lot_id FROM deck_cards WHERE deck_id = ?", (deck_id,))}
    assert sol_ring in on_list
    [v2, _] = deckmods.versions(conn, deck_id)
    assert v2["added"] == [("Sol Ring", 1)]
    assert v2["removed"] == [("Mystic Forge", 1)]
    conn.close()


def test_queued_change_can_replace_a_wanted_card(db_path, scryfall_api):
    conn, deck_id = _workshop(db_path)
    change = deckmods.add_owned(conn, deck_id, _lot(conn, "Reign of Dragons", "Anger"))
    forge = _want(conn, deck_id, "Mystic Forge")
    counterspell = _lot(conn, "Keen Engineering", "Counterspell")
    with pytest.raises(ChangeError, match="one card"):
        deckmods.set_cut(conn, deck_id, change, counterspell, cut_want_id=forge)
    with pytest.raises(ChangeError, match="to-acquire list"):
        deckmods.set_cut(conn, deck_id, change, None, cut_want_id=999999)
    deckmods.set_cut(conn, deck_id, change, None, cut_want_id=forge)
    deckmods.apply_changes(conn, deck_id)
    assert conn.execute("SELECT COUNT(*) FROM deck_wants WHERE id = ?", (forge,)).fetchone()[0] == 0
    conn.close()


def test_owned_fitting_respects_deck_colors(db_path, scryfall_api, cache_dir):
    # Give two owned cards a color identity: a green one doesn't fit blue-red.
    path = cache_dir / "fdc.json"
    cards = json.loads(path.read_text())
    for card in cards:
        if card["name"] == "Chaos Warp":
            card["color_identity"] = ["R"]
        if card["name"] == "Anger":
            card["color_identity"] = ["G"]  # pretend, to test the filter
    path.write_text(json.dumps(cards))
    conn, deck_id = _workshop(db_path)

    names = {r["card_name"] for r in deckmods.owned_fitting(conn, deck_id, ("U", "R"))}
    assert "Chaos Warp" in names
    assert "Anger" not in names
    assert "Counterspell" not in names  # already on this deck's list
    conn.close()


def test_swap_through_the_modify_page(client, db_path, scryfall_api):
    conn, deck_id = _workshop(db_path)
    forge = _want(conn, deck_id, "Mystic Forge")
    sol_ring = _lot(conn, "Keen Engineering", "Sol Ring")
    conn.close()

    page = client.get(f"/decks/{deck_id}/modify").text
    assert "Cards you don't own" in page
    assert page.count('id="own-picker"') == 1
    assert f'data-want-id="{forge}"' in page
    assert re.search(rf'name="lot_id" value="{sol_ring}" class="pick-tile"', page)

    resp = client.post(f"/decks/{deck_id}/swaps", data={"lot_id": str(sol_ring), "want_id": str(forge)},
                       follow_redirects=False)
    assert "Queued+the+swap" in resp.headers["location"]
    page = client.get(f"/decks/{deck_id}/modify").text
    assert f'data-want-id="{forge}"' not in page  # no longer offered while queued
    bad = client.post(f"/decks/{deck_id}/swaps", data={"lot_id": str(sol_ring)}, follow_redirects=False)
    assert "error=" in bad.headers["location"]
