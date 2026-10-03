import json

import httpx
import pytest

from mtgtracker import db, deckmods
from mtgtracker.deckmods import ChangeError
from mtgtracker.importers.decklist_text import parse
from tests.conftest import install_transport


def test_parse_common_export_formats():
    parsed = parse(
        "1 Academy Ruins\n"
        "1x Sol Ring\n"
        "Sol Ring (CMM) 400 *F*\n"
        "2 Island\n"
        "\n"
        "Commander:\n"
        "1 Shuri, Wakandan Inventor\n"
        "Fire // Ice\n"
        "// a comment\n"
        "Sideboard (2)\n"
        "1 Swords to Plowshares\n"
        "1 Path to Exile\n"
    )
    assert [(e.qty, e.name, e.set_code, e.collector_number, e.finish) for e in parsed.entries] == [
        (1, "Academy Ruins", None, None, "nonfoil"),
        (1, "Sol Ring", None, None, "nonfoil"),
        (1, "Sol Ring", "cmm", "400", "foil"),
        (2, "Island", None, None, "nonfoil"),
        (1, "Shuri, Wakandan Inventor", None, None, "nonfoil"),
        (1, "Fire // Ice", None, None, "nonfoil"),
    ]
    assert parsed.card_count == 7
    assert parsed.skipped == ["1 Swords to Plowshares", "1 Path to Exile"]
    assert parsed.errors == []


def test_parse_rejects_zero_quantity():
    assert parse("0 Sol Ring").errors == ["0 Sol Ring"]


SCRYFALL = {
    "counterspell": {"id": "cs", "name": "Counterspell", "set": "fdc", "collector_number": "1"},
    "blasphemous act": {"id": "ba", "name": "Blasphemous Act", "set": "fdc", "collector_number": "2"},
    "mystic forge": {"id": "mf", "name": "Mystic Forge", "set": "m20", "collector_number": "3",
                     "type_line": "Artifact", "image_uris": {"normal": "https://img/mf.jpg"}},
}


@pytest.fixture
def scryfall_api(monkeypatch, cache_dir):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/cards/collection":
            idents = json.loads(request.content)["identifiers"]
            found = [SCRYFALL[i["name"].lower()] for i in idents if i["name"].lower() in SCRYFALL]
            missing = [i for i in idents if i["name"].lower() not in SCRYFALL]
            return httpx.Response(200, json={"data": found, "not_found": missing})
        return httpx.Response(404, json={"object": "error", "details": "No cards found"})  # fuzzy retry

    install_transport(monkeypatch, httpx.MockTransport(handler))
    return calls


def _deck(conn, name):
    return conn.execute("SELECT id FROM decks WHERE name = ?", (name,)).fetchone()[0]


LIST = "1 Counterspell\n1 Blasphemous Act\n1 Mystic Forge\n1 Totally Made Up\nSideboard\n1 Mystic Forge\n"


def test_import_queues_owned_and_planned_and_skips_the_rest(db_path, scryfall_api, cache_dir):
    conn = db.connect(db_path)
    keen = _deck(conn, "Keen Engineering")

    summary = deckmods.import_list(conn, keen, LIST)
    assert summary.owned == ["Blasphemous Act"]  # lives in Reign of Dragons
    assert summary.planned == ["Mystic Forge"]
    assert summary.in_deck == ["Counterspell"]
    assert summary.not_found == ["Totally Made Up"]
    assert summary.skipped_lines == 1
    assert scryfall_api.count("/cards/collection") == 1  # one batched request

    changes = {c["add_card_name"]: c for c in deckmods.pending_changes(conn, keen)}
    assert set(changes) == {"Blasphemous Act", "Mystic Forge"}
    assert changes["Blasphemous Act"]["ready"]
    assert changes["Blasphemous Act"]["add_from_deck_name"] == "Reign of Dragons"
    assert not changes["Mystic Forge"]["ready"]
    assert (cache_dir / "cards" / "mf.json").exists()  # planned card gets an image/type

    message = summary.message()
    assert message.startswith("Queued 2: 1 you own, 1 to acquire.")
    assert "Already in the deck (1): Counterspell." in message
    assert "Not found on Scryfall (1): Totally Made Up." in message

    # Importing the same list again queues nothing new.
    again = deckmods.import_list(conn, keen, LIST)
    assert (again.owned, again.planned) == ([], [])
    assert sorted(again.queued) == ["Blasphemous Act", "Mystic Forge"]
    assert len(deckmods.pending_changes(conn, keen)) == 2
    conn.close()


def test_import_needs_readable_cards(db_path, scryfall_api):
    conn = db.connect(db_path)
    with pytest.raises(ChangeError, match="no cards found"):
        deckmods.import_list(conn, _deck(conn, "Keen Engineering"), "Sideboard\n1 Mystic Forge\n")
    conn.close()


def test_import_through_the_web(client, db_path, scryfall_api):
    conn = db.connect(db_path)
    keen = _deck(conn, "Keen Engineering")
    conn.close()

    assert "Import a list" in client.get(f"/decks/{keen}/modify").text
    resp = client.post(f"/decks/{keen}/import", data={"decklist": LIST}, follow_redirects=False)
    assert resp.status_code == 303
    page = client.get(resp.headers["location"]).text
    assert "Queued 2: 1 you own, 1 to acquire." in page
    assert "Mystic Forge" in page
    assert "to acquire" in page

    empty = client.post(f"/decks/{keen}/import", data={"decklist": "\n"}, follow_redirects=False)
    assert "error=" in empty.headers["location"]
