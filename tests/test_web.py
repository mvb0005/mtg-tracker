import json
import re

import httpx
from fastapi.testclient import TestClient

from mtgtracker import db, printings
from mtgtracker.decks import borrow, create_deck
from mtgtracker.web.app import create_app, format_cents
from tests.conftest import install_transport


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

    resp = client.get(f"/decks/{keen}", params={"view": "list"})
    assert resp.status_code == 200
    html = resp.text
    # 34 Islands collapse into one grouped row.
    assert re.search(r'<span class="deck-row-qty">34</span>\s*<span class="deck-row-name">Island', html)
    # The borrowed Counterspell's row is greyed out; the panel data says where it is.
    away_row = re.search(r'<li class="deck-row away" data-card=\'([^\']*)\'', html)
    assert json.loads(away_row.group(1))["where"] == "Lent to Blue Pile"
    assert "Pull from other decks" in html
    assert re.search(r'<a href="/decks/\d+">Blue Pile</a> <span class="subtle">· 1</span>', html)
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


def _unresolved_lot(db_path, card_name: str, deck_name: str) -> int:
    conn = db.connect(db_path)
    lot = conn.execute(
        """
        SELECT MIN(lots.id) FROM lots
        JOIN deck_cards ON deck_cards.lot_id = lots.id AND deck_cards.equipped = 1
        JOIN decks ON decks.id = deck_cards.deck_id
        WHERE lots.card_name = ? AND decks.name = ? AND lots.scryfall_id IS NULL
        """,
        (card_name, deck_name),
    ).fetchone()[0]
    conn.close()
    return lot


def _scryfall_id(card_name: str, number: str) -> str:
    return next(c["id"] for c in printings.find_printings(card_name) if c["collector_number"] == number)


def test_printings_queue_and_resolve_page_render(client, db_path):
    resp = client.get("/printings")
    assert resp.status_code == 200
    assert "Sol Ring" in resp.text
    assert "Basic lands" in resp.text

    lot = _unresolved_lot(db_path, "Sol Ring", "Keen Engineering")
    resp = client.get(f"/printings/{lot}")
    assert resp.status_code == 200
    assert "FDC #286" in resp.text
    assert "FDC #2<" in resp.text  # foil-only promo is still offered
    assert 'type="radio"' in resp.text  # single card -> pick one
    assert "(as recorded)" in resp.text


