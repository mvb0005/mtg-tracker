"""Read-only SQL for the web UI.

Collection-wide totals always sum `transactions`/`lots` once. They never sum
across decks: deck_cards is many-to-many, so a borrowed card sits on more than
one deck's list and per-deck sums would double-count it.
"""

from __future__ import annotations

import sqlite3
from collections import Counter

from mtgtracker import cardinfo, scryfall
from mtgtracker.decks import reassemble
from mtgtracker.printings import BASIC_LAND_NAMES
from mtgtracker.web.breakdown import deck_breakdown
from mtgtracker.web.filters import CardFilters

COMMANDER_SECTION = "Commander"
_BASICS_SQL = ", ".join("?" for _ in BASIC_LAND_NAMES)


def dashboard_stats(conn: sqlite3.Connection, owner_id: int) -> dict:
    """One user's totals: their purchases, the cards they own, their decks."""
    spend = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(total_cents), 0) AS total FROM transactions WHERE owner_id = ?",
        (owner_id,),
    ).fetchone()
    cards = conn.execute(
        "SELECT COUNT(*) FROM lots WHERE owner_id = ?"
        " AND NOT EXISTS (SELECT 1 FROM dispositions WHERE dispositions.lot_id = lots.id)",
        (owner_id,),
    ).fetchone()[0]
    decks = conn.execute("SELECT COUNT(*) FROM decks WHERE owner_id = ?", (owner_id,)).fetchone()[0]
    return {
        "transaction_count": spend["n"],
        "total_spent_cents": spend["total"],
        "card_count": cards,
        "deck_count": decks,
        "needs_input_count": sum(row["qty"] for row in needs_input(conn, owner_id)),
    }


