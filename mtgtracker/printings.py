"""Pin unresolved lots to the exact Scryfall printing the user has in hand.

Precon seeding leaves a lot's scryfall_id NULL when the decklist can't say
which printing shipped (e.g. two fdc Sol Rings) and for every basic land.
This module lets the user settle that by looking at the physical card:

- Candidates come only from the local Scryfall cache (every set already
  fetched into scryfall.CACHE_DIR). Looking up another set is an explicit
  user action that goes through the cached scryfall.fetch_set_cards().
- Unresolved lots are handled in groups: same card name, product, recorded
  finish, and current deck. A group of 34 Islands can then be split across
  several arts without mixing up a borrowed copy that sits in another deck.
- assign() validates everything before writing: the printing's name must
  match, the finish must be one that printing actually comes in, and every
  lot must still be unresolved. Picking a printing can change a lot's
  finish (e.g. a foil-only promo), since the seeded "nonfoil" was a default,
  not an observation. Cost allocation is by count, so it is unaffected.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from mtgtracker import cardinfo, scryfall
from mtgtracker.cardinfo import BASIC_LAND_NAMES

FINISHES = ("nonfoil", "foil", "etched")  # lots.finish CHECK constraint


class PrintingError(ValueError):
    """An assignment that doesn't fit the group or the printing."""


@dataclass
class Group:
    lot_ids: list[int]
    card_name: str
    finish: str
    product_id: int | None
    product_name: str | None
    deck_id: int | None
    deck_name: str | None

    @property
    def qty(self) -> int:
        return len(self.lot_ids)

    @property
    def is_basic(self) -> bool:
        return self.card_name in BASIC_LAND_NAMES


# Unresolved lots with their current (equipped) deck, if any.
_UNRESOLVED_CTE = """
WITH unresolved AS (
    SELECT lots.id, lots.card_name, lots.finish, lots.product_id,
           current.deck_id AS deck_id
    FROM lots
    LEFT JOIN deck_cards AS current ON current.lot_id = lots.id AND current.equipped = 1
    WHERE lots.scryfall_id IS NULL
)
"""