def test_resolve_single_card_via_form(client, db_path):
    lot = _unresolved_lot(db_path, "Sol Ring", "Keen Engineering")
    choice = f"{_scryfall_id('Sol Ring', '286')}|nonfoil"

    resp = client.post(f"/printings/{lot}", data={"choice": choice}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/printings?notice=")

    conn = db.connect(db_path)
    row = conn.execute("SELECT set_code, collector_number FROM lots WHERE id = ?", (lot,)).fetchone()
    conn.close()
    assert tuple(row) == ("fdc", "286")
    assert client.get(f"/printings/{lot}").status_code == 404  # no longer unresolved
    assert "Pinned 1 Sol Ring." in client.get(resp.headers["location"]).text


def test_invalid_assignment_is_400_and_writes_nothing(client, db_path):
    lot = _unresolved_lot(db_path, "Sol Ring", "Keen Engineering")
    promo = _scryfall_id("Sol Ring", "2")  # foil only

    resp = client.post(f"/printings/{lot}", data={"choice": f"{promo}|nonfoil"})
    assert resp.status_code == 400
    assert "doesn&#39;t come in nonfoil" in resp.text

    resp = client.post(f"/printings/{lot}", data={f"qty|{promo}|foil": "5"})
    assert resp.status_code == 400
    assert "only 1 are unresolved" in resp.text

    resp = client.post(f"/printings/{lot}", data={f"qty|{promo}|foil": "lots"})
    assert resp.status_code == 400

    conn = db.connect(db_path)
    assert conn.execute("SELECT scryfall_id FROM lots WHERE id = ?", (lot,)).fetchone()[0] is None
    conn.close()


def test_cross_origin_post_is_rejected(client, db_path):
    lot = _unresolved_lot(db_path, "Sol Ring", "Keen Engineering")
    choice = f"{_scryfall_id('Sol Ring', '286')}|nonfoil"

    resp = client.post(
        f"/printings/{lot}", data={"choice": choice}, headers={"Origin": "https://evil.example"}
    )
    assert resp.status_code == 403
    resp = client.post(f"/printings/{lot}", data={"choice": choice}, headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403

    # Same-origin (what a browser sends for our own form) is fine.
    resp = client.post(
        f"/printings/{lot}", data={"choice": choice},
        headers={"Origin": "http://testserver"}, follow_redirects=False,
    )
    assert resp.status_code == 303


def test_basics_lookup_set_then_split_quantities(client, db_path, cache_dir, monkeypatch):
    lot = _unresolved_lot(db_path, "Island", "Keen Engineering")
    page = client.get(f"/printings/{lot}")
    assert "No printings of Island in the cached sets" in page.text

    islands = [
        {"id": f"fdn-island-{n}", "name": "Island", "set": "fdn", "collector_number": n,
         "finishes": ["nonfoil", "foil"], "image_uris": {"normal": f"https://img/{n}.jpg"}}
        for n in ("272", "273")
    ]
    requests = []

    def scryfall_api(request: httpx.Request) -> httpx.Response:
        requests.append(request.url)
        return httpx.Response(200, json={"data": islands, "has_more": False})

    install_transport(monkeypatch, httpx.MockTransport(scryfall_api))
    resp = client.post(f"/printings/{lot}/fetch-set", data={"set_code": "FDN"})
    assert resp.status_code == 200
    assert "Loaded 2 printings from FDN." in resp.text
    assert "https://img/272.jpg" in resp.text
    assert 'name="qty|fdn-island-272|nonfoil"' in resp.text
    assert len(requests) == 1
    assert "e%3Afdn" in str(requests[0])

    # Page loads stay offline -- the set is cached now.
    client.get(f"/printings/{lot}")
    assert len(requests) == 1

    resp = client.post(
        f"/printings/{lot}",
        data={"qty|fdn-island-272|nonfoil": "30", "qty|fdn-island-273|nonfoil": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "Pinned+30+Island" in resp.headers["location"]
    remaining = client.get(resp.headers["location"])
    assert "<h1>4\N{MULTIPLICATION SIGN} Island</h1>" in remaining.text


def test_fetch_set_errors_are_reported(client, db_path, monkeypatch):
    lot = _unresolved_lot(db_path, "Island", "Keen Engineering")

    resp = client.post(f"/printings/{lot}/fetch-set", data={"set_code": "../etc"})
    assert "doesn&#39;t look like a set code" in resp.text

    install_transport(monkeypatch, httpx.MockTransport(lambda r: httpx.Response(404, json={})))
    resp = client.post(f"/printings/{lot}/fetch-set", data={"set_code": "nope"})
    assert "Scryfall has no set &#39;nope&#39;" in resp.text


def _add_art(cache_dir, **by_number) -> dict[str, str]:
    """Give fixture printings (by fdc collector number) an image and a type
    line, as the real Scryfall cache has. Returns number -> image URL."""
    path = cache_dir / "fdc.json"
    cards = json.loads(path.read_text())
    urls = {}
    for card in cards:
        if card["collector_number"] in by_number:
            urls[card["collector_number"]] = f"https://img/fdc/{card['collector_number']}.jpg"
            card["image_uris"] = {"normal": urls[card["collector_number"]]}
            card["type_line"] = by_number[card["collector_number"]]
    path.write_text(json.dumps(cards))
    return urls


def test_deck_grid_shows_images_sections_and_likely_printings(client, db_path, cache_dir):
    conn = db.connect(db_path)
    keen = conn.execute("SELECT id FROM decks WHERE name = 'Keen Engineering'").fetchone()["id"]
    counterspell = conn.execute(
        "SELECT collector_number FROM lots WHERE card_name = 'Counterspell'"
    ).fetchone()[0]
    conn.close()
    urls = _add_art(cache_dir, **{counterspell: "Instant", "2": "Artifact", "286": "Artifact"})

    html = client.get(f"/decks/{keen}").text
    # Pinned card shows its own printing, filed under its type.
    assert urls[counterspell] in html
    assert re.search(r"<h3>Instants <span class=\"subtle\">\(1\)</span></h3>", html)
    # Unpinned nonfoil Sol Ring shows the regular printing, not the foil-only promo.
    assert urls["286"] in html
    assert urls["2"] not in html
    assert "<h3>Artifacts" in html
    # Basics with no cached printing get a placeholder, grouped as lands.
    assert '<div class="tile-placeholder"><span>Island</span></div>' in html
    assert '<span class="tile-qty">34\N{MULTIPLICATION SIGN}</span>' in html
    assert "<h3>Lands" in html
    # Unpinned non-basics get an actionable flag; basics don't.
    assert html.count('class="tile-flag"') == 2  # Sol Ring + Arcane Signet
    assert "<span class=\"subtle\">FDC #" not in html  # printing lives in the hover panel


def test_cards_grid_and_list_views(client, cache_dir):
    urls = _add_art(cache_dir, **{"286": "Artifact"})
    grid = client.get("/cards", params={"q": "sol ring"}).text
    assert urls["286"] in grid
    assert "Showing 2 of 200 cards" in grid
    assert 'href="/cards?q=sol%20ring&amp;view=list"' in grid

    listing = client.get("/cards", params={"q": "sol ring", "view": "list"}).text
    assert f'src="{urls["286"]}"' not in listing  # no images drawn, only panel data
    assert '<section class="deck-section">' in listing
    assert re.search(r"<h3>Artifacts</h3>\s*<span>2 cards</span>", listing)
    assert 'name="view" value="list"' in listing  # search keeps the list view


def test_stylesheet_is_versioned_and_cached(client):
    html = client.get("/").text
    href = re.search(r'<link rel="stylesheet" href="([^"]*/static/style\.css[^"]*)"', html).group(1)
    assert re.search(r"/static/style\.css\?v=[0-9a-f]{12}$", href)

    versioned = client.get(href)
    assert versioned.status_code == 200
    assert "immutable" in versioned.headers["cache-control"]
    assert client.get("/static/style.css").headers["cache-control"] == "no-cache"


def _set_cmc(cache_dir, by_name: dict[str, float], type_line: str = "Artifact") -> None:
    path = cache_dir / "fdc.json"
    cards = json.loads(path.read_text())
    for card in cards:
        if card["name"] in by_name:
            card["cmc"] = by_name[card["name"]]
            card["type_line"] = type_line
    path.write_text(json.dumps(cards))


def test_seeded_decks_record_their_commander(db_path):
    conn = db.connect(db_path)
    commanders = dict(conn.execute("SELECT name, commander FROM decks").fetchall())
    conn.close()
    assert commanders == {
        "Reign of Dragons": "Lathliss, Dragon Queen",
        "Keen Engineering": "Sai, Master Thopterist",
    }


def test_deck_shows_commander_first_then_highest_mana_value(client, db_path, cache_dir):
    _set_cmc(cache_dir, {"Sol Ring": 1, "Arcane Signet": 2, "Mind's Eye": 5, "Sai, Master Thopterist": 3})
    conn = db.connect(db_path)
    keen = conn.execute("SELECT id FROM decks WHERE name = 'Keen Engineering'").fetchone()["id"]
    conn.close()

    html = client.get(f"/decks/{keen}").text
    headings = re.findall(r"<h3>([A-Za-z]+) ", html)
    assert headings[0] == "Commander"
    commander_section = html.split("<h3>Commander", 1)[1].split("<h3>", 1)[0]
    assert "Sai, Master Thopterist" in commander_section
    assert "Sol Ring" not in commander_section

    artifacts = html.split("<h3>Artifacts", 1)[1].split("<h3>", 1)[0]
    order = [artifacts.index(f'alt="{name}"') if f'alt="{name}"' in artifacts
             else artifacts.index(f"<span>{name}</span>")
             for name in ("Mind&#39;s Eye", "Arcane Signet", "Sol Ring")]
    assert order == sorted(order)

    rows = client.get(f"/decks/{keen}", params={"view": "list"}).text
    names = re.findall(r'<span class="deck-row-name">([^<]+?)(?: <a|<span|</span>)', rows)
    assert names[0] == "Sai, Master Thopterist"
    assert re.findall(r"<h3>([^<]+)</h3>", rows)[0] == "Commander"
    assert names.index("Mind&#39;s Eye") < names.index("Arcane Signet") < names.index("Sol Ring")


def test_cards_ordered_by_mana_value(client, cache_dir):
    _set_cmc(cache_dir, {"Sol Ring": 1, "Arcane Signet": 2})
    html = client.get("/cards", params={"view": "list", "q": "i"}).text
    names = re.findall(r'<span class="deck-row-name">([^<]+?)(?: <a|<span|</span>)', html)
    assert names.index("Arcane Signet") < names.index("Sol Ring")


def _type_cards(cache_dir, type_lines: dict[str, str]) -> None:
    path = cache_dir / "fdc.json"
    cards = json.loads(path.read_text())
    for card in cards:
        if card["name"] in type_lines:
            card["type_line"] = type_lines[card["name"]]
    path.write_text(json.dumps(cards))


def test_choose_commander_from_deck_page(client, db_path, cache_dir):
    _type_cards(cache_dir, {
        "Sai, Master Thopterist": "Legendary Creature — Human Artificer",
        "Padeem, Consul of Innovation": "Legendary Creature — Vedalken Artificer",
    })
    conn = db.connect(db_path)
    keen = conn.execute("SELECT id FROM decks WHERE name = 'Keen Engineering'").fetchone()["id"]
    conn.close()

    page = client.get(f"/decks/{keen}").text
    assert "Commander: <strong>Sai, Master Thopterist</strong>" in page
    assert 'value="Padeem, Consul of Innovation"' in page
    assert 'value="Counterspell"' not in page

    resp = client.post(
        f"/decks/{keen}/commander", data={"commander": "Padeem, Consul of Innovation"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    page = client.get(resp.headers["location"]).text
    assert "Padeem, Consul of Innovation is now the commander." in page
    commander_section = page.split("<h3>Commander", 1)[1].split("<h3>", 1)[0]
    assert "Padeem, Consul of Innovation" in commander_section
    assert "Sai, Master Thopterist" not in commander_section

    resp = client.post(f"/decks/{keen}/commander", data={"commander": "Counterspell"})
    assert "Counterspell isn&#39;t a legendary creature" in resp.text

    resp = client.post(f"/decks/{keen}/commander", data={"commander": ""})
    page = resp.text
    assert "Commander cleared." in page
    assert "<h3>Commander" not in page
    assert "Choose a commander" in page

    assert client.post("/decks/9999/commander", data={"commander": ""}).status_code == 404
    cross = client.post(
        f"/decks/{keen}/commander", data={"commander": ""}, headers={"Origin": "https://evil.example"}
    )
    assert cross.status_code == 403


def _panel_payloads(html: str) -> list[dict]:
    return [json.loads(raw) for raw in re.findall(r"data-card='([^']*)'", html)]


def test_card_panel_data_on_tiles_and_rows(client, db_path, cache_dir):
    path = cache_dir / "fdc.json"
    cards = json.loads(path.read_text())
    for card in cards:
        if card["name"] == "Sol Ring":
            card.update(mana_cost="{1}", type_line="Artifact", oracle_text="{T}: Add {C}{C}.",
                        rarity="uncommon", set_name="Foundations Commander", artist="Mike Bierek")
    path.write_text(json.dumps(cards))
    conn = db.connect(db_path)
    keen = conn.execute("SELECT id FROM decks WHERE name = 'Keen Engineering'").fetchone()["id"]
    conn.close()

    for url in (f"/decks/{keen}", f"/decks/{keen}?view=list", "/cards", "/cards?view=list"):
        html = client.get(url).text
        assert 'id="card-panel"' in html, url
        assert '<dialog id="card-dialog"' in html, url  # tap-to-open sheet (phones)
        assert 'class="card-dialog-position' in html, url  # "3 / 69" while paging
        assert re.search(r'card-panel\.js\?v=[0-9a-f]{12}"', html), url
        payloads = _panel_payloads(html)
        sol = next(p for p in payloads if p["name"] == "Sol Ring")
        assert sol["faces"] == [{"name": "Sol Ring", "mana_cost": "{1}", "type_line": "Artifact",
                                 "oracle_text": "{T}: Add {C}{C}.", "stats": None}], url
        assert sol["printing"] is None  # not pinned yet
        assert sol["exact"] is False
        assert sol["cost"].startswith("$")
        # Deck pages: this deck. Cards page: both decks' copies merged into one row.
        assert sol["where"] in {"In this deck", "Keen Engineering, Reign of Dragons"}, url

    # Every row/tile carries a payload; names with apostrophes survive the
    # single-quoted attribute.
    deck_payloads = _panel_payloads(client.get(f"/decks/{keen}").text)
    assert sum(p["qty"] for p in deck_payloads) == 100
    assert any(p["name"] == "Mind's Eye" for p in deck_payloads)

    # Pages without cards don't load the panel.
    assert 'id="card-panel"' not in client.get("/transactions").text


def test_cards_not_in_this_deck_are_greyed_out(client, db_path):
    conn = db.connect(db_path)
    keen = conn.execute("SELECT id FROM decks WHERE name = 'Keen Engineering'").fetchone()["id"]
    lot = conn.execute(
        "SELECT lots.id FROM lots JOIN deck_cards ON deck_cards.lot_id = lots.id"
        " WHERE deck_cards.deck_id = ? AND lots.card_name = 'Counterspell'",
        (keen,),
    ).fetchone()["id"]
    borrow(conn, lot, create_deck(conn, "Blue Pile", "2026-09-28"))
    conn.close()

    html = client.get(f"/decks/{keen}").text
    tiles = re.findall(r'<figure class="([^"]*)" data-card=\'([^\']*)\'', html)
    payloads = [(cls.split(), json.loads(data)) for cls, data in tiles]
    away = {card["name"]: card["where"] for classes, card in payloads if "away" in classes}
    assert away == {"Counterspell": "Lent to Blue Pile"}
    assert ">here<" not in html


def test_commander_picker_options_carry_panel_data(client, db_path, cache_dir):
    _type_cards(cache_dir, {
        "Sai, Master Thopterist": "Legendary Creature — Human Artificer",
        "Padeem, Consul of Innovation": "Legendary Creature — Vedalken Artificer",
    })
    conn = db.connect(db_path)
    keen = conn.execute("SELECT id FROM decks WHERE name = 'Keen Engineering'").fetchone()["id"]
    conn.close()

    html = client.get(f"/decks/{keen}").text
    picker = html.split('class="commander-picker"', 1)[1].split("</details>", 1)[0]
    options = re.findall(r'<label class="picker-option" data-card=\'([^\']*)\'>', picker)
    payloads = {json.loads(raw)["name"]: json.loads(raw) for raw in options}
    assert set(payloads) == {"Sai, Master Thopterist", "Padeem, Consul of Innovation"}
    assert payloads["Padeem, Consul of Innovation"]["where"] == "In this deck"
    assert payloads["Padeem, Consul of Innovation"]["faces"][0]["type_line"].startswith("Legendary Creature")


def test_cards_list_greys_out_disposed_cards(client, db_path):
    conn = db.connect(db_path)
    lot = conn.execute("SELECT id FROM lots WHERE card_name = 'Counterspell'").fetchone()["id"]
    conn.execute(
        "INSERT INTO dispositions (lot_id, disposed_at, disposition_type, proceeds_cents)"
        " VALUES (?, '2026-09-28', 'sold', 150)",
        (lot,),
    )
    conn.commit()
    conn.close()

    html = client.get("/cards", params={"view": "list"}).text
    rows = re.findall(r'<li class="deck-row( away)?" data-card=\'([^\']*)\'', html)
    away = {json.loads(data)["name"]: json.loads(data)["where"] for flag, data in rows if flag}
    assert away == {"Counterspell": "Sold"}
    assert sum(json.loads(data)["qty"] for _, data in rows) == 200


def test_cards_page_collapses_copies_across_decks(client, db_path):
    html = client.get("/cards", params={"view": "list"}).text
    rows = [json.loads(raw) for raw in re.findall(r"data-card='([^']*)'", html)]
    by_name = {row["name"]: row for row in rows}
    assert len(by_name) == len(rows)  # one row per card name

    sol = by_name["Sol Ring"]
    assert sol["qty"] == 2
    assert sol["where"] == "Keen Engineering, Reign of Dragons"
    assert sol["printing"] is None  # neither copy pinned yet
    assert by_name["Island"]["qty"] == 34
    assert sum(row["qty"] for row in rows) == 200

    # A deck filter applies to copies before merging.
    conn = db.connect(db_path)
    keen = conn.execute("SELECT id FROM decks WHERE name = 'Keen Engineering'").fetchone()["id"]
    conn.close()
    html = client.get("/cards", params={"view": "list", "deck": str(keen)}).text
    rows = {json.loads(raw)["name"]: json.loads(raw) for raw in re.findall(r"data-card='([^']*)'", html)}
    assert rows["Sol Ring"]["qty"] == 1
    assert rows["Sol Ring"]["where"] == "Keen Engineering"
    assert "Showing 100 of 200 cards" in html


def test_collapsed_printing_summary_and_needs_printing_link(client, db_path, cache_dir):
    conn = db.connect(db_path)
    keen_sol = _unresolved_lot(db_path, "Sol Ring", "Keen Engineering")
    reign_sol = _unresolved_lot(db_path, "Sol Ring", "Reign of Dragons")
    printings.assign(conn, keen_sol, [(_scryfall_id("Sol Ring", "286"), "nonfoil", 1)])
    conn.close()

    html = client.get("/cards", params={"q": "sol ring", "view": "list"}).text
    [sol] = [json.loads(raw) for raw in re.findall(r"data-card='([^']*)'", html)]
    assert sol["printing"] == "FDC #286 + 1 not picked"
    # "needs printing" links to the copy that still needs it.
    assert f'href="/printings/{reign_sol}"' in html


def test_filters_and_sorting(client, db_path, cache_dir):
    path = cache_dir / "fdc.json"
    cards = json.loads(path.read_text())
    meta = {"Sol Ring": (1, [], "Artifact"), "Arcane Signet": (2, [], "Artifact"),
            "Counterspell": (2, ["U"], "Instant"), "Lathliss, Dragon Queen": (6, ["R"], "Legendary Creature")}
    for card in cards:
        if card["name"] in meta:
            cmc, colors, type_line = meta[card["name"]]
            card.update(cmc=cmc, colors=colors, type_line=type_line)
    path.write_text(json.dumps(cards))

    def names(**params):
        html = client.get("/cards", params={"view": "list", **params}).text
        return [json.loads(raw)["name"] for raw in re.findall(r"data-card='([^']*)'", html)]

    assert names(color="U") == ["Counterspell"]
    assert names(color="R") == ["Lathliss, Dragon Queen"]
    assert set(names(color="C", type="Artifacts")) == {"Sol Ring", "Arcane Signet"}
    assert names(type="Instants", color="U") == ["Counterspell"]

    ordered = names(type="Artifacts", color="C", sort="mv-asc")
    assert ordered == ["Sol Ring", "Arcane Signet"]
    assert names(type="Artifacts", color="C") == ["Arcane Signet", "Sol Ring"]  # default: high to low
    assert names(type="Artifacts", color="C", sort="name") == ["Arcane Signet", "Sol Ring"]

    # Unknown values fall back to defaults instead of erroring.
    assert client.get("/cards", params={"color": "purple", "sort": "x", "deck": "999"}).status_code == 200

    # Deck page: same filters, and links keep them.
    conn = db.connect(db_path)
    keen = conn.execute("SELECT id FROM decks WHERE name = 'Keen Engineering'").fetchone()["id"]
    conn.close()
    html = client.get(f"/decks/{keen}", params={"color": "U", "sort": "name"}).text
    tiles = re.findall(r'<figure class="[^"]*" data-card=\'([^\']*)\'', html)
    shown = [json.loads(raw)["name"] for raw in tiles]
    assert shown == ["Counterspell"]
    assert f'href="/decks/{keen}?sort=name&amp;color=U&amp;view=list"' in html
    assert "Showing 1 of 100 cards" in html
    assert "<h2>Breakdown</h2>" in html  # breakdown still covers the whole deck


def test_card_details_list_every_deck_with_counts(client, db_path):
    conn = db.connect(db_path)
    keen = conn.execute("SELECT id FROM decks WHERE name = 'Keen Engineering'").fetchone()["id"]
    reign = conn.execute("SELECT id FROM decks WHERE name = 'Reign of Dragons'").fetchone()["id"]
    lot = conn.execute(
        "SELECT lots.id FROM lots JOIN deck_cards ON deck_cards.lot_id = lots.id"
        " WHERE deck_cards.deck_id = ? AND lots.card_name = 'Counterspell'",
        (keen,),
    ).fetchone()["id"]
    pile = create_deck(conn, "Blue Pile", "2026-09-28")
    borrow(conn, lot, pile)
    conn.close()

    def payloads(url, **params):
        html = client.get(url, params=params).text
        return {json.loads(raw)["name"]: json.loads(raw) for raw in re.findall(r"data-card='([^']*)'", html)}

    # A lent card: on both lists, physically in Blue Pile.
    counterspell = payloads(f"/decks/{keen}")["Counterspell"]
    assert counterspell["decks"] == [
        {"id": pile, "name": "Blue Pile", "count": 1, "current": True},
        {"id": keen, "name": "Keen Engineering", "count": 1, "current": False},
    ]

    # Cards page: copies in two decks merge, each deck with its own count.
    cards = payloads("/cards", view="list")
    assert cards["Sol Ring"]["decks"] == [
        {"id": keen, "name": "Keen Engineering", "count": 1, "current": True},
        {"id": reign, "name": "Reign of Dragons", "count": 1, "current": True},
    ]
    assert [d["count"] for d in cards["Island"]["decks"]] == [34]
