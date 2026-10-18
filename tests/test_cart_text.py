"""importers/cart_text: the three cart formats, hints in names, and the errors."""

from pathlib import Path

import pytest

from mtgtracker.importers import cart_text
from mtgtracker.importers.cart_text import CartParseError, parse

CARTS = Path(__file__).parent / "fixtures" / "carts"
CHECKOUT_HEAD = "Description\tCond\tQty\tStock\tPrice\tTotal\n"


def fixture(name):
    return parse((CARTS / name).read_text())


def by_name(cart, name):
    return next(line for line in cart.lines if line.name == name)


def test_store_csv():
    cart = fixture("store.csv")
    assert (cart.format, len(cart.lines), cart.copies, cart.subtotal_cents) == ("store CSV", 12, 18, 102521)
    hearse = by_name(cart, "Unlicensed Hearse")
    assert (hearse.collector_number, hearse.treatments, hearse.unit_cents) == ("0064", ("Showcase",), 59)
    assert hearse.set_name == "Outlaws of Thunder Junction Breaking News"
    tower = by_name(cart, "Command Tower")  # "(Foil)" is a finish, not part of the name
    assert (tower.finish, tower.qty, tower.total_cents) == ("foil", 2, 250)
    assert by_name(cart, "Arcane Signet").finish == "foil"  # " - Foil"
    assert by_name(cart, "Rhystic Study").finish == "nonfoil"  # "(Non-foil)" is not foil
    assert by_name(cart, "Giada, Font of Hope").unit_cents == 250  # a quoted comma
    assert by_name(cart, "Swords to Plowshares").unit_cents == 100000  # "$1,000.00"
    assert by_name(cart, "Circle of Protection: Red").condition == "HP"


def test_condition_goes_to_notes_and_nm_does_not():
    cart = fixture("store.csv")
    assert by_name(cart, "Truce").notes == "Condition: EX"
    assert by_name(cart, "Adarkar Wastes").notes is None
    assert by_name(cart, "Unlicensed Hearse").notes == "Showcase"


def test_checkout_text():
    cart = fixture("checkout.txt")
    assert (cart.format, len(cart.lines), cart.copies, cart.subtotal_cents) == ("checkout", 12, 19, 2010)
    wastes = cart.lines[0]
    assert (wastes.name, wastes.set_name, wastes.qty, wastes.unit_cents) == (
        "Adarkar Wastes", "Modern Horizons 3 Commander Decks", 1, 59)
    assert by_name(cart, "An Offer You Can't Refuse").unit_cents == 599
    assert by_name(cart, "Truce").condition == "EX"
    assert cart.lines[10].finish == "etched"  # " - Foil Etched", before foil is looked for
    assert cart.lines[5].finish == "foil"
    assert cart.lines[5].name == "Command Tower"


def test_checkout_colons_in_set_names_and_card_names():
    cart = fixture("checkout.txt")
    muse = by_name(cart, "Windborn Muse")  # the set has a colon: split at the last one
    assert muse.set_name == "Innistrad: Crimson Vow Commander"
    assert muse.set_alternatives == (("Innistrad", "Crimson Vow Commander: Windborn Muse"),)
    circle = next(line for line in cart.lines if line.set_name == "Mirage: Circle of Protection")
    # the card has a colon: the other cut is kept for the resolver to try
    assert circle.name == "Red"
    assert circle.set_alternatives == (("Mirage", "Circle of Protection: Red"),)


def test_plain_list():
    cart = fixture("list.txt")
    assert (cart.format, len(cart.lines), cart.copies, cart.subtotal_cents) == ("list", 12, 19, 2685)
    muse = by_name(cart, "Windborn Muse")
    assert (muse.set_name, muse.condition) == ("Innistrad: Crimson Vow Commander", None)
    assert by_name(cart, "Sol Ring").condition == "LP"
    # a comma in the name: the price is anchored at the end of the line
    assert by_name(cart, "Giada, Font of Hope").unit_cents == 250
    assert by_name(cart, "Arcane Signet").finish == "foil"  # *F*
    circle = by_name(cart, "Circle of Protection: Red")
    assert (circle.qty, circle.unit_cents, circle.total_cents) == (2, 35, 70)  # "$0.70 total"
    assert by_name(cart, "Island").unit_cents == 10  # "$0.10 each"
    hearse = by_name(cart, "Unlicensed Hearse")
    assert (hearse.collector_number, hearse.set_name) == ("0064", None)