def unresolved_groups(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """One row per group, non-basics first. `lot_id` is the group's lowest
    lot id, used to address the group."""
    return conn.execute(
        _UNRESOLVED_CTE
        + f"""
        SELECT MIN(u.id) AS lot_id, u.card_name, u.finish, COUNT(*) AS qty,
               u.card_name IN ({", ".join("?" for _ in BASIC_LAND_NAMES)}) AS is_basic,
               products.display_name AS product_name,
               u.deck_id, decks.name AS deck_name
        FROM unresolved AS u
        LEFT JOIN products ON products.id = u.product_id
        LEFT JOIN decks ON decks.id = u.deck_id
        GROUP BY u.card_name, u.finish, u.product_id, u.deck_id
        ORDER BY is_basic, u.card_name COLLATE NOCASE, decks.name
        """,
        BASIC_LAND_NAMES,
    ).fetchall()


def group_for_lot(conn: sqlite3.Connection, lot_id: int) -> Group | None:
    """The unresolved group containing lot_id, or None if that lot doesn't
    exist or is already resolved."""
    rows = conn.execute(
        _UNRESOLVED_CTE
        + """
        SELECT u.id, u.card_name, u.finish, u.product_id, u.deck_id,
               products.display_name AS product_name, decks.name AS deck_name
        FROM unresolved AS u
        JOIN unresolved AS anchor ON anchor.id = ?
        LEFT JOIN products ON products.id = u.product_id
        LEFT JOIN decks ON decks.id = u.deck_id
        WHERE u.card_name = anchor.card_name
          AND u.finish = anchor.finish
          AND u.product_id IS anchor.product_id
          AND u.deck_id IS anchor.deck_id
        ORDER BY u.id
        """,
        (lot_id,),
    ).fetchall()
    if not rows:
        return None
    first = rows[0]
    return Group(
        lot_ids=[row["id"] for row in rows],
        card_name=first["card_name"],
        finish=first["finish"],
        product_id=first["product_id"],
        product_name=first["product_name"],
        deck_id=first["deck_id"],
        deck_name=first["deck_name"],
    )


def _collector_sort_key(number: str) -> tuple[int, str]:
    digits = "".join(ch for ch in number if ch.isdigit())
    return (int(digits) if digits else 0, number)


def cached_set_codes() -> list[str]:
    return sorted(p.stem for p in scryfall.CACHE_DIR.glob("*.json"))


def find_printings(card_name: str) -> list[dict]:
    """Every paper printing of card_name across all cached sets, as trimmed
    dicts safe to render (missing Scryfall fields become None/empty)."""
    found: dict[str, dict] = {}
    for card in cardinfo.printings_named(card_name):
        found[card["id"]] = {
            "id": card["id"],
            "name": card["name"],
            "set": card["set"],
            "set_name": card.get("set_name"),
            "collector_number": card["collector_number"],
            "finishes": [f for f in card.get("finishes", []) if f in FINISHES],
            "promo_types": card.get("promo_types") or [],
            "artist": card.get("artist"),
            "rarity": card.get("rarity"),
            "released_at": card.get("released_at"),
            "full_art": bool(card.get("full_art")),
            "image": cardinfo.image_url(card),
        }
    return sorted(
        found.values(), key=lambda c: (c["set"], _collector_sort_key(c["collector_number"]))
    )


def assign(
    conn: sqlite3.Connection, lot_id: int, assignments: list[tuple[str, str, int]]
) -> int:
    """Pin lots in lot_id's group to printings.

    assignments is [(scryfall_id, finish, qty), ...]; qty lots are taken from
    the group in ascending lot id order. The total may be less than the group
    size (the rest stay unresolved) but not more. All-or-nothing: any invalid
    entry raises PrintingError before anything is written. Returns the number
    of lots assigned.
    """
    group = group_for_lot(conn, lot_id)
    if group is None:
        raise PrintingError(f"lot {lot_id} doesn't exist or is already resolved")

    assignments = [(sid, finish, qty) for sid, finish, qty in assignments if qty]
    if any(qty < 0 for _, _, qty in assignments):
        raise PrintingError("quantities can't be negative")
    total = sum(qty for _, _, qty in assignments)
    if total == 0:
        raise PrintingError("nothing selected")
    if total > group.qty:
        raise PrintingError(f"assigned {total} cards but only {group.qty} are unresolved here")

    candidates = {card["id"]: card for card in find_printings(group.card_name)}
    for scryfall_id, finish, _ in assignments:
        card = candidates.get(scryfall_id)
        if card is None:
            raise PrintingError(f"{scryfall_id} isn't a known printing of {group.card_name}")
        if finish not in card["finishes"]:
            raise PrintingError(
                f"{card['set'].upper()} #{card['collector_number']} doesn't come in {finish}"
            )

    lot_ids = iter(group.lot_ids)
    now = datetime.now(UTC).isoformat()
    for scryfall_id, finish, qty in assignments:
        card = candidates[scryfall_id]
        for _ in range(qty):
            conn.execute(
                "UPDATE lots SET scryfall_id = ?, set_code = ?, collector_number = ?, finish = ?"
                " WHERE id = ? AND scryfall_id IS NULL",
                (card["id"], card["set"], card["collector_number"], finish, next(lot_ids)),
            )
        conn.execute(
            """
            INSERT INTO scryfall_cards (
                scryfall_id, name, set_code, collector_number, finishes, last_synced_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (scryfall_id) DO UPDATE SET
                name = excluded.name, set_code = excluded.set_code,
                collector_number = excluded.collector_number,
                finishes = excluded.finishes, last_synced_at = excluded.last_synced_at
            """,
            (card["id"], card["name"], card["set"], card["collector_number"],
             json.dumps(card["finishes"]), now),
        )
    conn.commit()
    return total
