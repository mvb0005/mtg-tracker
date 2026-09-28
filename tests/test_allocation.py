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
