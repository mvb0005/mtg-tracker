"""Recording a purchase of singles, and undoing one: the flows behind
/purchases/*, POST/DELETE /api/v1/purchases, and `mtg record-purchase`.
Guards (whose purchase it is) stay with the caller."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

import httpx

from mtgtracker import purchases, users
from mtgtracker.importers import cart_text
from mtgtracker.services import CodedError, NotFoundError, ServiceError, UpstreamError
from mtgtracker.services.collection import blockers_text, lot_blockers

MAX_CART_BYTES = 512 * 1024


class CardsNotFound(CodedError):
    code = "cards_not_found"
    status = 422


class PurchaseInUseError(CodedError):
    """Some of the purchase's cards are already part of something else."""

    code = "purchase_in_use"
    status = 409


@contextmanager
def _translated() -> Iterator[None]:
    try:
        yield
    except purchases.CardsNotFoundError as exc:
        raise CardsNotFound(str(exc), names=exc.names) from exc
    except (purchases.PurchaseError, cart_text.CartParseError) as exc:
        raise ServiceError(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise UpstreamError("Couldn't reach Scryfall; nothing was recorded") from exc


def preview_purchase(
    conn: sqlite3.Connection, owner: users.User, cart: str, *, vendor: str, date: str | None,
    tax_cents: int, shipping_cents: int, total_cents: int,
) -> purchases.PurchasePlan:
    """Parse, resolve and reconcile a cart. Writes nothing to the database."""
    if len(cart.encode()) > MAX_CART_BYTES:
        raise ServiceError("That cart is too big (512 KB at most)")
    with _translated():
        return purchases.plan(
            conn, cart, owner_id=owner.id, vendor=vendor, date=date, tax_cents=tax_cents,
            shipping_cents=shipping_cents, total_cents=total_cents,
        )


def record_purchase(
    conn: sqlite3.Connection, owner: users.User, cart: str, *, vendor: str, date: str | None,
    tax_cents: int, shipping_cents: int, total_cents: int, commit: bool = True,
) -> tuple[purchases.PurchasePlan, purchases.PurchaseSummary]:
    """Everything resolves and reconciles before the first write; then the
    transaction, products and lots are written together."""
    plan = preview_purchase(
        conn, owner, cart, vendor=vendor, date=date, tax_cents=tax_cents,
        shipping_cents=shipping_cents, total_cents=total_cents,
    )
    return plan, purchases.record(conn, plan, commit=commit)


def get_purchase(conn: sqlite3.Connection, purchase_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT transactions.*, users.display_name AS owner_name FROM transactions"
        " JOIN users ON users.id = transactions.owner_id WHERE transactions.id = ?",
        (purchase_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("No such purchase")
    return row


def delete_purchase(
    conn: sqlite3.Connection, purchase_id: int, *, commit: bool = True
) -> tuple[str, dict]:
    """Undo a manually recorded purchase: delete its lots, products and the
    transaction in one transaction. Refuses (PurchaseInUseError) if any lot
    is on a deck's list, disposed of, in a version snapshot or in a change,
    or if the transaction has receipts or deck costs attached; nothing is
    written then. A wallet order can't be undone here. The caller checks the
    actor may (the purchase's owner). Returns (notice, preview); the
    preview is deterministic so it can be confirmed by digest."""
    txn = get_purchase(conn, purchase_id)
    if txn["source"] != "manual":
        raise ServiceError("Only a purchase recorded by hand can be undone")
    lots = conn.execute("SELECT COUNT(*) FROM lots WHERE transaction_id = ?", (purchase_id,)).fetchone()[0]
    blockers = lot_blockers(conn, "lots.transaction_id = ?", (purchase_id,))
    for name, table in (("has_receipts", "receipts"), ("has_deck_costs", "deck_costs")):
        query = f"SELECT COUNT(*) FROM {table} WHERE transaction_id = ?"
        n = conn.execute(query, (purchase_id,)).fetchone()[0]
        if n:
            blockers[name] = n
    if blockers:
        text = blockers_text({k: v for k, v in blockers.items() if not k.startswith("has_")})
        extra = [f"{blockers[k]} {label}" for k, label in
                 (("has_receipts", "receipt(s) attached"), ("has_deck_costs", "deck cost(s) attached"))
                 if k in blockers]
        raise PurchaseInUseError(
            "Can't undo that purchase: " + ", ".join(filter(None, [text, *extra])), blockers=blockers)
    preview = {
        "action": "delete_purchase", "purchase_id": purchase_id, "owner_id": txn["owner_id"],
        "owner": txn["owner_name"], "vendor": txn["vendor"], "date": txn["created_at"][:10],
        "total_cents": txn["total_cents"], "lots": lots,
        "effect": "deletes these cards, the purchase's lines and the purchase record",
    }
    conn.execute("DELETE FROM lots WHERE transaction_id = ?", (purchase_id,))
    conn.execute("DELETE FROM products WHERE transaction_id = ?", (purchase_id,))
    conn.execute("DELETE FROM transactions WHERE id = ?", (purchase_id,))
    if commit:
        conn.commit()
    return f"Removed the purchase from {txn['vendor']}: {lots} card{'s' if lots != 1 else ''}.", preview
