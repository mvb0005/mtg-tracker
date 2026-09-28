"""Resolve a precon's seed decklist (seed/precon_decklists/*.json) against
Scryfall, then create the deck, its lots, and their deck_cards rows.

Resolution is honest, not guessed: a card name that matches exactly one
printing in the deck's set is resolved; a name matching more than one
printing is left unresolved and reported as ambiguous (e.g. Sol Ring and
Arcane Signet each have two fdc printings -- an original-wave foil-only
version and a newer nonfoil/foil version -- and nothing in the decklist
text says which one shipped in this box); a name matching none is reported
as not-found. Basic lands are never resolved to a specific printing (a set
can have several land arts and there's no way to know which is in the box)
-- they're reported once per basic type, not once per copy.

Unresolved cards still get lots (card_name only, scryfall_id NULL) so nothing
blocks deck creation or cost allocation, which only need a card count.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from mtgtracker.allocation import allocate
from mtgtracker.decks import add_to_deck, create_deck
from mtgtracker.scryfall import fetch_set_cards, find_matches, index_by_name

SEED_DIR = Path(__file__).parent.parent.parent / "seed" / "precon_decklists"


@dataclass
class ResolutionReport:
    deck_name: str
    resolved: list[dict] = field(default_factory=list)
    ambiguous: list[dict] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    basics_unresolved: list[str] = field(default_factory=list)

    def has_issues(self) -> bool:
        return bool(self.ambiguous or self.not_found)


def load_seed_decklists() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(SEED_DIR.glob("*.json"))]


def resolve_decklist(seed: dict, *, use_cache: bool = True) -> ResolutionReport:
    set_cards = fetch_set_cards(seed["set_code"], use_cache=use_cache)
    name_index = index_by_name(set_cards)

    report = ResolutionReport(deck_name=seed["deck_name"])

    for name in seed["cards"]:
        matches = find_matches(name_index, name)
        is_commander = name == seed["commander"]

        if len(matches) == 1:
            card = matches[0]
            finish = "foil" if is_commander else "nonfoil"
            if is_commander and "foil" not in card.get("finishes", []):
                report.ambiguous.append(
                    {
                        "name": name,
                        "reason": "commander should be foil but this printing has no foil finish",
                        "candidates": matches,
                    }
                )
                continue
            report.resolved.append(
                {
                    "name": name,
                    "scryfall_id": card["id"],
                    "set_code": card["set"],
                    "collector_number": card["collector_number"],
                    "finish": finish,
                }
            )
        elif len(matches) == 0:
            report.not_found.append(name)
        else:
            report.ambiguous.append({"name": name, "candidates": matches})

    for basic_name in seed.get("basics", {}):
        report.basics_unresolved.append(basic_name)

    return report


def create_deck_from_seed(
    conn: sqlite3.Connection,
    seed: dict,
    report: ResolutionReport,
    *,
    product_id: int,
    transaction_id: int,
    acquired_date: str,
) -> int:
    """Creates the deck and one lot per physical card (resolved, ambiguous,
    not-found, and basics all get a lot -- only scryfall_id/set/collector
    differ), each equipped in the new deck. Returns the deck id.
    """
    deck_id = create_deck(conn, seed["deck_name"], acquired_date, format=seed["format"])

    def _add_lot(card_name: str, scryfall_id: str | None, set_code: str | None,
                 collector_number: str | None, finish: str) -> None:
        cur = conn.execute(
            """
            INSERT INTO lots (
                product_id, transaction_id, scryfall_id, card_name, set_code,
                collector_number, finish, acquired_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (product_id, transaction_id, scryfall_id, card_name, set_code,
             collector_number, finish, acquired_date),
        )
        add_to_deck(conn, deck_id, cur.lastrowid, equipped=True)

    for entry in report.resolved:
        _add_lot(entry["name"], entry["scryfall_id"], entry["set_code"],
                  entry["collector_number"], entry["finish"])

    for entry in report.ambiguous:
        finish = "foil" if entry["name"] == seed["commander"] else "nonfoil"
        _add_lot(entry["name"], None, None, None, finish)

    for name in report.not_found:
        finish = "foil" if name == seed["commander"] else "nonfoil"
        _add_lot(name, None, None, None, finish)

    for basic_name, qty in seed.get("basics", {}).items():
        for _ in range(qty):
            _add_lot(basic_name, None, None, None, "nonfoil")

    conn.commit()
    allocate(conn, product_id)
    return deck_id


def seed_precon_deck_for_product(
    conn: sqlite3.Connection, product_id: int, *, use_cache: bool = True
) -> tuple[int, ResolutionReport] | None:
    """If this product matches a known precon seed decklist (by
    display_name), resolves it and creates the deck + lots + allocation.
    Returns (deck_id, report), or None if this product isn't a known precon.
    """
    product = conn.execute(
        "SELECT display_name, transaction_id FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    if product is None:
        raise ValueError(f"no product with id {product_id}")

    txn = conn.execute(
        "SELECT created_at FROM transactions WHERE id = ?", (product["transaction_id"],)
    ).fetchone()
    acquired_date = txn["created_at"][:10]

    for seed in load_seed_decklists():
        if seed["product_display_name"] == product["display_name"]:
            report = resolve_decklist(seed, use_cache=use_cache)
            conn.execute(
                "UPDATE products SET product_type = 'precon' WHERE id = ?", (product_id,)
            )
            deck_id = create_deck_from_seed(
                conn, seed, report,
                product_id=product_id,
                transaction_id=product["transaction_id"],
                acquired_date=acquired_date,
            )
            return deck_id, report

    return None
