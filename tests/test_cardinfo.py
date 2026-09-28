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
