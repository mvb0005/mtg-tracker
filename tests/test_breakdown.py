import json

from mtgtracker import db
from mtgtracker.cardinfo import CardInfo
from mtgtracker.web.app import mana_symbols
from mtgtracker.web.breakdown import deck_breakdown


def _group(name, qty, category, cmc=None, colors=None):
    return {"card_name": name, "qty": qty, "category": category,
            "art": CardInfo(image=None, type_line=None, exact=True, cmc=cmc, colors=colors)}


def test_breakdown_counts_curve_colors_and_types():
    groups = [
        _group("Lathliss", 1, "Creatures", 6, ("R",)),
        _group("Shock", 2, "Instants", 1, ("R",)),
        _group("Boros Charm", 1, "Instants", 2, ("R", "W")),
        _group("Sol Ring", 1, "Artifacts", 1, ()),
        _group("Mystery", 1, "Other", None, None),  # not cached
        _group("Mountain", 30, "Lands", 0, ()),
    ]
    b = deck_breakdown(groups)

    # Lands are left out of the curve; the gap at 3-5 shows as zeros.
    assert [(bar["value"], bar["count"]) for bar in b["curve"]] == [
        (1, 3), (2, 1), (3, 0), (4, 0), (5, 0), (6, 1),
    ]
    assert b["curve"][0]["pct"] == 100
    assert b["curve_total"] == 5
    assert b["uncounted"] == 1

    # Each non-land card counted once: multicolor and colorless get their own buckets.
    assert [(s["label"], s["count"]) for s in b["colors"]] == [
        ("Red", 3), ("Multicolor", 1), ("Colorless", 1),
    ]
    assert round(sum(s["pct"] for s in b["colors"])) == 100

    # Types in fixed slot order, Other last with no slot.
    assert [(s["label"], s["count"], s["slot"]) for s in b["types"]] == [
        ("Creatures", 1, 1), ("Instants", 3, 2), ("Artifacts", 1, 4), ("Lands", 30, 6), ("Other", 1, None),
    ]
    assert b["type_total"] == 36


def test_breakdown_includes_zero_cost_spells():
    b = deck_breakdown([
        _group("Ornithopter", 1, "Creatures", 0, ()),
        _group("Bolt", 1, "Instants", 1, ("R",)),
    ])
    assert [bar["value"] for bar in b["curve"]] == [0, 1]


def test_empty_breakdown():
    b = deck_breakdown([])
    assert b["curve"] == []
    assert b["colors"] == []
    assert b["types"] == []


def test_mana_symbols_filter_escapes_and_renders():
    html = str(mana_symbols("{2}{W/U}"))
    assert html.count('<img class="mana"') == 2
    assert "card-symbols/WU.svg" in html
    assert 'alt="{W/U}"' in html
    assert str(mana_symbols("<b>{R}")).startswith("&lt;b&gt;")
    assert str(mana_symbols(None)) == ""


def test_deck_page_renders_breakdown(client, db_path, cache_dir):
    path = cache_dir / "fdc.json"
    cards = json.loads(path.read_text())
    for card in cards:
        if card["name"] == "Sol Ring":
            card.update(cmc=1, colors=[], type_line="Artifact", mana_cost="{1}")
        if card["name"] == "Counterspell":
            card.update(cmc=2, colors=["U"], type_line="Instant", mana_cost="{U}{U}")
    path.write_text(json.dumps(cards))
    conn = db.connect(db_path)
    keen = conn.execute("SELECT id FROM decks WHERE name = 'Keen Engineering'").fetchone()["id"]
    conn.close()

    html = client.get(f"/decks/{keen}").text
    assert "Deck breakdown" in html
    assert 'title="1 card at mana value 1"' in html
    assert 'title="Blue: 1 (50%)"' in html
    assert 'title="Colorless: 1 (50%)"' in html
    assert "Lands" in html.split('id="bd-types"', 1)[1]

    listing = client.get(f"/decks/{keen}", params={"view": "list"}).text
    assert 'src="https://svgs.scryfall.io/card-symbols/U.svg"' in listing
