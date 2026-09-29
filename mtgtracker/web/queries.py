"""Read-only SQL for the web UI.

Collection-wide totals always sum `transactions`/`lots` once. They never sum
across decks: deck_cards is many-to-many, so a borrowed card sits on more than
one deck's list and per-deck sums would double-count it.
"""

from __future__ import annotations

import sqlite3

from mtgtracker import cardinfo
from mtgtracker.decks import reassemble
from mtgtracker.printings import BASIC_LAND_NAMES
from mtgtracker.web.breakdown import deck_breakdown
from mtgtracker.web.filters import CardFilters

COMMANDER_SECTION = "Commander"
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


def get_deck(
    conn: sqlite3.Connection, deck_id: int, filters: CardFilters | None = None
) -> dict | None:
    """The deck, its display groups, and `sections` for the grid/list views.
    `filters` narrows and orders only what's shown (sections); `groups`,
    counts and the breakdown always cover the whole deck."""
    filters = filters or CardFilters()
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
               MIN(lots.id) AS lot_id,
               GROUP_CONCAT(lots.id) AS lot_ids,
               MIN(lots.scryfall_id) AS scryfall_id,
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
    # Commander first, then the chosen sort (highest mana value by default);
    # both views split into type sections.
    commander = deck["commander"]
    groups = sorted(
        _with_card_info(groups, conn),
        key=lambda g: (g["card_name"] != commander, cardinfo.mana_value_order(g["card_name"], g["art"])),
    )
    shown = sorted(
        (g for g in groups if filters.matches(g)),
        key=lambda g: (g["card_name"] != commander, filters.sort_key(g)),
    )

    sections: dict[str, list[dict]] = {}
    for group in shown:
        label = COMMANDER_SECTION if group["card_name"] == commander else group["category"]
        sections.setdefault(label, []).append(group)

    list_cost = conn.execute(
        "SELECT COALESCE(SUM(lots.allocated_cost_cents), 0) FROM deck_cards"
        " JOIN lots ON lots.id = deck_cards.lot_id WHERE deck_cards.deck_id = ?",
        (deck_id,),
    ).fetchone()[0]

    return {
        "deck": deck,
        "groups": groups,
        "sections": [
            (label, sections[label], sum(g["qty"] for g in sections[label]))
            for label in [COMMANDER_SECTION, *cardinfo.SECTION_ORDER]
            if label in sections
        ],
        "shown_count": sum(g["qty"] for g in shown),
        "facets": filters.facet_counts(groups),
        "breakdown": deck_breakdown(groups),
        "list_count": sum(g["qty"] for g in groups),
        "equipped_count": sum(g["qty"] for g in groups if g["equipped"]),
        "list_cost_cents": list_cost,
        "missing": reassemble(conn, deck_id),
        "deck_names": {row["id"]: row["name"] for row in conn.execute("SELECT id, name FROM decks")},
    }


def count_cards(conn: sqlite3.Connection) -> int:
    """Every lot, disposed or not -- the cards page's unfiltered total."""
    return conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0]


