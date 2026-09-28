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


def _payment(total, subtotal, tax=None, shipping=None, transactions=None):
    items = []
    if tax is not None:
        items.append({"label": "Tax", "value": {"amount": tax, "currency": "USD"}})
    if subtotal is not None:
        items.append({"label": "Subtotal", "value": {"amount": subtotal, "currency": "USD"}})
    if shipping is not None:
        items.append({"label": "Shipping", "value": {"amount": shipping, "currency": "USD"}})
    return {
        "status": "paid",
        "total": {"amount": total, "currency": "USD"},
        "summaryItems": items,
        "transactions": transactions if transactions is not None else [],
    }


def _line(title, amount, quantity=1):
    return {"title": title, "price": {"amount": amount, "currency": "USD"}, "quantity": quantity}


def test_missing_subtotal_raises(tmp_path):
    conn = _make_conn()
    bad_path = _modify_fixture(tmp_path, payment=_payment("86.2", None, tax="8.22"))

    with pytest.raises(ValueError, match="no 'Subtotal'"):
        import_order_file(conn, bad_path, tmp_path / "receipts")


def test_line_items_not_summing_to_subtotal_raises(tmp_path):
    conn = _make_conn()
    receipts_dir = tmp_path / "receipts"
    # Totals reconcile, but the line items only add up to 77.97.
    bad_path = _modify_fixture(
        tmp_path,
        lineItems=[_line("A", "42.99"), _line("B", "34.98")],
    )

    with pytest.raises(ValueError, match="does not match subtotal"):
        import_order_file(conn, bad_path, receipts_dir)

    # Nothing half-written, and no receipt stored for a rejected order.
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert list(receipts_dir.iterdir()) == []


def test_fractional_cent_amount_raises(tmp_path):
    conn = _make_conn()
    bad_path = _modify_fixture(tmp_path, payment=_payment("86.205", "77.985", tax="8.22"))

    with pytest.raises(ValueError, match="not a whole number of cents"):
        import_order_file(conn, bad_path, tmp_path / "receipts")


def test_shipping_and_quantity_are_allocated_pro_rata(tmp_path):
    conn = _make_conn()
    # 2 x 10.00 + 1 x 5.00 = 25.00 subtotal; tax 2.00 + shipping 1.01 = 301c
    # extra, split 2000:500 -> exact 240.8 / 60.2 -> floors 240 + 60 = 300,
    # and the 1 leftover cent goes to the larger remainder (index 0: .8).
    path = _modify_fixture(
        tmp_path,
        payment=_payment("28.01", "25.00", tax="2.00", shipping="1.01"),
        lineItems=[_line("Booster Pack", "10.00", quantity=2), _line("Sleeves", "5.00")],
        fulfillments=[],
    )

    result = import_order_file(conn, path, tmp_path / "receipts")

    txn = conn.execute("SELECT * FROM transactions WHERE id = ?", (result.transaction_id,)).fetchone()
    assert txn["shipping_cents"] == 101
    assert txn["tax_cents"] == 200
    assert txn["payment_method"] is None  # no payment transactions listed

    booster, sleeves = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
    assert booster["qty"] == 2
    assert booster["unit_price_cents"] == 1000
    assert booster["line_subtotal_cents"] == 2000
    assert booster["tax_allocated_cents"] == 241
    assert booster["cost_basis_cents"] == 2241
    assert sleeves["line_subtotal_cents"] == 500
    assert sleeves["tax_allocated_cents"] == 60
    assert sleeves["cost_basis_cents"] == 560
    assert booster["cost_basis_cents"] + sleeves["cost_basis_cents"] == txn["total_cents"]

    # No fulfillments -> no SKU to join on.
    assert booster["sku"] is None
    assert sleeves["sku"] is None
    # No availability suffix -> display_name == title.
    assert booster["display_name"] == booster["title"] == "Booster Pack"


def test_sku_joined_from_fulfillments_and_availability_suffix_stripped(tmp_path):
    conn = _make_conn()
    import_order_file(conn, FIXTURE, tmp_path / "receipts")

    rows = conn.execute("SELECT sku, title, display_name FROM products ORDER BY id").fetchall()
    assert [r["sku"] for r in rows] == ["157251", "157249"]
    assert rows[0]["title"].endswith("(Available on 9/25)")
    assert "(Available" not in rows[0]["display_name"]


def test_zero_dollar_order_imports(tmp_path):
    # A reconciling $0 order (e.g. a free promo) with no tax or shipping to
    # allocate. Previously this raised "weights must sum to a positive
    # number" from largest_remainder_split because every line weight is 0.
    conn = _make_conn()
    path = _modify_fixture(
        tmp_path,
        payment=_payment("0.00", "0.00", tax="0.00"),
        lineItems=[_line("Promo Card", "0.00"), _line("Promo Card 2", "0.00")],
        fulfillments=[],
    )

    result = import_order_file(conn, path, tmp_path / "receipts")

    assert result.created is True
    products = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
    assert [p["cost_basis_cents"] for p in products] == [0, 0]
    assert [p["tax_allocated_cents"] for p in products] == [0, 0]


def test_tax_on_all_zero_dollar_lines_still_raises(tmp_path):
    # Tax with no priced lines to spread it over can't be allocated; that
    # stays an error rather than being silently dropped or guessed.
    conn = _make_conn()
    path = _modify_fixture(
        tmp_path,
        payment=_payment("1.00", "0.00", tax="1.00"),
        lineItems=[_line("Promo Card", "0.00")],
        fulfillments=[],
    )

    with pytest.raises(ValueError, match="sum to a positive number"):
        import_order_file(conn, path, tmp_path / "receipts")


def test_same_updated_at_different_bytes_does_not_update_but_stores_receipt(tmp_path):
    conn = _make_conn()
    receipts_dir = tmp_path / "receipts"
    first = import_order_file(conn, FIXTURE, receipts_dir)

    # Different file content (so a different sha256), same updatedAt.
    same_time = _modify_fixture(tmp_path, status="delivered")
    result = import_order_file(conn, same_time, receipts_dir)

    assert result.transaction_id == first.transaction_id
    assert result.created is False
    assert result.updated is False
    assert result.receipt_created is True

    assert conn.execute("SELECT status FROM transactions").fetchone()["status"] == "open"
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 2
    assert len(list(receipts_dir.iterdir())) == 2


def test_existing_receipt_file_on_disk_is_not_rewritten(tmp_path):
    # A receipts dir shared with another database already holds the file.
    receipts_dir = tmp_path / "receipts"
    import_order_file(_make_conn(), FIXTURE, receipts_dir)
    stored = next(receipts_dir.iterdir())
    mtime = stored.stat().st_mtime_ns

    conn = _make_conn()
    result = import_order_file(conn, FIXTURE, receipts_dir)

    assert result.created is True
    assert result.receipt_created is True
    assert stored.stat().st_mtime_ns == mtime
    assert stored.read_bytes() == FIXTURE.read_bytes()
