"""Cost allocation: split a product's cost_basis_cents evenly across the
lots that came from it, using the largest-remainder method so cents always
reconcile exactly. Re-running is blocked once any lot in the product has a
disposition recorded, since that would silently change a booked realized
gain.
"""

from __future__ import annotations

import sqlite3

from mtgtracker.money import largest_remainder_split


class AlreadyDisposedError(Exception):
    """Raised when allocation is re-run on a product with a disposed lot."""


def allocate(conn: sqlite3.Connection, product_id: int, *, commit: bool = True) -> list[int]:
    """Allocate a product's cost basis evenly across its lots.

    Returns the list of lot ids in the order their allocated_cost_cents was
    assigned (ascending lot id — the deterministic tie-break order).
    """
    product = conn.execute(
        "SELECT cost_basis_cents FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    if product is None:
        raise ValueError(f"no product with id {product_id}")

    lots = conn.execute(
        "SELECT lots.id FROM lots"
        " LEFT JOIN dispositions ON dispositions.lot_id = lots.id"
        " WHERE lots.product_id = ?"
        " ORDER BY lots.id",
        (product_id,),
    ).fetchall()
    if not lots:
        raise ValueError(f"product {product_id} has no lots to allocate cost to")

    lot_ids = [row["id"] for row in lots]

    disposed_count = conn.execute(
        "SELECT COUNT(*) FROM dispositions"
        " JOIN lots ON lots.id = dispositions.lot_id"
        " WHERE lots.product_id = ?",
        (product_id,),
    ).fetchone()[0]
    if disposed_count > 0:
        raise AlreadyDisposedError(
            f"product {product_id} has {disposed_count} disposed lot(s); "
            "re-running allocation would change a booked realized gain"
        )

    weights = [1] * len(lot_ids)
    shares = largest_remainder_split(product["cost_basis_cents"], weights)

    for lot_id, share in zip(lot_ids, shares, strict=True):
        conn.execute(
            "UPDATE lots SET allocated_cost_cents = ? WHERE id = ?",
            (share, lot_id),
        )
    if commit:
        conn.commit()

    return lot_ids
