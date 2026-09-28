import json

import pytest

from mtgtracker import db
from mtgtracker.commanders import CommanderError, eligible, set_commander
from mtgtracker.decks import create_deck

TYPE_LINES = {
    "Sai, Master Thopterist": "Legendary Creature — Human Artificer",
    "Padeem, Consul of Innovation": "Legendary Creature — Vedalken Artificer",
    "Graaz, Unstoppable Juggernaut": "Legendary Artifact Creature — Juggernaut",
    "Lathliss, Dragon Queen": "Legendary Creature — Dragon",
    "Skysovereign, Consul Flagship": "Legendary Artifact — Vehicle",
    "Counterspell": "Instant",
}


@pytest.fixture
def typed_cache(cache_dir):
    """The fdc fixture has no type lines; add a few real ones."""
    path = cache_dir / "fdc.json"
    cards = json.loads(path.read_text())
    for card in cards:
        if card["name"] in TYPE_LINES:
            card["type_line"] = TYPE_LINES[card["name"]]
    path.write_text(json.dumps(cards))
    return cache_dir


def _deck_id(conn, name):
    return conn.execute("SELECT id FROM decks WHERE name = ?", (name,)).fetchone()[0]


def _commander(conn, deck_id):
    return conn.execute("SELECT commander FROM decks WHERE id = ?", (deck_id,)).fetchone()[0]


def test_eligible_lists_only_legendary_creatures_on_the_list(db_path, typed_cache):
    conn = db.connect(db_path)
    names = {c["card_name"] for c in eligible(conn, _deck_id(conn, "Keen Engineering"))}
    # Legendary artifact (vehicle) and instants aren't creatures; Lathliss
    # isn't on this deck's list.
    assert names == {"Sai, Master Thopterist", "Padeem, Consul of Innovation",
                     "Graaz, Unstoppable Juggernaut"}


def test_set_and_clear_commander(db_path, typed_cache):
    conn = db.connect(db_path)
    keen = _deck_id(conn, "Keen Engineering")

    set_commander(conn, keen, "Padeem, Consul of Innovation")
    assert _commander(conn, keen) == "Padeem, Consul of Innovation"

    set_commander(conn, keen, None)
    assert _commander(conn, keen) is None


@pytest.mark.parametrize(
    ("card_name", "message"),
    [
        ("Counterspell", "isn't a legendary creature"),
        ("Skysovereign, Consul Flagship", "isn't a legendary creature"),
        ("Lathliss, Dragon Queen", "isn't a legendary creature on this deck's list"),
    ],
)
def test_rejects_ineligible_commanders(db_path, typed_cache, card_name, message):
    conn = db.connect(db_path)
    keen = _deck_id(conn, "Keen Engineering")
    with pytest.raises(CommanderError, match=message):
        set_commander(conn, keen, card_name)
    assert _commander(conn, keen) == "Sai, Master Thopterist"


def test_uncached_type_line_is_not_eligible(db_path, cache_dir):
    conn = db.connect(db_path)  # plain fixture: no type lines at all
    assert eligible(conn, _deck_id(conn, "Keen Engineering")) == []


def test_non_commander_format_has_no_commander(db_path, typed_cache):
    conn = db.connect(db_path)
    modern = create_deck(conn, "Brew", "2026-09-28", format="Modern")
    with pytest.raises(CommanderError, match="Modern decks don't have a commander"):
        set_commander(conn, modern, "Sai, Master Thopterist")
    set_commander(conn, modern, None)  # clearing is always fine
