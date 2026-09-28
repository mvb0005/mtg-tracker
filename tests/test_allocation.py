import sqlite3

import pytest

from mtgtracker import db
from mtgtracker.allocation import AlreadyDisposedError, allocate


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    return conn


def _make_product_with_lots(conn, cost_basis_cents: int, lot_count: int) -> int:
    conn.execute(
        "INSERT INTO transactions (vendor, created_at, updated_at, subtotal_cents,"
        " tax_cents, total_cents, source) VALUES ('Test', 't', 't', 0, 0, 0, 'manual')"
    )
    txn_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute(
        "INSERT INTO products (transaction_id, title, display_name, qty,"
        " unit_price_cents, line_subtotal_cents, tax_allocated_cents,"
        " cost_basis_cents) VALUES (?, 'p', 'p', 1, ?, ?, 0, ?)",
        (txn_id, cost_basis_cents, cost_basis_cents, cost_basis_cents),
    )
    product_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    for i in range(lot_count):
        conn.execute(
            "INSERT INTO lots (product_id, transaction_id, card_name, acquired_date)"
            " VALUES (?, ?, ?, '2026-01-01')",
            (product_id, txn_id, f"Card {i}"),
        )

    return product_id


def test_allocate_reconciles_exactly_to_the_cent():
    conn = _make_conn()
    product_id = _make_product_with_lots(conn, cost_basis_cents=7798, lot_count=100)

    allocate(conn, product_id)

    total = conn.execute(
        "SELECT SUM(allocated_cost_cents) FROM lots WHERE product_id = ?", (product_id,)
    ).fetchone()[0]
    assert total == 7798


def test_allocate_is_deterministic_across_reruns():
    conn = _make_conn()
    product_id = _make_product_with_lots(conn, cost_basis_cents=4752, lot_count=100)

    allocate(conn, product_id)
    first = [
        row["allocated_cost_cents"]
        for row in conn.execute(
            "SELECT allocated_cost_cents FROM lots WHERE product_id = ? ORDER BY id",
            (product_id,),
        ).fetchall()
    ]

    allocate(conn, product_id)
    second = [
        row["allocated_cost_cents"]
        for row in conn.execute(
            "SELECT allocated_cost_cents FROM lots WHERE product_id = ? ORDER BY id",
            (product_id,),
        ).fetchall()
    ]

    assert first == second


def test_allocate_blocked_after_disposition():
    conn = _make_conn()
    product_id = _make_product_with_lots(conn, cost_basis_cents=1000, lot_count=4)

    allocate(conn, product_id)

    lot_id = conn.execute(
        "SELECT id FROM lots WHERE product_id = ? LIMIT 1", (product_id,)
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO dispositions (lot_id, disposed_at, disposition_type, proceeds_cents)"
        " VALUES (?, '2026-02-01', 'sold', 500)",
        (lot_id,),
    )

    with pytest.raises(AlreadyDisposedError):
        allocate(conn, product_id)


def _shares(conn, product_id):
    return [
        row["allocated_cost_cents"]
        for row in conn.execute(
            "SELECT allocated_cost_cents FROM lots WHERE product_id = ? ORDER BY id", (product_id,)
        ).fetchall()
    ]


def test_allocate_remainder_goes_to_lowest_lot_ids():
    conn = _make_conn()
    product_id = _make_product_with_lots(conn, cost_basis_cents=10, lot_count=3)

    lot_ids = allocate(conn, product_id)

    assert lot_ids == sorted(lot_ids)
    assert len(lot_ids) == 3
    assert _shares(conn, product_id) == [4, 3, 3]


def test_allocate_fewer_cents_than_lots():
    conn = _make_conn()
    product_id = _make_product_with_lots(conn, cost_basis_cents=2, lot_count=5)

    allocate(conn, product_id)

    assert _shares(conn, product_id) == [1, 1, 0, 0, 0]


def test_allocate_zero_cost_basis():
    conn = _make_conn()
    product_id = _make_product_with_lots(conn, cost_basis_cents=0, lot_count=3)

    allocate(conn, product_id)

    assert _shares(conn, product_id) == [0, 0, 0]


def test_allocate_only_touches_the_given_product():
    conn = _make_conn()
    first = _make_product_with_lots(conn, cost_basis_cents=300, lot_count=3)
    second = _make_product_with_lots(conn, cost_basis_cents=1000, lot_count=2)

    allocate(conn, first)

    assert _shares(conn, first) == [100, 100, 100]
    assert _shares(conn, second) == [None, None]


def test_allocate_unknown_product_raises():
    conn = _make_conn()
    with pytest.raises(ValueError, match="no product with id 999"):
        allocate(conn, 999)


def test_allocate_product_without_lots_raises():
    conn = _make_conn()
    product_id = _make_product_with_lots(conn, cost_basis_cents=1000, lot_count=0)
    with pytest.raises(ValueError, match="has no lots"):
        allocate(conn, product_id)


def test_disposition_on_other_product_does_not_block():
    conn = _make_conn()
    first = _make_product_with_lots(conn, cost_basis_cents=100, lot_count=2)
    second = _make_product_with_lots(conn, cost_basis_cents=100, lot_count=2)
    other_lot = conn.execute("SELECT id FROM lots WHERE product_id = ? LIMIT 1", (second,)).fetchone()["id"]
    conn.execute(
        "INSERT INTO dispositions (lot_id, disposed_at, disposition_type, proceeds_cents)"
        " VALUES (?, '2026-02-01', 'sold', 500)",
        (other_lot,),
    )

    allocate(conn, first)
    assert _shares(conn, first) == [50, 50]

    with pytest.raises(AlreadyDisposedError):
        allocate(conn, second)


def test_blocked_allocation_leaves_existing_shares_untouched():
    conn = _make_conn()
    product_id = _make_product_with_lots(conn, cost_basis_cents=1000, lot_count=4)
    allocate(conn, product_id)
    before = _shares(conn, product_id)

    lot_id = conn.execute("SELECT id FROM lots WHERE product_id = ? LIMIT 1", (product_id,)).fetchone()["id"]
    conn.execute(
        "INSERT INTO dispositions (lot_id, disposed_at, disposition_type, proceeds_cents)"
        " VALUES (?, '2026-02-01', 'sold', 500)",
        (lot_id,),
    )
    conn.execute("UPDATE products SET cost_basis_cents = 2000 WHERE id = ?", (product_id,))

    with pytest.raises(AlreadyDisposedError):
        allocate(conn, product_id)

    assert _shares(conn, product_id) == before
