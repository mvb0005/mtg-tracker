import json
import re

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

    # Each non-land card counted once: a color combination gets its own
    # bucket (WUBRG-ordered key), colorless its own.
    assert [(s["key"], s["label"], s["count"]) for s in b["colors"]] == [
        ("R", "Red", 3), ("WR", "White-Red", 1), ("C", "Colorless", 1),
    ]
    assert b["colors"][0]["fill"] == "var(--mana-R)"
    assert b["colors"][1]["fill"] == (
        "repeating-linear-gradient(135deg, var(--mana-W) 0px 5px, var(--mana-R) 5px 10px)"
    )
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
    assert "<h2>Breakdown</h2>" in html
    assert 'title="1 card at mana value 1"' in html
    assert 'title="Blue: 1 (50%)"' in html
    assert 'title="Colorless: 1 (50%)"' in html
    assert "Lands" in html.split('id="bd-types"', 1)[1]

    listing = client.get(f"/decks/{keen}", params={"view": "list"}).text
    assert 'src="https://svgs.scryfall.io/card-symbols/U.svg"' in listing


def test_curve_stacks_colors_for_multicolor_decks():
    groups = [
        _group("Counterspell", 2, "Instants", 2, ("U",)),
        _group("Lightning Bolt", 1, "Instants", 1, ("R",)),
        _group("Izzet Charm", 1, "Instants", 2, ("U", "R")),
        _group("Sol Ring", 1, "Artifacts", 1, ()),
        _group("Mystery", 1, "Other", 2, None),
    ]
    b = deck_breakdown(groups, ("U", "R"))
    assert b["stacked"]
    by_value = {bar["value"]: [(s["key"], s["count"]) for s in bar["segments"]] for bar in b["curve"]}
    assert by_value[1] == [("R", 1), ("C", 1)]
    assert by_value[2] == [("U", 2), ("UR", 1), ("X", 1)]  # singles, combinations, colorless, unknown
    legend = [(s["key"], s["count"]) for s in b["curve_legend"]]
    assert legend == [("U", 2), ("R", 1), ("UR", 1), ("C", 1), ("X", 1)]
    # Card types stack the same way.
    instants = next(t for t in b["types"] if t["label"] == "Instants")
    assert [(s["key"], s["count"]) for s in instants["segments"]] == [("U", 2), ("R", 1), ("UR", 1)]
    assert instants["bar_pct"] == 100
    # Segment totals always add up to the bar.
    assert all(sum(s["count"] for s in bar["segments"]) == bar["count"] for bar in b["curve"])


def test_one_color_decks_keep_a_plain_curve():
    groups = [_group("Shock", 2, "Instants", 1, ("R",)), _group("Sol Ring", 1, "Artifacts", 1, ())]
    assert not deck_breakdown(groups, ("R",))["stacked"]
    assert not deck_breakdown(groups)["stacked"]


def test_stacked_curve_renders(client, db_path, cache_dir):
    path = cache_dir / "fdc.json"
    cards = json.loads(path.read_text())
    for card in cards:
        if card["name"] == "Counterspell":
            card.update(cmc=2, colors=["U"], color_identity=["U"], type_line="Instant")
        if card["name"] == "Blasphemous Act":
            card.update(cmc=9, colors=["R"], color_identity=["R"], type_line="Sorcery")
    path.write_text(json.dumps(cards))
    conn = db.connect(db_path)
    keen = conn.execute("SELECT id FROM decks WHERE name = 'Keen Engineering'").fetchone()[0]
    act = conn.execute("SELECT id FROM lots WHERE card_name = 'Blasphemous Act'").fetchone()[0]
    conn.execute("INSERT INTO deck_cards (deck_id, lot_id, equipped) VALUES (?, ?, 0)", (keen, act))
    conn.execute("UPDATE decks SET commander = NULL WHERE id = ?", (keen,))
    conn.commit()
    conn.close()

    html = client.get(f"/decks/{keen}").text
    assert 'data-identity="UR"' in html
    assert 'class="curve-seg" data-colors="U" style="flex-grow: 1; background: var(--mana-U)"' in html
    assert 'class="type-bar type-stack"' in html
    assert re.search(r'class="type-seg" data-colors="R"[^>]*title="Sorceries · Red: 1"', html)
    assert 'title="Mana value 9 · Red: 1"' in html
    assert html.count('<div class="curve-bar curve-stack"') >= 2
