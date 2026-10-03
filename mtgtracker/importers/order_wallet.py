"""Importer for Apple Wallet .order files (order.json inside a zip).

Behavior:
- The receipt file is content-addressed by sha256. Importing byte-identical
  content twice is a no-op (the second import just returns the existing
  transaction id).
- Transactions are upserted on (order_type_identifier, order_identifier).
  A transaction is only updated if the incoming file's updatedAt is newer
  than what's stored, so importing an older copy of the same order doesn't
  regress it.
- Products are only created the first time a transaction is created, not on
  update — .order updates in practice only change status/fulfillment, not
  line items.
- SKUs live only in fulfillments[].lineItems, not the top-level lineItems,
  so they're joined by (title, price).
- Raw product titles (which can carry suffixes like "(Available on 9/25)")
  are kept as-is in `title`; `display_name` strips that suffix.
- Amounts are parsed via Decimal, never float.
- subtotal + tax + shipping must equal total, and product lines must sum to
  subtotal — a mismatch raises rather than being silently coerced.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mtgtracker.money import largest_remainder_split, parse_cents

AVAILABILITY_SUFFIX_RE = re.compile(r"\s*\(Available on [^)]*\)\s*$")


@dataclass
class ImportResult:
    transaction_id: int
    created: bool
    updated: bool
    receipt_created: bool


def _clean_display_name(title: str) -> str:
    return AVAILABILITY_SUFFIX_RE.sub("", title).strip()


def _find_summary_amount(summary_items: list[dict], label: str) -> str | None:
    for item in summary_items:
        if item.get("label") == label:
            return item["value"]["amount"]
    return None


def _find_sku(fulfillments: list[dict], title: str, price_amount: str) -> str | None:
    for fulfillment in fulfillments:
        for line in fulfillment.get("lineItems", []):
            if line.get("title") == title and line.get("price", {}).get("amount") == price_amount:
                return line.get("sku")
    return None


def import_order_file(
    conn: sqlite3.Connection, path: str | Path, receipts_dir: str | Path, *, owner_id: int = 1
) -> ImportResult:
    """Import an Apple Wallet order for `owner_id` (see module docstring)."""
    path = Path(path)
    receipts_dir = Path(receipts_dir)
    receipts_dir.mkdir(parents=True, exist_ok=True)

    raw_bytes = path.read_bytes()
    file_hash = hashlib.sha256(raw_bytes).hexdigest()

    existing_receipt = conn.execute(
        "SELECT id, transaction_id FROM receipts WHERE sha256 = ?", (file_hash,)
    ).fetchone()
    if existing_receipt is not None:
        return ImportResult(
            transaction_id=existing_receipt["transaction_id"],
            created=False,
            updated=False,
            receipt_created=False,
        )

    with zipfile.ZipFile(path, "r") as zf:
        order = json.loads(zf.read("order.json"))

    order_type_identifier = order["orderTypeIdentifier"]
    order_identifier = order["orderIdentifier"]
    order_number = order.get("orderNumber")
    vendor = order["merchant"]["displayName"]
    status = order.get("status")
    created_at = order["createdAt"]
    updated_at = order["updatedAt"]

    payment = order["payment"]
    total_cents = parse_cents(payment["total"]["amount"])
    currency = payment["total"]["currency"]

    summary_items = payment.get("summaryItems", [])
    tax_amount = _find_summary_amount(summary_items, "Tax")
    subtotal_amount = _find_summary_amount(summary_items, "Subtotal")
    shipping_amount = _find_summary_amount(summary_items, "Shipping")

    if subtotal_amount is None:
        raise ValueError("order.json has no 'Subtotal' summary item")
    tax_cents = parse_cents(tax_amount) if tax_amount is not None else 0
    subtotal_cents = parse_cents(subtotal_amount)
    shipping_cents = parse_cents(shipping_amount) if shipping_amount is not None else 0

    if subtotal_cents + tax_cents + shipping_cents != total_cents:
        raise ValueError(
            "order does not reconcile: subtotal "
            f"({subtotal_cents}) + tax ({tax_cents}) + shipping "
            f"({shipping_cents}) != total ({total_cents})"
        )

    payment_method = None
    transactions = payment.get("transactions", [])
    if transactions:
        payment_method = transactions[0].get("paymentMethod", {}).get("displayName")

    line_items = order.get("lineItems", [])
    fulfillments = order.get("fulfillments", [])

    line_subtotals = [parse_cents(li["price"]["amount"]) * li.get("quantity", 1) for li in line_items]
    if sum(line_subtotals) != subtotal_cents:
        raise ValueError(
            f"product lines sum to {sum(line_subtotals)} cents, "
            f"which does not match subtotal {subtotal_cents} cents"
        )

    # tax + shipping allocated pro-rata across products, by line subtotal
    extra_cents = tax_cents + shipping_cents
    # With nothing to allocate, every share is 0; skip the split so an
    # all-$0 order (weights summing to 0) doesn't trip its sum > 0 check.
    if extra_cents == 0:
        extra_allocations = [0] * len(line_subtotals)
    else:
        extra_allocations = largest_remainder_split(extra_cents, line_subtotals) if line_subtotals else []

    existing_txn = conn.execute(
        "SELECT id, updated_at, owner_id FROM transactions"
        " WHERE order_type_identifier = ? AND order_identifier = ?",
        (order_type_identifier, order_identifier),
    ).fetchone()
    if existing_txn is not None and existing_txn["owner_id"] != owner_id:
        raise ValueError("that order was already imported by another user")

    created = False
    updated = False

    if existing_txn is None:
        cur = conn.execute(
            """
            INSERT INTO transactions (
                order_type_identifier, order_identifier, order_number, vendor,
                status, created_at, updated_at, subtotal_cents, tax_cents,
                shipping_cents, total_cents, currency, payment_method, source, owner_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'wallet_order', ?)
            """,
            (
                order_type_identifier, order_identifier, order_number, vendor,
                status, created_at, updated_at, subtotal_cents, tax_cents,
                shipping_cents, total_cents, currency, payment_method, owner_id,
            ),
        )
        transaction_id = cur.lastrowid
        created = True

        for line, line_subtotal, extra_alloc in zip(
            line_items, line_subtotals, extra_allocations, strict=True
        ):
            title = line["title"]
            unit_price_cents = parse_cents(line["price"]["amount"])
            qty = line.get("quantity", 1)
            sku = _find_sku(fulfillments, title, line["price"]["amount"])
            conn.execute(
                """
                INSERT INTO products (
                    transaction_id, sku, title, display_name, qty,
                    unit_price_cents, line_subtotal_cents, tax_allocated_cents,
                    cost_basis_cents, product_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    transaction_id, sku, title, _clean_display_name(title), qty,
                    unit_price_cents, line_subtotal, extra_alloc,
                    line_subtotal + extra_alloc,
                ),
            )
    else:
        transaction_id = existing_txn["id"]
        if updated_at > existing_txn["updated_at"]:
            conn.execute(
                """
                UPDATE transactions
                SET status = ?, updated_at = ?, payment_method = ?
                WHERE id = ?
                """,
                (status, updated_at, payment_method, transaction_id),
            )
            updated = True

    receipt_dest = receipts_dir / f"{file_hash}.order"
    if not receipt_dest.exists():
        receipt_dest.write_bytes(raw_bytes)

    conn.execute(
        """
        INSERT INTO receipts (
            transaction_id, sha256, original_filename, mime_type, file_ext,
            imported_at, kind
        ) VALUES (?, ?, ?, ?, ?, ?, 'wallet_order')
        """,
        (
            transaction_id, file_hash, path.name, "application/vnd.apple.pkpass+order",
            "order", datetime.now(UTC).isoformat(),
        ),
    )

    conn.commit()

    return ImportResult(
        transaction_id=transaction_id,
        created=created,
        updated=updated,
        receipt_created=True,
    )
