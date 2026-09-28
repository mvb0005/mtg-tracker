"""Deck membership and physical placement.

A (deck_id, lot_id) row in deck_cards means "this physical card is on this
deck's list." The `equipped` flag marks which one deck currently physically
holds it — enforced at the database level by a partial unique index on
deck_cards(lot_id) WHERE equipped = 1.

- add_to_deck: put a card on a deck's list, optionally equipping it there.
- borrow: move physical possession of a card to a different deck that
  already has it on its list (or add it to that deck's list first). The
  card stays on the original deck's list, just unequipped.
- cut: permanently remove a card from a deck's list. A future reassemble
  won't ask for it back.
- reassemble: for a deck, find every card on its list that isn't currently
  equipped there, and report where it currently is instead.
"""

from __future__ import annotations

import sqlite3


def create_deck(conn: sqlite3.Connection, name: str, created_at: str, format: str = "Commander", notes: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO decks (name, format, created_at, notes) VALUES (?, ?, ?, ?)",
        (name, format, created_at, notes),
    )
    conn.commit()
    return cur.lastrowid


def add_to_deck(conn: sqlite3.Connection, deck_id: int, lot_id: int, equipped: bool = False) -> None:
    """Put a card on a deck's list. If equipped, unequips it from wherever
    it currently is first (in the same transaction, so the partial unique
    index is never violated)."""
    if equipped:
        conn.execute("UPDATE deck_cards SET equipped = 0 WHERE lot_id = ?", (lot_id,))

    conn.execute(
        "INSERT INTO deck_cards (deck_id, lot_id, equipped) VALUES (?, ?, ?)"
        " ON CONFLICT (deck_id, lot_id) DO UPDATE SET equipped = excluded.equipped",
        (deck_id, lot_id, int(equipped)),
    )
    conn.commit()


def borrow(conn: sqlite3.Connection, lot_id: int, to_deck_id: int) -> None:
    """Move physical possession of a card to to_deck_id. Adds it to
    to_deck_id's list if it isn't already there. The card's original
    deck(s) keep their list entry, just unequipped."""
    add_to_deck(conn, to_deck_id, lot_id, equipped=True)


def cut(conn: sqlite3.Connection, deck_id: int, lot_id: int) -> None:
    """Permanently remove a card from a deck's list."""
    conn.execute(
        "DELETE FROM deck_cards WHERE deck_id = ? AND lot_id = ?", (deck_id, lot_id)
    )
    conn.commit()


def reassemble(conn: sqlite3.Connection, deck_id: int) -> list[dict]:
    """Cards on deck_id's list that are currently equipped elsewhere (or
    nowhere). Returns rows with lot_id, card_name, and current_deck_id
    (None if the card isn't currently equipped anywhere)."""
    rows = conn.execute(
        """
        SELECT lots.id AS lot_id, lots.card_name AS card_name,
               current.deck_id AS current_deck_id
        FROM deck_cards
        JOIN lots ON lots.id = deck_cards.lot_id
        LEFT JOIN deck_cards AS current
               ON current.lot_id = deck_cards.lot_id AND current.equipped = 1
        WHERE deck_cards.deck_id = ? AND deck_cards.equipped = 0
        ORDER BY lots.card_name
        """,
        (deck_id,),
    ).fetchall()
    return [dict(row) for row in rows]
