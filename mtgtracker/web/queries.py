"""Read-only SQL for the web UI.

Collection-wide totals always sum `transactions`/`lots` once. They never sum
across decks: deck_cards is many-to-many, so a borrowed card sits on more than
one deck's list and per-deck sums would double-count it.
"""

from __future__ import annotations

import sqlite3

from mtgtracker.decks import reassemble

BASIC_LAND_NAMES = ("Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes")
_BASICS_SQL = ", ".join("?" for _ in BASIC_LAND_NAMES)


def dashboard_stats(conn: sqlite3.Connection) -> dict:
    spend = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(total_cents), 0) AS total FROM transactions"
    ).fetchone()
    cards = conn.execute(
        "SELECT COUNT(*) FROM lots"
        " WHERE NOT EXISTS (SELECT 1 FROM dispositions WHERE dispositions.lot_id = lots.id)"
    ).fetchone()[0]
    decks = conn.execute("SELECT COUNT(*) FROM decks").fetchone()[0]
    return {
        "transaction_count": spend["n"],
        "total_spent_cents": spend["total"],
        "card_count": cards,
        "deck_count": decks,
        "needs_input_count": sum(row["qty"] for row in needs_input(conn)),
    }


def list_transactions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT transactions.*,
               (SELECT COUNT(*) FROM products WHERE products.transaction_id = transactions.id)
                   AS product_count
        FROM transactions
        ORDER BY created_at DESC, id DESC
        """
    ).fetchall()


def get_transaction(conn: sqlite3.Connection, transaction_id: int) -> dict | None:
    txn = conn.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
    if txn is None:
        return None
    products = conn.execute(
        """
        SELECT products.*,
               (SELECT COUNT(*) FROM lots WHERE lots.product_id = products.id) AS lot_count
        FROM products
        WHERE transaction_id = ?
        ORDER BY id
        """,
        (transaction_id,),
    ).fetchall()
    # Receipt metadata only -- the files themselves hold credentials and are
    # never served.
    receipts = conn.execute(
        "SELECT original_filename, kind, imported_at FROM receipts"
        " WHERE transaction_id = ? ORDER BY imported_at",
        (transaction_id,),
    ).fetchall()
    return {"txn": txn, "products": products, "receipts": receipts}


def list_decks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT decks.*,
               COUNT(deck_cards.lot_id) AS list_count,
               COALESCE(SUM(deck_cards.equipped), 0) AS equipped_count
        FROM decks
        LEFT JOIN deck_cards ON deck_cards.deck_id = decks.id
        GROUP BY decks.id
        ORDER BY decks.id
        """
    ).fetchall()


def get_deck(conn: sqlite3.Connection, deck_id: int) -> dict | None:
    deck = conn.execute("SELECT * FROM decks WHERE id = ?", (deck_id,)).fetchone()
    if deck is None:
        return None

    # Physical lots are grouped for display only (e.g. "34x Island"); each
    # group shares name, printing, finish and current location.
    groups = conn.execute(
        f"""
        SELECT lots.card_name, lots.set_code, lots.collector_number, lots.finish,
               lots.scryfall_id IS NULL AND lots.card_name NOT IN ({_BASICS_SQL}) AS needs_input,
               deck_cards.equipped,
               current.deck_id AS current_deck_id,
               current_deck.name AS current_deck_name,
               COUNT(*) AS qty,
               SUM(lots.allocated_cost_cents) AS cost_cents
        FROM deck_cards
        JOIN lots ON lots.id = deck_cards.lot_id
        LEFT JOIN deck_cards AS current
               ON current.lot_id = lots.id AND current.equipped = 1
        LEFT JOIN decks AS current_deck ON current_deck.id = current.deck_id
        WHERE deck_cards.deck_id = ?
        GROUP BY lots.card_name, lots.set_code, lots.collector_number, lots.finish,
                 deck_cards.equipped, current.deck_id
        ORDER BY lots.card_name COLLATE NOCASE
        """,
        (*BASIC_LAND_NAMES, deck_id),
    ).fetchall()

    list_cost = conn.execute(
        "SELECT COALESCE(SUM(lots.allocated_cost_cents), 0) FROM deck_cards"
        " JOIN lots ON lots.id = deck_cards.lot_id WHERE deck_cards.deck_id = ?",
        (deck_id,),
    ).fetchone()[0]

    return {
        "deck": deck,
        "groups": groups,
        "list_count": sum(g["qty"] for g in groups),
        "equipped_count": sum(g["qty"] for g in groups if g["equipped"]),
        "list_cost_cents": list_cost,
        "missing": reassemble(conn, deck_id),
        "deck_names": {row["id"]: row["name"] for row in conn.execute("SELECT id, name FROM decks")},
    }


def search_cards(conn: sqlite3.Connection, query: str = "") -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT lots.card_name, lots.set_code, lots.collector_number, lots.finish,
               current.deck_id AS current_deck_id,
               decks.name AS current_deck_name,
               dispositions.disposition_type,
               COUNT(*) AS qty,
               SUM(lots.allocated_cost_cents) AS cost_cents
        FROM lots
        LEFT JOIN deck_cards AS current ON current.lot_id = lots.id AND current.equipped = 1
        LEFT JOIN decks ON decks.id = current.deck_id
        LEFT JOIN dispositions ON dispositions.lot_id = lots.id
        WHERE lots.card_name LIKE ? ESCAPE '\\'
        GROUP BY lots.card_name, lots.set_code, lots.collector_number, lots.finish,
                 current.deck_id, dispositions.disposition_type
        ORDER BY lots.card_name COLLATE NOCASE
        """,
        ("%" + _escape_like(query.strip()) + "%",),
    ).fetchall()


def needs_input(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Non-basic lots with no resolved printing (ambiguous or not found
    during precon seeding) -- the user has to check the physical card."""
    return conn.execute(
        f"""
        SELECT lots.card_name, lots.finish, COUNT(*) AS qty,
               products.display_name AS product_name
        FROM lots
        LEFT JOIN products ON products.id = lots.product_id
        WHERE lots.scryfall_id IS NULL AND lots.card_name NOT IN ({_BASICS_SQL})
        GROUP BY lots.card_name, lots.finish, lots.product_id
        ORDER BY lots.card_name COLLATE NOCASE
        """,
        BASIC_LAND_NAMES,
    ).fetchall()


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
