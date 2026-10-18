"""Tests for scripts/scrub_order.py. The committed fixture is already
scrubbed, so each test first builds a "dirty" copy with real-looking
credentials/PII injected, then scrubs that."""

import importlib.util
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from mtgtracker import db
from mtgtracker.importers.order_wallet import import_order_file

REPO_ROOT = Path(__file__).parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "order_scrubbed.order"

_spec = importlib.util.spec_from_file_location("scrub_order", REPO_ROOT / "scripts" / "scrub_order.py")
scrub_order = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scrub_order)

DIRTY = {
    "authenticationToken": "eyJREAL.SECRET.TOKEN-abc123",
    "orderManagementURL": "https://shop.example.net/orders/9f8e7d?key=REALKEY42",
    "orderIdentifier": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
    "emailAddress": "jane.q.realperson@mail.test",
    "familyName": "Realperson",
    "givenName": "Jane",
    "launchURL": "https://shop.example.net/launch/REALLAUNCH",
}


def _read_zip(path):
    with zipfile.ZipFile(path) as zf:
        return {name: zf.read(name) for name in zf.namelist()}, zf.namelist()


@pytest.fixture
def dirty_order(tmp_path):
    contents, names = _read_zip(FIXTURE)
    order = json.loads(contents["order.json"])
    order["authenticationToken"] = DIRTY["authenticationToken"]
    order["orderManagementURL"] = DIRTY["orderManagementURL"]
    order["orderIdentifier"] = DIRTY["orderIdentifier"]
    order["customer"] = {
        "emailAddress": DIRTY["emailAddress"],
        "familyName": DIRTY["familyName"],
        "givenName": DIRTY["givenName"],
    }
    order["associatedApplications"][0]["launchURL"] = DIRTY["launchURL"]
    contents["order.json"] = json.dumps(order).encode()

    path = tmp_path / "dirty.order"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as dst:
        for name in names:
            dst.writestr(name, contents[name])
    return path


@pytest.fixture
def scrubbed_order(dirty_order, tmp_path):
    out = tmp_path / "scrubbed.order"
    scrub_order.scrub(dirty_order, out)
    return out


def test_scrub_replaces_credentials_and_pii_with_fakes(scrubbed_order):
    contents, _ = _read_zip(scrubbed_order)
    order = json.loads(contents["order.json"])

    assert order["authenticationToken"] == scrub_order.FAKE_AUTH_TOKEN
    assert order["orderManagementURL"] == scrub_order.FAKE_ORDER_MANAGEMENT_URL
    assert order["orderIdentifier"] == scrub_order.FAKE_ORDER_IDENTIFIER
    assert order["customer"] == {
        "emailAddress": scrub_order.FAKE_EMAIL,
        "familyName": scrub_order.FAKE_FAMILY_NAME,
        "givenName": scrub_order.FAKE_GIVEN_NAME,
    }
    assert order["associatedApplications"][0]["launchURL"] == scrub_order.FAKE_LAUNCH_URL

    raw = contents["order.json"].decode()
    for value in DIRTY.values():
        assert value not in raw


def test_scrub_leaves_purchase_data_unchanged(dirty_order, scrubbed_order):
    dirty_contents, dirty_names = _read_zip(dirty_order)
    clean_contents, clean_names = _read_zip(scrubbed_order)

    # Same entries, same order; everything except order.json byte-identical.
    assert clean_names == dirty_names
    for name in dirty_names:
        if name != "order.json":
            assert clean_contents[name] == dirty_contents[name], name

    dirty = json.loads(dirty_contents["order.json"])
    clean = json.loads(clean_contents["order.json"])
    scrubbed_keys = {"authenticationToken", "orderManagementURL", "orderIdentifier", "customer"}
    for key in dirty.keys() - scrubbed_keys - {"associatedApplications"}:
        assert clean[key] == dirty[key], key
    assert clean.keys() == dirty.keys()

    for key in ("payment", "lineItems", "fulfillments", "merchant", "createdAt", "updatedAt", "status"):
        assert clean[key] == dirty[key]


def test_scrubbed_order_imports_identically(dirty_order, scrubbed_order, tmp_path):
    def import_into_fresh_db(path):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.migrate(conn)
        result = import_order_file(conn, path, tmp_path / f"receipts_{path.stem}")
        txn = dict(
            conn.execute(
                "SELECT order_number, vendor, status, subtotal_cents, tax_cents, shipping_cents,"
                " total_cents, currency, payment_method FROM transactions WHERE id = ?",
                (result.transaction_id,),
            ).fetchone()
        )
        products = [
            dict(r)
            for r in conn.execute(
                "SELECT sku, title, display_name, qty, unit_price_cents, line_subtotal_cents,"
                " tax_allocated_cents, cost_basis_cents FROM products ORDER BY id"
            ).fetchall()
        ]
        return txn, products

    assert import_into_fresh_db(scrubbed_order) == import_into_fresh_db(dirty_order)


def test_scrub_order_json_tolerates_missing_optional_fields():
    raw = json.dumps({"status": "open", "payment": {"total": {"amount": "1"}}}).encode()
    assert json.loads(scrub_order.scrub_order_json(raw)) == json.loads(raw)


def test_scrub_removes_card_digits_store_and_signature(scrubbed_order):
    contents, _ = _read_zip(scrubbed_order)
    data = json.loads(contents["order.json"])
    assert data["payment"]["transactions"][0]["paymentMethod"]["displayName"] == "Visa 0000"
    assert data["orderNumber"] == "#00000"
    assert data["merchant"]["displayName"] == "Example Games"
    assert set(data["fulfillments"][0]["fulfillmentIdentifier"]) == {"0"}
    assert "signature" not in contents
    manifest = json.loads(contents["manifest.json"])
    assert set(manifest) == set(contents) - {"manifest.json"}


def test_scrubbing_is_idempotent(scrubbed_order, tmp_path):
    twice = tmp_path / "twice.order"
    scrub_order.scrub(scrubbed_order, twice)
    assert _read_zip(twice)[0]["order.json"] == _read_zip(scrubbed_order)[0]["order.json"]


def test_main_requires_two_arguments(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["scrub_order.py", "only-one"])
    with pytest.raises(SystemExit) as exc_info:
        scrub_order.main()
    assert exc_info.value.code == 1
    assert "usage:" in capsys.readouterr().err


def test_main_scrubs_file(dirty_order, tmp_path, monkeypatch):
    out = tmp_path / "via_main.order"
    monkeypatch.setattr("sys.argv", ["scrub_order.py", str(dirty_order), str(out)])
    scrub_order.main()
    order = json.loads(_read_zip(out)[0]["order.json"])
    assert order["authenticationToken"] == scrub_order.FAKE_AUTH_TOKEN
