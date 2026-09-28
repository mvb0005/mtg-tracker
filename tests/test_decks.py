import sqlite3

from mtgtracker import db
from mtgtracker.decks import add_to_deck, borrow, create_deck, cut, reassemble


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    return conn


def _make_lots(conn, count: int, prefix: str) -> list[int]:
    conn.execute(
        "INSERT INTO transactions (vendor, created_at, updated_at, subtotal_cents,"
        " tax_cents, total_cents, source) VALUES ('Test', 't', 't', 0, 0, 0, 'manual')"
    )
    txn_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    lot_ids = []
    for i in range(count):
        conn.execute(
            "INSERT INTO lots (transaction_id, card_name, acquired_date)"
            " VALUES (?, ?, '2026-01-01')",
            (txn_id, f"{prefix} {i}"),
        )
        lot_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    return lot_ids


def test_borrow_cut_reassemble_worked_example():
    conn = _make_conn()

    # 1. Two precons come in fully equipped in their own decks.
    reign = create_deck(conn, "Reign of Dragons", "2026-09-27")
    keen = create_deck(conn, "Keen Engineering", "2026-09-27")

    reign_lots = _make_lots(conn, 100, "Reign card")
    keen_lots = _make_lots(conn, 100, "Keen card")

    for lot_id in reign_lots:
        add_to_deck(conn, reign, lot_id, equipped=True)
    for lot_id in keen_lots:
        add_to_deck(conn, keen, lot_id, equipped=True)

    # 2. Build deck 3 from 50 cards borrowed out of each precon.
    frankendeck = create_deck(conn, "Frankendeck", "2026-10-01")
    borrowed_from_reign = reign_lots[:50]
    borrowed_from_keen = keen_lots[:50]

    for lot_id in borrowed_from_reign + borrowed_from_keen:
        borrow(conn, lot_id, frankendeck)

    # 3. Cut one weak card from deck 1 that was never borrowed.
    never_borrowed = reign_lots[50]
    cut(conn, reign, never_borrowed)

    # 4. Reassemble deck 1: should list exactly the 50 borrowed cards,
    #    each pointing at Frankendeck, and NOT the cut card.
    result = reassemble(conn, reign)

    assert len(result) == 50
    result_lot_ids = {row["lot_id"] for row in result}
    assert result_lot_ids == set(borrowed_from_reign)
    assert all(row["current_deck_id"] == frankendeck for row in result)
    assert never_borrowed not in result_lot_ids

    # The unaffected 49 cards in deck 1 are still equipped there.
    still_equipped = conn.execute(
        "SELECT COUNT(*) FROM deck_cards WHERE deck_id = ? AND equipped = 1", (reign,)
    ).fetchone()[0]
    assert still_equipped == 49  # 100 - 50 borrowed - 1 cut

    # Deck 1's card list no longer references the cut card at all.
    still_listed = conn.execute(
        "SELECT COUNT(*) FROM deck_cards WHERE deck_id = ? AND lot_id = ?",
        (reign, never_borrowed),
    ).fetchone()[0]
    assert still_listed == 0

    # Frankendeck has all 100 borrowed cards equipped.
    frankendeck_equipped = conn.execute(
        "SELECT COUNT(*) FROM deck_cards WHERE deck_id = ? AND equipped = 1", (frankendeck,)
    ).fetchone()[0]
    assert frankendeck_equipped == 100

    # Keen Engineering's own list is untouched except its 50 borrowed cards
    # are now unequipped there.
    keen_reassemble = reassemble(conn, keen)
    assert {row["lot_id"] for row in keen_reassemble} == set(borrowed_from_keen)