@pytest.mark.parametrize(("text", "finish"), [
    ("1 Sol Ring (Foil), $1.00", "foil"),
    ("1 Sol Ring (Foil Etched), $1.00", "etched"),
    ("1 Sol Ring (Etched), $1.00", "etched"),
    ("1 Sol Ring - Foil, $1.00", "foil"),
    ("1 Sol Ring *E*, $1.00", "etched"),
    ("1 Sol Ring (Non-foil), $1.00", "nonfoil"),
    ("1 Sol Ring (Nonfoil), $1.00", "nonfoil"),
    ("1 Sol Ring, $1.00", "nonfoil"),
])
def test_finish_detection(text, finish):
    [line] = parse(text).lines
    assert (line.name, line.finish) == ("Sol Ring", finish)


def test_a_set_name_with_a_comma_is_kept_whole():
    [line] = parse("1 Sol Ring (Warhammer 40,000 Commander, LP), $1.00").lines
    assert (line.set_name, line.condition) == ("Warhammer 40,000 Commander", "LP")


def test_a_name_ending_in_parentheses_that_are_not_hints_is_left_alone():
    [line] = parse("Card Name,Quantity,Price\nB.F.M. (Big Furry Monster),1,$0.50").lines
    assert line.name == "B.F.M. (Big Furry Monster)"


def test_blank_lines_are_fine():
    cart = parse("\n\n1 Sol Ring, $1.00\n\n   \n2 Island, $0.10\n\n")
    assert cart.copies == 3


@pytest.mark.parametrize(("text", "message"), [
    ("", "empty"),
    ("   \n\n", "empty"),
    ("1 Sol Ring, $1.00\nthis is not a card line", "line 2"),
    ("1 Sol Ring, $1.00\nSol Ring $1.00", "line 2"),
    ("1 Sol Ring, $1.00\n1 Island, $abc", "line 2"),
    ("1 Sol Ring, $1.00\n1 Island, $0.001", "line 2"),
    ("0 Sol Ring, $1.00", "quantity"),
    ("1 Sol Ring, $1.00\nTOTAL: 2 items, $2.00 subtotal", "line 2: TOTAL says 2 items but the list has 1"),
    ("1 Sol Ring, $1.00\nTOTAL: 1 items, $1.01 subtotal", "line 2: TOTAL says 101 cents"),
    ("1 Sol Ring, $1.00\nTOTAL: lots", "line 2"),
    ("Description\tCond\tQty\tStock\tPrice\tTotal\nFixture: Sol Ring\tNM\t2\t1 left\t1.00\t$3.00", "line 2"),
    ("Description\tCond\tQty\tStock\tPrice\tTotal\nFixture: Sol Ring\tNM\t2", "line 2: expected"),
    (CHECKOUT_HEAD + "Fixture: Sol Ring\tXX\t1\t1 left\t1.00\t$1.00", "condition"),
    ("Card Name,Quantity,Price\nSol Ring,1,$1.00\n,1,$1.00", "line 3: no card name"),
    ("Card Name,Quantity,Price\nSol Ring,1,free", "line 2"),
    ("Card Name,Condition\nSol Ring,NM", "needs Card Name, Quantity and Price"),
])
def test_unreadable_carts_raise_naming_the_line(text, message):
    with pytest.raises(CartParseError, match=message):
        parse(text)


def test_total_line_may_come_anywhere_and_matches():
    text = "TOTAL: 3 items, $1.20 subtotal (before tax)\n2 Island, $0.10\n1 Sol Ring, $1.00"
    assert parse(text).copies == 3


def test_lines_know_their_numbers():
    cart = parse("\n1 Sol Ring, $1.00\n\n1 Island, $0.10")
    assert [line.line for line in cart.lines] == [2, 4]
    assert cart_text.MAX_QTY >= 1
