"""Choosing a deck's commander.

Any legendary creature on the deck's list can be its commander (decks.commander,
stored by card name). Eligibility comes from the card's cached Scryfall type
line -- the front face for double-faced cards -- so a card with no cached
printing can't be verified and isn't offered. Only Commander-format decks
have a commander. One commander per deck; partner pairs aren't modelled.
"""

from __future__ import annotations

import sqlite3

from mtgtracker import cardinfo


class CommanderError(ValueError):
    """The requested commander isn't allowed for this deck."""


def is_legendary_creature(info: cardinfo.CardInfo) -> bool:
    if not info.type_line:
        return False
    front = info.type_line.split("//")[0]
    return "Legendary" in front and "Creature" in front


def eligible(conn: sqlite3.Connection, deck_id: int) -> list[dict]:
    """Legendary creatures on deck_id's list, highest mana value first. Each
    is {"card_name", "art"} with art from the lot's printing if pinned."""
    rows = conn.execute(
        """
        SELECT lots.card_name, MIN(lots.scryfall_id) AS scryfall_id, MIN(lots.finish) AS finish
        FROM deck_cards
        JOIN lots ON lots.id = deck_cards.lot_id
        WHERE deck_cards.deck_id = ?
        GROUP BY lots.card_name
        """,
        (deck_id,),
    ).fetchall()
    result = []
    for row in rows:
        art = cardinfo.lookup(row["scryfall_id"], row["card_name"], row["finish"])
        if is_legendary_creature(art):
            result.append({"card_name": row["card_name"], "art": art})
    return sorted(result, key=lambda c: cardinfo.mana_value_order(c["card_name"], c["art"]))


def set_commander(
    conn: sqlite3.Connection, deck_id: int, card_name: str | None,
    *, commit: bool = True,
) -> None:
    """Make card_name deck_id's commander, or clear it with None."""
    deck = conn.execute("SELECT format FROM decks WHERE id = ?", (deck_id,)).fetchone()
    if deck is None:
        raise CommanderError(f"no deck with id {deck_id}")
    if card_name is not None:
        if deck["format"] != "Commander":
            raise CommanderError(f"{deck['format']} decks don't have a commander")
        if card_name not in {c["card_name"] for c in eligible(conn, deck_id)}:
            raise CommanderError(f"{card_name} isn't a legendary creature on this deck's list")
    conn.execute("UPDATE decks SET commander = ? WHERE id = ?", (card_name, deck_id))
    if commit:
        conn.commit()
