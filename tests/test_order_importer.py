import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from mtgtracker import db
from mtgtracker.importers.order_wallet import import_order_file

FIXTURE = Path(__file__).parent / "fixtures" / "order_scrubbed.order"


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    return conn


def _modify_fixture(tmp_path: Path, **order_overrides) -> Path:
    with zipfile.ZipFile(FIXTURE, "r") as src:
        names = src.namelist()
        contents = {name: src.read(name) for name in names}

    order = json.loads(contents["order.json"])
    order.update(order_overrides)
    contents["order.json"] = json.dumps(order).encode("utf-8")

    out_path = tmp_path / "modified.order"
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as dst:
        for name in names:
            dst.writestr(name, contents[name])
    return out_path


def test_first_import_creates_transaction_products_and_receipt(tmp_path):
    conn = _make_conn()
    receipts_dir = tmp_path / "receipts"

    result = import_order_file(conn, FIXTURE, receipts_dir)

    assert result.created is True
    assert result.receipt_created is True

    txn = conn.execute(
        "SELECT * FROM transactions WHERE id = ?", (result.transaction_id,)
    ).fetchone()
    assert txn["vendor"] == "Example Games"
    assert txn["order_number"] == "#00000"
    assert txn["subtotal_cents"] == 7798
    assert txn["tax_cents"] == 822
    assert txn["shipping_cents"] == 0
    assert txn["total_cents"] == 8620
    assert txn["source"] == "wallet_order"

    products = conn.execute(
        "SELECT * FROM products WHERE transaction_id = ? ORDER BY id", (result.transaction_id,)
    ).fetchall()
    assert len(products) == 2

    reign, keen = products
    assert reign["display_name"] == "Foundations Commander Deck - Reign of Dragons"
    assert reign["unit_price_cents"] == 4299
    assert reign["tax_allocated_cents"] == 453
    assert reign["cost_basis_cents"] == 4752

    assert keen["display_name"] == "Foundations Commander Deck - Keen Engineering"
    assert keen["unit_price_cents"] == 3499
    assert keen["tax_allocated_cents"] == 369
    assert keen["cost_basis_cents"] == 3868

    assert reign["cost_basis_cents"] + keen["cost_basis_cents"] == txn["total_cents"]

    receipts = conn.execute("SELECT * FROM receipts").fetchall()
    assert len(receipts) == 1
    assert receipts[0]["transaction_id"] == result.transaction_id
    assert receipts[0]["kind"] == "wallet_order"

    stored_files = list(receipts_dir.iterdir())
    assert len(stored_files) == 1
    assert stored_files[0].name == f"{receipts[0]['sha256']}.order"


def test_reimporting_identical_file_is_a_noop(tmp_path):
    conn = _make_conn()
    receipts_dir = tmp_path / "receipts"

    first = import_order_file(conn, FIXTURE, receipts_dir)
    second = import_order_file(conn, FIXTURE, receipts_dir)

    assert second.transaction_id == first.transaction_id
    assert second.created is False
    assert second.receipt_created is False

    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


def test_newer_order_updates_in_place_and_adds_a_receipt(tmp_path):
    conn = _make_conn()
    receipts_dir = tmp_path / "receipts"

    first = import_order_file(conn, FIXTURE, receipts_dir)

    newer_path = _modify_fixture(
        tmp_path, status="delivered", updatedAt="2099-01-01T00:00:00Z"
    )
    second = import_order_file(conn, newer_path, receipts_dir)

    assert second.transaction_id == first.transaction_id
    assert second.created is False
    assert second.updated is True
    assert second.receipt_created is True

    txn = conn.execute(
        "SELECT * FROM transactions WHERE id = ?", (first.transaction_id,)
    ).fetchone()
    assert txn["status"] == "delivered"
    assert txn["updated_at"] == "2099-01-01T00:00:00Z"

    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 2


def test_older_order_does_not_regress_the_transaction(tmp_path):
    conn = _make_conn()
    receipts_dir = tmp_path / "receipts"

    import_order_file(conn, FIXTURE, receipts_dir)

    older_path = _modify_fixture(
        tmp_path, status="pending", updatedAt="2000-01-01T00:00:00Z"
    )
    result = import_order_file(conn, older_path, receipts_dir)

    assert result.updated is False

    txn = conn.execute(
        "SELECT * FROM transactions WHERE id = ?", (result.transaction_id,)
    ).fetchone()
    assert txn["status"] == "open"  # unchanged from the original fixture


def test_reconciliation_mismatch_raises(tmp_path):
    conn = _make_conn()
    receipts_dir = tmp_path / "receipts"

    bad_path = _modify_fixture(
        tmp_path,
        payment={
            "status": "paid",
            "total": {"amount": "999.99", "currency": "USD"},
            "summaryItems": [
                {"label": "Tax", "value": {"amount": "8.22", "currency": "USD"}},
                {"label": "Subtotal", "value": {"amount": "77.98", "currency": "USD"}},
            ],
            "transactions": [],
        },
    )

    with pytest.raises(ValueError, match="does not reconcile"):
        import_order_file(conn, bad_path, receipts_dir)
