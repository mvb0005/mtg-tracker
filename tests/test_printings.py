import json

import pytest

from mtgtracker import db, printings
from mtgtracker.decks import borrow, create_deck


def _conn(db_path):
    return db.connect(db_path)


def _printing(name: str, number: str) -> dict:
    return next(
        c for c in printings.find_printings(name) if c["collector_number"] == number
    )


def _group_lot(conn, card_name: str, deck_name: str) -> int:
    return conn.execute(
        """
        SELECT MIN(lots.id) FROM lots
        JOIN deck_cards ON deck_cards.lot_id = lots.id AND deck_cards.equipped = 1
        JOIN decks ON decks.id = deck_cards.deck_id
        WHERE lots.card_name = ? AND decks.name = ? AND lots.scryfall_id IS NULL
        """,
        (card_name, deck_name),
    ).fetchone()[0]


def _write_fake_set(cache_dir, set_code: str, cards: list[dict]) -> None:
    (cache_dir / f"{set_code}.json").write_text(json.dumps(cards))


def _island(number: str, **extra) -> dict:
    return {
        "id": f"fdn-island-{number}",
        "name": "Island",
        "set": "fdn",
        "collector_number": number,
        "finishes": ["nonfoil", "foil"],
        **extra,
    }


def test_find_printings_reads_cached_sets_only(cache_dir):
    found = printings.find_printings("sol ring")  # normalized match
    assert [(c["set"], c["collector_number"]) for c in found] == [("fdc", "2"), ("fdc", "286")]
    # The trimmed fixture lacks most Scryfall fields; they come back empty.
    assert found[0]["image"] is None
    assert found[0]["finishes"] == ["foil"]
    assert printings.find_printings("Island") == []


def test_find_printings_skips_digital_and_matches_card_faces(cache_dir):
    _write_fake_set(cache_dir, "zzz", [
        {"id": "a", "name": "Island", "set": "zzz", "collector_number": "10",
         "finishes": ["nonfoil"], "digital": True},
        {"id": "b", "name": "Front // Back", "set": "zzz", "collector_number": "11",
         "finishes": ["nonfoil"],
         "card_faces": [{"name": "Front", "image_uris": {"normal": "https://img/front"}},
                        {"name": "Back"}]},
    ])
    assert printings.find_printings("Island") == []
    [dfc] = printings.find_printings("Front")
    assert dfc["image"] == "https://img/front"


def test_unresolved_groups_list_nonbasics_first(db_path, cache_dir):
    conn = _conn(db_path)
    groups = printings.unresolved_groups(conn)
    names = [(g["card_name"], g["qty"], g["is_basic"]) for g in groups]
    assert names[:4] == [
        ("Arcane Signet", 1, 0), ("Arcane Signet", 1, 0), ("Sol Ring", 1, 0), ("Sol Ring", 1, 0),
    ]
    assert sorted(names[4:]) == [("Island", 34, 1), ("Mountain", 32, 1)]


def test_assign_single_pins_lot_and_records_scryfall_card(db_path, cache_dir):
    conn = _conn(db_path)
    lot = _group_lot(conn, "Sol Ring", "Keen Engineering")
    card = _printing("Sol Ring", "286")

    assert printings.assign(conn, lot, [(card["id"], "nonfoil", 1)]) == 1

    row = conn.execute("SELECT * FROM lots WHERE id = ?", (lot,)).fetchone()
    assert (row["scryfall_id"], row["set_code"], row["collector_number"], row["finish"]) == (
        card["id"], "fdc", "286", "nonfoil",
    )
    cached = conn.execute("SELECT * FROM scryfall_cards WHERE scryfall_id = ?", (card["id"],)).fetchone()
    assert json.loads(cached["finishes"]) == ["nonfoil", "foil"]
    # The other deck's Sol Ring is untouched.
    assert _group_lot(conn, "Sol Ring", "Reign of Dragons") is not None
    assert printings.group_for_lot(conn, lot) is None