def list_transactions(conn: sqlite3.Connection, owner_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT transactions.*,
               (SELECT COUNT(*) FROM products WHERE products.transaction_id = transactions.id)
                   AS product_count
        FROM transactions
        WHERE owner_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        (owner_id,),
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


def list_decks(conn: sqlite3.Connection, owner_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT decks.*,
               COUNT(deck_cards.lot_id) AS owned_count,
               COALESCE(SUM(deck_cards.equipped), 0) AS equipped_count,
               (SELECT COUNT(*) FROM deck_wants WHERE deck_wants.deck_id = decks.id) AS wanted_count
        FROM decks
        LEFT JOIN deck_cards ON deck_cards.deck_id = decks.id
        WHERE decks.owner_id = ?
        GROUP BY decks.id
        ORDER BY decks.id
        """,
        (owner_id,),
    ).fetchall()
    identities = deck_identities(conn)
    return [
        {**dict(row), "identity": identities.get(row["id"], ()),
         "list_count": row["owned_count"] + row["wanted_count"],
         "elsewhere_count": row["owned_count"] - row["equipped_count"]}
        for row in rows
    ]


def deck_identities(conn: sqlite3.Connection) -> dict[int, tuple[str, ...]]:
    """deck_id -> its colors (cardinfo.deck_identity), for every deck, from
    one query over all deck lists."""
    rows = conn.execute(
        """
        SELECT deck_cards.deck_id, lots.card_name, MIN(lots.scryfall_id) AS scryfall_id,
               MIN(lots.finish) AS finish, decks.commander
        FROM deck_cards
        JOIN lots ON lots.id = deck_cards.lot_id
        JOIN decks ON decks.id = deck_cards.deck_id
        GROUP BY deck_cards.deck_id, lots.card_name
        UNION ALL
        SELECT deck_wants.deck_id, deck_wants.card_name, MIN(deck_wants.scryfall_id),
               'nonfoil', decks.commander
        FROM deck_wants
        JOIN decks ON decks.id = deck_wants.deck_id
        GROUP BY deck_wants.deck_id, deck_wants.card_name
        """
    ).fetchall()
    per_deck: dict[int, list[dict]] = {}
    commanders: dict[int, str | None] = {}
    for row in rows:
        art = cardinfo.lookup(row["scryfall_id"], row["card_name"], row["finish"])
        per_deck.setdefault(row["deck_id"], []).append(
            {"card_name": row["card_name"], "art": art, "category": art.category(row["card_name"])}
        )
        commanders[row["deck_id"]] = row["commander"]
    return {
        deck_id: cardinfo.deck_identity(cards, commanders[deck_id]) for deck_id, cards in per_deck.items()
    }


def get_deck(
    conn: sqlite3.Connection, deck_id: int, filters: CardFilters | None = None,
    *, viewer_id: int | None = None,
) -> dict | None:
    """The deck, its display groups, and `sections` for the grid/list views.
    `filters` narrows and orders only what's shown (sections); `groups`,
    counts and the breakdown always cover the whole deck. Money is private:
    `viewer_id` sees cost only on lots they own, and the deck's cost basis
    only on their own deck (None otherwise)."""
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
               SUM(lots.allocated_cost_cents) AS cost_cents,
               lots.owner_id AS lot_owner_id
        FROM deck_cards
        JOIN lots ON lots.id = deck_cards.lot_id
        LEFT JOIN deck_cards AS current
               ON current.lot_id = lots.id AND current.equipped = 1
        LEFT JOIN decks AS current_deck ON current_deck.id = current.deck_id
        WHERE deck_cards.deck_id = ?
        GROUP BY lots.card_name, lots.set_code, lots.collector_number, lots.finish,
                 deck_cards.equipped, current.deck_id, lots.owner_id
        ORDER BY lots.card_name COLLATE NOCASE
        """,
        (*BASIC_LAND_NAMES, deck_id),
    ).fetchall()
    owned = [
        {**g, "wanted": False, "mine": g["lot_owner_id"] == viewer_id,
         "cost_cents": g["cost_cents"] if g["lot_owner_id"] == viewer_id else None}
        for g in _with_card_info(groups, conn)
    ]
    wanted = _wanted_rows(conn.execute(
        "SELECT card_name, scryfall_id, COUNT(*) AS qty FROM deck_wants WHERE deck_id = ?"
        " GROUP BY card_name, scryfall_id",
        (deck_id,),
    ))
    # Who else owns each card (never the deck's owner or the viewer).
    friend_copies = FriendCopies(conn, {deck["owner_id"], viewer_id} - {None})
    for g in owned + wanted:
        g["friends"] = friend_copies(g["card_name"])
    borrow_from, acquire = _borrow_lists(wanted)
    # Commander first, then the chosen sort (highest mana value by default);
    # both views split into type sections.
    commander = deck["commander"]
    groups = sorted(
        owned + wanted,
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
        " JOIN lots ON lots.id = deck_cards.lot_id WHERE deck_cards.deck_id = ? AND lots.owner_id = ?",
        (deck_id, viewer_id),
    ).fetchone()[0] if viewer_id == deck["owner_id"] else None

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
        "breakdown": deck_breakdown(groups, cardinfo.deck_identity(groups, commander)),
        "identity": cardinfo.deck_identity(groups, commander),
        "list_count": sum(g["qty"] for g in groups),
        "equipped_count": sum(g["qty"] for g in groups if g["equipped"]),
        "elsewhere_count": sum(g["qty"] for g in owned if not g["equipped"]),
        "wanted_count": sum(g["qty"] for g in wanted),
        "list_cost_cents": list_cost,
        "missing": reassemble(conn, deck_id),
        "pull_from": _pull_groups(reassemble(conn, deck_id), conn),
        "acquire": acquire,
        "borrow_from": borrow_from,
        "borrowable_count": sum(min(g["qty"], sum(f["qty"] for f in g["friends"])) for g in wanted),
        "deck_names": {row["id"]: row["name"] for row in conn.execute("SELECT id, name FROM decks")},
    }


def _name_keys(card_name: str) -> set[str]:
    """A card's normalized name and its front face's ("A // B" -> "a b", "a"),
    so a double-faced card matches however a list or a lot spells it."""
    return {scryfall._normalize(card_name), scryfall._normalize(card_name.split("//")[0])}