def search_cards(conn: sqlite3.Connection, query: str = "") -> list[dict]:
    rows = conn.execute(
        """
        SELECT lots.card_name, lots.set_code, lots.collector_number, lots.finish,
               current.deck_id AS current_deck_id,
               decks.name AS current_deck_name,
               dispositions.disposition_type,
               COUNT(*) AS qty,
               MIN(lots.id) AS lot_id,
               GROUP_CONCAT(lots.id) AS lot_ids,
               MIN(lots.scryfall_id) AS scryfall_id,
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
    return sorted(
        _with_card_info(rows, conn), key=lambda c: cardinfo.mana_value_order(c["card_name"], c["art"])
    )


def lots_as_rows(conn: sqlite3.Connection, lot_ids: list[int], *, where: str) -> list[dict]:
    """These lots grouped by card name as display rows (like search_cards,
    ordered by mana value), for showing a past deck version. `where` is the
    location text the details panel shows."""
    if not lot_ids:
        return []
    marks = ",".join("?" * len(lot_ids))
    rows = conn.execute(
        f"""
        SELECT card_name, COUNT(*) AS qty, MIN(scryfall_id) AS scryfall_id, MIN(finish) AS finish,
               MIN(set_code) AS set_code, MIN(collector_number) AS collector_number,
               MIN(id) AS lot_id, GROUP_CONCAT(id) AS lot_ids,
               SUM(allocated_cost_cents) AS cost_cents
        FROM lots WHERE id IN ({marks})
        GROUP BY card_name
        """,
        tuple(lot_ids),
    ).fetchall()
    result = []
    for row in _with_card_info(rows, conn):
        row.update(where=where, disposition_type=None, current_deck_id=None, current_deck_name=None)
        result.append(row)
    return sorted(result, key=lambda r: cardinfo.mana_value_order(r["card_name"], r["art"]))


def collapse_by_name(rows: list[dict]) -> list[dict]:
    """Merge rows for the same card into one, whatever deck or printing each
    copy is in (2x Sol Ring, one per deck). Quantities and cost add up;
    `where`, `printing` and `finish` summarize the copies for the hover panel.
    `lot_id` points at an unresolved copy when there is one, so "needs
    printing" links to a lot that actually needs it. A merged card only counts
    as disposed if every copy is."""
    by_name: dict[str, list[dict]] = {}
    for row in rows:
        by_name.setdefault(row["card_name"], []).append(row)

    merged = []
    for copies in by_name.values():
        unresolved = [c for c in copies if c["scryfall_id"] is None]
        pinned = [c for c in copies if c["scryfall_id"] is not None]
        printings = {(c["set_code"], c["collector_number"]) for c in pinned}
        finishes: dict[str, int] = {}
        places: dict[str, int] = {}
        for c in copies:
            finishes[c["finish"]] = finishes.get(c["finish"], 0) + c["qty"]
            place = (c["disposition_type"] or "").capitalize() or c["current_deck_name"] or "Not in any deck"
            places[place] = places.get(place, 0) + c["qty"]

        dispositions = {c["disposition_type"] for c in copies}
        base = (pinned or copies)[0]
        printing_text = ", ".join(f"{s.upper()} #{n}" for s, n in sorted(printings))
        if unresolved and printing_text:
            printing_text += f" + {sum(c['qty'] for c in unresolved)} not picked"
        merged.append({
            **base,
            "qty": sum(c["qty"] for c in copies),
            "cost_cents": sum(c["cost_cents"] or 0 for c in copies),
            "lot_id": min(c["lot_id"] for c in (unresolved or copies)),
            # A single shared printing keeps set/number; mixed or unpicked -> None.
            "set_code": base["set_code"] if len(printings) == 1 and not unresolved else None,
            "collector_number": base["collector_number"] if len(printings) == 1 and not unresolved else None,
            "printing": printing_text or None,
            "finish": next(iter(finishes)) if len(finishes) == 1
                      else ", ".join(f"{n} {f}" for f, n in sorted(finishes.items())),
            "decks": _merge_decks(copies),
            "disposition_type": base["disposition_type"] if len(dispositions) == 1 else None,
            "current_deck_id": base["current_deck_id"] if len(places) == 1 else None,
            "where": next(iter(places)) if len(places) == 1 else ", ".join(
                place if n == 1 else f"{place} ({n})" for place, n in sorted(places.items())
            ),
        })
    return merged


def sections_by_type(rows: list[dict]) -> list[tuple[str, list[dict], int]]:
    """(label, rows, card count) per card-type section, in deck-list order.
    Rows keep their existing order within a section."""
    sections: dict[str, list[dict]] = {}
    for row in rows:
        sections.setdefault(row["category"], []).append(row)
    return [
        (label, sections[label], sum(r["qty"] for r in sections[label]))
        for label in cardinfo.SECTION_ORDER
        if label in sections
    ]


def _with_card_info(rows: list[sqlite3.Row], conn: sqlite3.Connection) -> list[dict]:
    """Rows as dicts plus `art` (image/type from the Scryfall cache, never
    the network), `category` (deck-list section) and `decks` (see
    _deck_memberships)."""
    memberships = _deck_memberships(conn)
    result = []
    for row in rows:
        item = dict(row)
        art = cardinfo.lookup(item["scryfall_id"], item["card_name"], item["finish"])
        item["art"] = art
        item["category"] = art.category(item["card_name"])
        lot_ids = [int(i) for i in (item.pop("lot_ids") or "").split(",") if i]
        item["decks"] = _decks_for(lot_ids, memberships)
        result.append(item)
    return result


def _deck_memberships(conn: sqlite3.Connection) -> dict[int, list[tuple[int, str, bool]]]:
    """lot_id -> [(deck_id, deck name, equipped there)] for every deck list
    the lot is on. One query for the whole (small, single-user) collection."""
    memberships: dict[int, list[tuple[int, str, bool]]] = {}
    for row in conn.execute(
        "SELECT deck_cards.lot_id, decks.id, decks.name, deck_cards.equipped"
        " FROM deck_cards JOIN decks ON decks.id = deck_cards.deck_id"
    ):
        memberships.setdefault(row[0], []).append((row[1], row[2], bool(row[3])))
    return memberships


def _decks_for(lot_ids: list[int], memberships: dict) -> list[dict]:
    """Decks whose lists include any of these lots: how many copies each list
    has (`count`) and whether the deck physically holds any of them now
    (`current`). Sorted by deck name."""
    decks: dict[int, dict] = {}
    for lot_id in lot_ids:
        for deck_id, name, equipped in memberships.get(lot_id, ()):
            deck = decks.setdefault(deck_id, {"id": deck_id, "name": name, "count": 0, "current": False})
            deck["count"] += 1
            deck["current"] = deck["current"] or equipped
    return sorted(decks.values(), key=lambda d: d["name"].casefold())


def _merge_decks(copies: list[dict]) -> list[dict]:
    merged: dict[int, dict] = {}
    for copy in copies:
        for deck in copy["decks"]:
            into = merged.setdefault(deck["id"], {**deck, "count": 0, "current": False})
            into["count"] += deck["count"]
            into["current"] = into["current"] or deck["current"]
    return sorted(merged.values(), key=lambda d: d["name"].casefold())


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