def test_assign_foil_only_printing_changes_finish(db_path, cache_dir):
    conn = _conn(db_path)
    lot = _group_lot(conn, "Arcane Signet", "Reign of Dragons")
    promo = _printing("Arcane Signet", "1")  # foil-only starter collection promo

    printings.assign(conn, lot, [(promo["id"], "foil", 1)])
    assert conn.execute("SELECT finish FROM lots WHERE id = ?", (lot,)).fetchone()[0] == "foil"


@pytest.mark.parametrize(
    ("card_number", "finish", "qty", "message"),
    [
        ("2", "nonfoil", 1, "doesn't come in nonfoil"),
        ("286", "etched", 1, "doesn't come in etched"),
        ("286", "nonfoil", 2, "only 1 are unresolved"),
        ("286", "nonfoil", -1, "negative"),
        ("286", "nonfoil", 0, "nothing selected"),
    ],
)
def test_assign_rejects_invalid_and_writes_nothing(db_path, cache_dir, card_number, finish, qty, message):
    conn = _conn(db_path)
    lot = _group_lot(conn, "Sol Ring", "Keen Engineering")
    card = _printing("Sol Ring", card_number)

    with pytest.raises(printings.PrintingError, match=message):
        printings.assign(conn, lot, [(card["id"], finish, qty)])
    assert conn.execute("SELECT scryfall_id FROM lots WHERE id = ?", (lot,)).fetchone()[0] is None


def test_assign_rejects_printing_of_another_card(db_path, cache_dir):
    conn = _conn(db_path)
    lot = _group_lot(conn, "Sol Ring", "Keen Engineering")
    signet = _printing("Arcane Signet", "245")
    with pytest.raises(printings.PrintingError, match="isn't a known printing of Sol Ring"):
        printings.assign(conn, lot, [(signet["id"], "nonfoil", 1)])


def test_assign_rejects_resolved_lot(db_path, cache_dir):
    conn = _conn(db_path)
    resolved = conn.execute("SELECT id FROM lots WHERE scryfall_id IS NOT NULL").fetchone()[0]
    with pytest.raises(printings.PrintingError, match="already resolved"):
        printings.assign(conn, resolved, [("x", "nonfoil", 1)])


def test_basics_split_across_arts_in_lot_order(db_path, cache_dir):
    _write_fake_set(cache_dir, "fdn", [_island("272"), _island("273")])
    conn = _conn(db_path)
    lot = _group_lot(conn, "Island", "Keen Engineering")
    group = printings.group_for_lot(conn, lot)
    assert group.qty == 34

    assigned = printings.assign(conn, lot, [("fdn-island-272", "nonfoil", 20),
                                            ("fdn-island-273", "nonfoil", 10)])
    assert assigned == 30

    rows = conn.execute(
        f"SELECT id, collector_number FROM lots WHERE id IN ({','.join('?' * len(group.lot_ids))})"
        " ORDER BY id",
        group.lot_ids,
    ).fetchall()
    assert [r["collector_number"] for r in rows] == ["272"] * 20 + ["273"] * 10 + [None] * 4
    remaining = printings.group_for_lot(conn, group.lot_ids[30])
    assert remaining.lot_ids == group.lot_ids[30:]


def test_borrowed_copy_is_its_own_group(db_path, cache_dir):
    conn = _conn(db_path)
    lot = _group_lot(conn, "Island", "Keen Engineering")
    elsewhere = create_deck(conn, "Blue Pile", "2026-09-28")
    borrow(conn, lot, elsewhere)

    borrowed = printings.group_for_lot(conn, lot)
    assert (borrowed.qty, borrowed.deck_name) == (1, "Blue Pile")
    rest = printings.group_for_lot(conn, _group_lot(conn, "Island", "Keen Engineering"))
    assert (rest.qty, rest.deck_name) == (33, "Keen Engineering")