class FriendCopies:
    """Copies of each card that other people own (not disposed of), for
    showing what you could borrow and from whom. Friends see each other's
    collections, so this reveals nothing new; it never includes cost."""

    def __init__(self, conn: sqlite3.Connection, exclude: set[int]):
        self._by_key: dict[str, dict[tuple[int, str], dict]] = {}
        placeholders = ",".join("?" * len(exclude)) or "NULL"
        for row in conn.execute(
            f"""
            SELECT lots.card_name, users.id AS user_id, users.display_name,
                   current_deck.name AS deck_name, COUNT(*) AS qty
            FROM lots
            JOIN users ON users.id = lots.owner_id
            LEFT JOIN dispositions ON dispositions.lot_id = lots.id
            LEFT JOIN deck_cards AS current ON current.lot_id = lots.id AND current.equipped = 1
            LEFT JOIN decks AS current_deck ON current_deck.id = current.deck_id
            WHERE dispositions.id IS NULL AND lots.owner_id NOT IN ({placeholders})
            GROUP BY lots.card_name, users.id, current.deck_id
            """,
            tuple(exclude),
        ):
            for key in _name_keys(row["card_name"]):
                entry = self._by_key.setdefault(key, {}).setdefault(
                    (row["user_id"], row["card_name"]),
                    {"user_id": row["user_id"], "name": row["display_name"], "loose": 0, "decks": Counter()},
                )
                if row["deck_name"] is None:
                    entry["loose"] += row["qty"]
                else:
                    entry["decks"][row["deck_name"]] += row["qty"]

    def __call__(self, card_name: str) -> list[dict]:
        """[{id, name, qty, where}] per friend who owns card_name, most copies
        first. `where` says where their copies are ("in no deck", or in their
        decks)."""
        merged: dict[tuple[int, str], dict] = {}
        for key in _name_keys(card_name):
            merged.update(self._by_key.get(key, {}))
        friends: dict[int, dict] = {}
        for entry in merged.values():
            friend = friends.setdefault(
                entry["user_id"], {"loose": 0, "decks": Counter(), "name": entry["name"]}
            )
            friend["loose"] += entry["loose"]
            friend["decks"].update(entry["decks"])
        result = []
        for user_id, f in friends.items():
            places = ([f"{f['loose']} in no deck"] if f["loose"] else []) + [
                f"{n} in {deck}" for deck, n in sorted(f["decks"].items())
            ]
            qty = f["loose"] + sum(f["decks"].values())
            result.append({"id": user_id, "name": f["name"], "qty": qty, "where": ", ".join(places)})
        return sorted(result, key=lambda f: (-f["qty"], f["name"].casefold()))


def borrow_summary(friends: list[dict]) -> str:
    """Tooltip text: "Owned by 1 friend: Alex (3)"."""
    if not friends:
        return ""
    names = ", ".join(f"{f['name']} ({f['qty']})" for f in friends)
    return f"Owned by {len(friends)} friend{'s' if len(friends) != 1 else ''}: {names}"


def _borrow_lists(wanted: list[dict]) -> tuple[list[dict], list[tuple[str, int]]]:
    """Reassemble's to-acquire cards split into per-friend borrow checklists
    (a card several friends own is listed under each) and what nobody has."""
    by_friend: dict[int, dict] = {}
    nobody: list[tuple[str, int]] = []
    for g in wanted:
        if not g["friends"]:
            nobody.append((g["card_name"], g["qty"]))
        for f in g["friends"]:
            entry = by_friend.setdefault(f["id"], {"id": f["id"], "name": f["name"], "cards": []})
            entry["cards"].append((g["card_name"], min(g["qty"], f["qty"])))
    for entry in by_friend.values():
        entry["cards"].sort(key=lambda item: item[0].casefold())
        entry["total"] = sum(n for _, n in entry["cards"])
    return (sorted(by_friend.values(), key=lambda e: (-e["total"], e["name"].casefold())),
            sorted(nobody, key=lambda item: item[0].casefold()))


def _wanted_rows(rows) -> list[dict]:
    """deck_wants groups as display rows (same shape as owned groups) with
    wanted=True: not owned, so no lot, location, printing or cost."""
    result = []
    for row in rows:
        art = cardinfo.lookup(row["scryfall_id"], row["card_name"], "nonfoil")
        result.append({
            "card_name": row["card_name"], "scryfall_id": row["scryfall_id"], "qty": row["qty"],
            "set_code": None, "collector_number": None, "finish": "nonfoil", "needs_input": False,
            "equipped": 0, "current_deck_id": None, "current_deck_name": None, "disposition_type": None,
            "lot_id": None, "cost_cents": None, "decks": [], "art": art,
            "category": art.category(row["card_name"]), "wanted": True,
            "where": "Not owned yet: need to acquire", "printing": "Not owned yet",
        })
    return result


