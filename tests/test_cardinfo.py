import pytest

from mtgtracker.cardinfo import CardInfo, color_weight, mana_value_order


@pytest.mark.parametrize(
    ("cost", "expected"),
    [
        ("{1}{U}{R}", (2, 2)),
        ("{1}{R}{R}", (1, 2)),
        ("{3}", (0, 0)),
        ("{X}{C}{C}", (0, 0)),
        ("{W/U}{W/U}", (2, 2)),
        ("{2/R}{G/P}", (2, 2)),
        ("", (0, 0)),
    ],
)
def test_color_weight(cost, expected):
    assert color_weight(cost) == expected


def _card(cmc, cost):
    return CardInfo(image=None, type_line=None, exact=True, cmc=cmc,
                    faces=({"mana_cost": cost},))


def test_colored_mana_outranks_colorless_at_equal_mana_value():
    cards = {
        "Colorless Three": _card(3, "{3}"),
        "Mono Red": _card(3, "{1}{R}{R}"),
        "One Pip": _card(3, "{2}{R}"),
        "Two Colors": _card(3, "{1}{U}{R}"),
        "Big Colorless": _card(4, "{4}"),
        "Uncached": CardInfo(image=None, type_line=None, exact=False),
    }
    order = sorted(cards, key=lambda name: mana_value_order(name, cards[name]))
    assert order == ["Big Colorless", "Two Colors", "Mono Red", "One Pip", "Colorless Three", "Uncached"]


def test_digital_only_printing_is_a_name_fallback(cache_dir):
    """A card whose only cached printing is MTGO-only (the collection
    endpoint's default for e.g. Spawning Pit) still gets its image and type,
    but a paper printing wins whenever one is cached."""
    import json

    from mtgtracker import cardinfo, scryfall

    def card(id_, name, set_, digital):
        return {"id": id_, "name": name, "set": set_, "collector_number": "1",
                "finishes": ["nonfoil"], "digital": digital, "type_line": "Artifact",
                "cmc": 2.0, "image_uris": {"normal": f"https://img/{id_}"}}

    (cache_dir / "cards").mkdir(exist_ok=True)
    for c in (card("d1", "Spawning Pit", "td2", True), card("d2", "Sol Ring", "zzz", True)):
        (cache_dir / "cards" / f"{c['id']}.json").write_text(json.dumps(c))
    scryfall.invalidate_cache()

    pit = cardinfo.lookup(None, "Spawning Pit")
    assert (pit.image, pit.type_line, pit.exact) == ("https://img/d1", "Artifact", False)
    assert cardinfo.lookup("d1", "Spawning Pit").exact is False  # never pinned to MTGO
    assert cardinfo.lookup(None, "Sol Ring").image != "https://img/d2"  # fdc paper printing
    assert cardinfo.printings_named("Spawning Pit") == []
