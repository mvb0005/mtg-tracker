import json
import re

from mtgtracker import db
from mtgtracker.cardinfo import CardInfo
from mtgtracker.web.filters import CardFilters


def _row(name, qty, colors, type_line, cmc, category, deck=None):
    art = CardInfo(image=None, type_line=type_line, exact=True, cmc=cmc, colors=colors)
    return {"card_name": name, "qty": qty, "art": art, "category": category,
            "current_deck_id": deck, "cost_cents": 0}


ROWS = [
    _row("Counterspell", 1, ("U",), "Instant", 2, "Instants", deck=1),
    _row("Lightning Bolt", 1, ("R",), "Instant", 1, "Instants", deck=2),
    _row("Izzet Charm", 1, ("U", "R"), "Instant", 2, "Instants", deck=1),
    _row("Myr Battlesphere", 1, (), "Artifact Creature — Myr Construct", 7, "Creatures"),
    _row("Sol Ring", 2, (), "Artifact", 1, "Artifacts", deck=1),
    _row("Island", 34, (), "Basic Land — Island", 0, "Lands", deck=1),
]


def _names(f):
    return sorted(r["card_name"] for r in f.apply(ROWS))


def test_multiple_colors_match_any_by_default():
    f = CardFilters.parse(color=["U", "R"])
    assert _names(f) == ["Counterspell", "Izzet Charm", "Lightning Bolt"]


def test_color_mode_all_requires_every_ticked_color():
    f = CardFilters.parse(color=["U", "R"], color_mode="all")
    assert _names(f) == ["Izzet Charm"]
    assert CardFilters.parse(color=["U", "C"], color_mode="all").apply(ROWS) == []
    assert CardFilters.parse(color_mode="bogus").color_mode == "any"


def test_multi_type_cards_match_every_type_they_have():
    assert "Myr Battlesphere" in _names(CardFilters.parse(type=["Artifacts"]))
    assert "Myr Battlesphere" in _names(CardFilters.parse(type=["Creatures"]))
    counts = CardFilters().facet_counts(ROWS)["type"]
    # Battlesphere counts under both; Sol Ring's 2 copies under Artifacts.
    assert counts["Artifacts"] == 3
    assert counts["Creatures"] == 1
    assert counts["Lands"] == 34


def test_mana_value_and_deck_facets():
    assert _names(CardFilters.parse(mv=["7+"])) == ["Myr Battlesphere"]
    assert _names(CardFilters.parse(mv=["1", "2"], color=["U"])) == ["Counterspell", "Izzet Charm"]
    f = CardFilters.parse(deck=["none"], deck_ids={1, 2})
    assert _names(f) == ["Myr Battlesphere"]
    assert CardFilters.parse(deck=["99"], deck_ids={1, 2}).deck == ()


def test_facet_counts_ignore_their_own_facet():
    f = CardFilters.parse(color=["U"])
    counts = f.facet_counts(ROWS)
    # Color counts aren't narrowed by the color selection (so you can add Red)...
    assert counts["color"]["R"] == 2
    # ...but other facets are: only blue cards count toward types.
    assert counts["type"] == {"Instants": 2}

    all_mode = CardFilters.parse(color=["U"], color_mode="all").facet_counts(ROWS)["color"]
    assert all_mode["R"] == 1  # only Izzet Charm is both blue and red
    assert "C" not in all_mode


def test_params_and_href_round_trip():
    f = CardFilters.parse(sort="name", color=["U", "R"], color_mode="all", mv=["7+"])
    assert f.href("/cards", view="list") == (
        "/cards?sort=name&color_mode=all&color=U&color=R&mv=7%2B&view=list"
    )
    assert f.active_count == 3
    assert f.cleared().href("/cards") == "/cards?sort=name&color_mode=all"


def test_sidebar_renders_with_counts(client, db_path, cache_dir):
    path = cache_dir / "fdc.json"
    cards = json.loads(path.read_text())
    for card in cards:
        if card["name"] == "Myr Battlesphere":
            card.update(cmc=7, colors=[], type_line="Artifact Creature — Myr Construct")
    path.write_text(json.dumps(cards))

    html = client.get("/cards", params=[("type", "Artifacts"), ("type", "Creatures")]).text
    assert 'id="facet-form"' in html
    assert '<select name="sort" form="facet-form">' in html
    checked = r'name="type" value="Artifacts" checked>\s*<span class="facet-label">Artifacts</span>'
    assert re.search(checked, html)
    assert 'name="color_mode" value="any" checked' in html
    # Phones fold the sidebar before first paint (inline, right after it).
    assert re.search(r"</details>\s*<script>if \(matchMedia\(\"\(max-width: 899px\)\"\)", html)
    assert "Myr Battlesphere" in html

    conn = db.connect(db_path)
    keen = conn.execute("SELECT id FROM decks WHERE name = 'Keen Engineering'").fetchone()["id"]
    conn.close()
    deck_html = client.get(f"/decks/{keen}", params={"mv": "7+"}).text
    shown = re.findall(r'<figure class="[^"]*" data-card=\'([^\']*)\'', deck_html)
    assert [json.loads(raw)["name"] for raw in shown] == ["Myr Battlesphere"]
    assert 'name="deck"' not in deck_html  # deck facet is cards-page only