def _pull_groups(missing: list[dict], conn: sqlite3.Connection) -> list[dict]:
    """Reassemble rows grouped by where each card is now, as a physical
    checklist: [{deck_id, deck_name, cards: [(name, qty)], total}]."""
    names = {row[0]: row[1] for row in conn.execute("SELECT id, name FROM decks")}
    groups: dict[int | None, Counter] = {}
    for row in missing:
        groups.setdefault(row["current_deck_id"], Counter())[row["card_name"]] += 1
    return [
        {
            "deck_id": deck_id,
            "deck_name": names.get(deck_id, "Not in any deck") if deck_id is not None else "Not in any deck",
            "cards": sorted(counter.items(), key=lambda item: item[0].casefold()),
            "total": sum(counter.values()),
        }
        for deck_id, counter in sorted(groups.items(), key=lambda item: (item[0] is None, item[0] or 0))
    ]


def count_cards(conn: sqlite3.Connection, owner_id: int) -> int:
    """Every lot the user owns, disposed or not -- the cards page's unfiltered total."""
    return conn.execute("SELECT COUNT(*) FROM lots WHERE owner_id = ?", (owner_id,)).fetchone()[0]


def search_cards(conn: sqlite3.Connection, owner_id: int, query: str = "") -> list[dict]:
    """The user's own cards (never ones they've borrowed), one row per
    printing/location, matching `query` by name."""
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
        WHERE lots.owner_id = ? AND lots.card_name LIKE ? ESCAPE '\\'
        GROUP BY lots.card_name, lots.set_code, lots.collector_number, lots.finish,
                 current.deck_id, dispositions.disposition_type
        ORDER BY lots.card_name COLLATE NOCASE
        """,
        (owner_id, "%" + _escape_like(query.strip()) + "%"),
    ).fetchall()
    return sorted(
        _with_card_info(rows, conn), key=lambda c: cardinfo.mana_value_order(c["card_name"], c["art"])
    )


def lots_as_rows(
    conn: sqlite3.Connection, lot_ids: list[int], *, where: str,
    wants: list[tuple[str, str | None]] = (), viewer_id: int | None = None,
) -> list[dict]:
    """These lots grouped by card name as display rows (like search_cards,
    ordered by mana value), for showing a past deck version. `where` is the
    location text the details panel shows."""
    wanted_counts = Counter(wants)
    wanted = _wanted_rows(
        {"card_name": name, "scryfall_id": sid, "qty": n} for (name, sid), n in wanted_counts.items()
    )
    if not lot_ids:
        return sorted(wanted, key=lambda r: cardinfo.mana_value_order(r["card_name"], r["art"]))
    marks = ",".join("?" * len(lot_ids))
    rows = conn.execute(
        f"""
        SELECT card_name, COUNT(*) AS qty, MIN(scryfall_id) AS scryfall_id, MIN(finish) AS finish,
               MIN(set_code) AS set_code, MIN(collector_number) AS collector_number,
               MIN(id) AS lot_id, GROUP_CONCAT(id) AS lot_ids,
               SUM(CASE WHEN owner_id = ? THEN allocated_cost_cents END) AS cost_cents
        FROM lots WHERE id IN ({marks})
        GROUP BY card_name
        """,
        (viewer_id, *lot_ids),
    ).fetchall()
    result = []
    for row in _with_card_info(rows, conn):
        row.update(where=where, disposition_type=None, current_deck_id=None, current_deck_name=None)
        result.append(row)
    return sorted(result + wanted, key=lambda r: cardinfo.mana_value_order(r["card_name"], r["art"]))


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
            # None when no copy has a cost you may see (someone else's cards).
            "cost_cents": None if all(c["cost_cents"] is None for c in copies)
            else sum(c["cost_cents"] or 0 for c in copies),
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


def needs_input(conn: sqlite3.Connection, owner_id: int) -> list[sqlite3.Row]:
    """The user's non-basic lots with no resolved printing (ambiguous or not
    found during precon seeding) -- they have to check the physical card."""
    return conn.execute(
        f"""
        SELECT lots.card_name, lots.finish, COUNT(*) AS qty,
               products.display_name AS product_name
        FROM lots
        LEFT JOIN products ON products.id = lots.product_id
        WHERE lots.owner_id = ? AND lots.scryfall_id IS NULL AND lots.card_name NOT IN ({_BASICS_SQL})
        GROUP BY lots.card_name, lots.finish, lots.product_id
        ORDER BY lots.card_name COLLATE NOCASE
        """,
        (owner_id, *BASIC_LAND_NAMES),
    ).fetchall()


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
