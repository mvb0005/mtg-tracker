"""Deck modifications and versions.

A deck's modification list (deck_changes) holds cards to insert, each
optionally paired with a card on the list to replace. An insert is either
*owned* -- a specific lot from the collection, possibly one listed in another
deck (applying it borrows that copy, so that deck's Reassemble will ask for
it back) -- or *planned*: a card you don't own yet, identified by name and
Scryfall id, which stays pending until an owned copy is linked to it.

apply_changes() turns every ready (owned) change into the next deck version,
all or nothing: the whole batch is validated first, then written in one
transaction (the decks.* helpers each commit, so they aren't used here).
Replaced cards are cut from the list and go back to the collection (still
owned, cost basis unchanged). Cutting the last copy of the commander clears
decks.commander. Planned changes stay pending.

Versions snapshot the whole list (deck_version_cards), so any version's list
can be shown and each version's diff is computed against the one before.
"""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from mtgtracker import scryfall
from mtgtracker.decks import record_version


class ChangeError(ValueError):
    """A modification that doesn't fit the deck or the collection."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _deck(conn: sqlite3.Connection, deck_id: int) -> sqlite3.Row:
    deck = conn.execute("SELECT * FROM decks WHERE id = ?", (deck_id,)).fetchone()
    if deck is None:
        raise ChangeError(f"no deck with id {deck_id}")
    return deck


def _on_list(conn: sqlite3.Connection, deck_id: int, lot_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM deck_cards WHERE deck_id = ? AND lot_id = ?", (deck_id, lot_id)
    ).fetchone() is not None


def _pending_lots(
    conn: sqlite3.Connection, deck_id: int, column: str, *, exclude: int | None = None
) -> set[int]:
    assert column in ("add_lot_id", "cut_lot_id")
    rows = conn.execute(
        f"SELECT {column} FROM deck_changes WHERE deck_id = ? AND applied_version IS NULL"
        f" AND {column} IS NOT NULL AND id IS NOT ?",
        (deck_id, exclude),
    ).fetchall()
    return {row[0] for row in rows}


def _check_insertable(
    conn: sqlite3.Connection, deck_id: int, lot_id: int, *, change_id: int | None = None
) -> sqlite3.Row:
    lot = conn.execute(
        "SELECT lots.*, dispositions.id AS disposed FROM lots"
        " LEFT JOIN dispositions ON dispositions.lot_id = lots.id WHERE lots.id = ?",
        (lot_id,),
    ).fetchone()
    if lot is None:
        raise ChangeError(f"no card with id {lot_id}")
    if lot["disposed"] is not None:
        raise ChangeError(f"{lot['card_name']} has been disposed of")
    if _on_list(conn, deck_id, lot_id):
        raise ChangeError(f"that copy of {lot['card_name']} is already on this deck's list")
    if lot_id in _pending_lots(conn, deck_id, "add_lot_id", exclude=change_id):
        raise ChangeError(f"that copy of {lot['card_name']} is already in the change list")
    return lot


def _change(conn: sqlite3.Connection, deck_id: int, change_id: int) -> sqlite3.Row:
    change = conn.execute(
        "SELECT * FROM deck_changes WHERE id = ? AND deck_id = ? AND applied_version IS NULL",
        (change_id, deck_id),
    ).fetchone()
    if change is None:
        raise ChangeError("no such pending change for this deck")
    return change


# --- editing the change list ------------------------------------------------

def add_owned(conn: sqlite3.Connection, deck_id: int, lot_id: int) -> int:
    """Queue inserting this owned copy into the deck. Returns the change id."""
    _deck(conn, deck_id)
    lot = _check_insertable(conn, deck_id, lot_id)
    cur = conn.execute(
        "INSERT INTO deck_changes (deck_id, add_card_name, add_lot_id, add_scryfall_id, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (deck_id, lot["card_name"], lot_id, lot["scryfall_id"], _now()),
    )
    conn.commit()
    return cur.lastrowid


def add_planned(conn: sqlite3.Connection, deck_id: int, name: str) -> int:
    """Queue a card you don't own yet, looked up by name on Scryfall (an
    explicit network call, cached). Raises ChangeError if no single card
    matches."""
    _deck(conn, deck_id)
    if not name.strip():
        raise ChangeError("enter a card name")
    try:
        card = scryfall.fetch_named_card(name.strip())
    except scryfall.CardLookupError as exc:
        raise ChangeError(str(exc)) from exc
    cur = conn.execute(
        "INSERT INTO deck_changes (deck_id, add_card_name, add_scryfall_id, created_at) VALUES (?, ?, ?, ?)",
        (deck_id, card["name"], card["id"], _now()),
    )
    conn.commit()
    return cur.lastrowid


def link_owned(conn: sqlite3.Connection, deck_id: int, change_id: int, lot_id: int) -> None:
    """Turn a planned change into an owned one with a copy of the same card."""
    change = _change(conn, deck_id, change_id)
    lot = _check_insertable(conn, deck_id, lot_id, change_id=change_id)
    if scryfall._normalize(lot["card_name"]) != scryfall._normalize(change["add_card_name"]):
        raise ChangeError(f"that copy is {lot['card_name']}, not {change['add_card_name']}")
    conn.execute("UPDATE deck_changes SET add_lot_id = ? WHERE id = ?", (lot_id, change_id))
    conn.commit()


def set_cut(conn: sqlite3.Connection, deck_id: int, change_id: int, cut_lot_id: int | None) -> None:
    """Pick (or clear, with None) the card on the list this insert replaces."""
    _change(conn, deck_id, change_id)
    if cut_lot_id is not None:
        if not _on_list(conn, deck_id, cut_lot_id):
            raise ChangeError("that card isn't on this deck's list")
        if cut_lot_id in _pending_lots(conn, deck_id, "cut_lot_id", exclude=change_id):
            raise ChangeError("that copy is already being replaced by another change")
    conn.execute("UPDATE deck_changes SET cut_lot_id = ? WHERE id = ?", (cut_lot_id, change_id))
    conn.commit()


def remove_change(conn: sqlite3.Connection, deck_id: int, change_id: int) -> None:
    _change(conn, deck_id, change_id)
    conn.execute("DELETE FROM deck_changes WHERE id = ?", (change_id,))
    conn.commit()


# --- importing a list ---------------------------------------------------------

@dataclass
class ImportSummary:
    owned: list[str] = field(default_factory=list)  # queued with a copy you own
    planned: list[str] = field(default_factory=list)  # queued as "to buy"
    in_deck: list[str] = field(default_factory=list)  # already on the deck's list
    queued: list[str] = field(default_factory=list)  # already in the change list
    not_found: list[str] = field(default_factory=list)  # Scryfall has no such card
    skipped_lines: int = 0  # sideboard / maybeboard lines
    unreadable: list[str] = field(default_factory=list)

    def message(self) -> str:
        parts = [f"Queued {len(self.owned) + len(self.planned)}: {len(self.owned)} you own, "
                 f"{len(self.planned)} to buy."]
        for label, names in (("Already in the deck", self.in_deck), ("Already queued", self.queued),
                             ("Not found on Scryfall", self.not_found), ("Couldn't read", self.unreadable)):
            if names:
                parts.append(f"{label} ({len(names)}): {', '.join(names)}.")
        if self.skipped_lines:
            parts.append(f"Skipped {self.skipped_lines} sideboard/maybeboard line(s).")
        return " ".join(parts)


def _name_keys(card: dict) -> set[str]:
    """Normalized names a Scryfall card answers to (full name and faces)."""
    keys = {scryfall._normalize(card["name"])}
    keys |= {scryfall._normalize(face["name"]) for face in card.get("card_faces") or []}
    return keys


def _lot_keys(card_name: str) -> set[str]:
    return {scryfall._normalize(card_name), scryfall._normalize(card_name.split("//")[0])}


def import_list(conn: sqlite3.Connection, deck_id: int, text: str) -> ImportSummary:
    """Queue every card in a plain-text decklist (see
    importers/decklist_text.py) as inserts on this deck's change list.

    Each name is resolved on Scryfall (batched; an explicit network call).
    Copies the deck already has, or that are already queued, are skipped --
    so re-importing the same list is harmless. For the rest, free copies you
    own are used first (unassigned ones before ones in other decks, which
    would be borrowed); anything left is queued as planned ("to buy").
    Written in one transaction. Raises ChangeError if nothing is readable."""
    from mtgtracker.importers.decklist_text import parse

    _deck(conn, deck_id)
    parsed = parse(text)
    summary = ImportSummary(skipped_lines=len(parsed.skipped), unreadable=parsed.errors)
    if not parsed.entries:
        raise ChangeError("no cards found in that list")

    idents = [
        {"set": e.set_code, "collector_number": e.collector_number} if e.set_code and e.collector_number
        else {"name": e.name}
        for e in parsed.entries
    ]
    cards = scryfall.fetch_collection(idents)
    # A few misses get a fuzzy retry (typos, front-face-only names).
    for i, (entry, card) in enumerate(zip(parsed.entries, cards, strict=True)):
        if card is None and sum(c is None for c in cards[:i]) < 10:
            with contextlib.suppress(scryfall.CardLookupError):
                cards[i] = scryfall.fetch_named_card(entry.name)

    in_deck: dict[str, int] = {}
    for (name,) in conn.execute(
        "SELECT lots.card_name FROM deck_cards JOIN lots ON lots.id = deck_cards.lot_id"
        " WHERE deck_cards.deck_id = ?", (deck_id,)
    ):
        for key in _lot_keys(name):
            in_deck[key] = in_deck.get(key, 0) + 1
    queued: dict[str, int] = {}
    for change in pending_changes(conn, deck_id):
        for key in _lot_keys(change["add_card_name"]):
            queued[key] = queued.get(key, 0) + 1

    taken = _pending_lots(conn, deck_id, "add_lot_id")
    free: dict[str, list[int]] = {}
    for row in conn.execute(
        """
        SELECT lots.id, lots.card_name, holder.deck_id AS holder_id
        FROM lots
        LEFT JOIN dispositions ON dispositions.lot_id = lots.id
        LEFT JOIN deck_cards AS holder ON holder.lot_id = lots.id AND holder.equipped = 1
        WHERE dispositions.id IS NULL
          AND NOT EXISTS (SELECT 1 FROM deck_cards WHERE deck_id = ? AND lot_id = lots.id)
        ORDER BY holder.deck_id IS NOT NULL, lots.id
        """,
        (deck_id,),
    ):
        if row["id"] in taken:
            continue
        for key in _lot_keys(row["card_name"]):
            free.setdefault(key, []).append(row["id"])

    inserts: list[tuple[str, int | None, str]] = []  # (name, lot_id or None, scryfall id)
    used: set[int] = set()
    for entry, card in zip(parsed.entries, cards, strict=True):
        if card is None:
            summary.not_found.append(entry.name)
            continue
        keys = _name_keys(card)
        have = max((in_deck.get(k, 0) for k in keys), default=0)
        pending = max((queued.get(k, 0) for k in keys), default=0)
        wanted = entry.qty - have - pending
        if wanted <= 0:
            (summary.in_deck if have else summary.queued).append(card["name"])
            continue
        for key in keys:  # count this entry, so a repeated line isn't queued twice
            queued[key] = queued.get(key, 0) + wanted
        pool = next((free[k] for k in keys if k in free), [])
        for _ in range(wanted):
            lot_id = next((lot for lot in pool if lot not in used), None)
            if lot_id is not None:
                used.add(lot_id)
                summary.owned.append(card["name"])
            else:
                summary.planned.append(card["name"])
            inserts.append((card["name"], lot_id, card["id"]))

    now = _now()
    conn.executemany(
        "INSERT INTO deck_changes (deck_id, add_card_name, add_lot_id, add_scryfall_id, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        [(deck_id, name, lot_id, scryfall_id, now) for name, lot_id, scryfall_id in inserts],
    )
    conn.commit()
    return summary


# --- reading ------------------------------------------------------------------

def pending_changes(conn: sqlite3.Connection, deck_id: int) -> list[dict]:
    """Pending changes, oldest first, with where the inserted copy is now and
    what it replaces. `ready` is True for owned inserts."""
    rows = conn.execute(
        """
        SELECT deck_changes.*,
               add_lot.finish AS add_finish, add_lot.set_code AS add_set_code,
               add_lot.collector_number AS add_collector_number,
               add_lot.allocated_cost_cents AS add_cost_cents,
               holder.deck_id AS add_from_deck_id, holder_deck.name AS add_from_deck_name,
               cut_lot.card_name AS cut_card_name
        FROM deck_changes
        LEFT JOIN lots AS add_lot ON add_lot.id = deck_changes.add_lot_id
        LEFT JOIN deck_cards AS holder ON holder.lot_id = deck_changes.add_lot_id AND holder.equipped = 1
        LEFT JOIN decks AS holder_deck ON holder_deck.id = holder.deck_id
        LEFT JOIN lots AS cut_lot ON cut_lot.id = deck_changes.cut_lot_id
        WHERE deck_changes.deck_id = ? AND deck_changes.applied_version IS NULL
        ORDER BY deck_changes.id
        """,
        (deck_id,),
    ).fetchall()
    return [{**dict(row), "ready": row["add_lot_id"] is not None} for row in rows]


def owned_candidates(conn: sqlite3.Connection, deck_id: int, query: str) -> list[dict]:
    """Owned copies matching `query` that could be inserted: not disposed,
    not already on this deck's list or in its change list. Grouped by card
    and current location; `lot_id` is one available copy to use."""
    query = query.strip()
    if not query:
        return []
    taken = _pending_lots(conn, deck_id, "add_lot_id")
    rows = conn.execute(
        """
        SELECT lots.id, lots.card_name, lots.scryfall_id, lots.finish,
               lots.set_code, lots.collector_number,
               holder.deck_id AS holder_id, holder_deck.name AS holder_name
        FROM lots
        LEFT JOIN dispositions ON dispositions.lot_id = lots.id
        LEFT JOIN deck_cards AS holder ON holder.lot_id = lots.id AND holder.equipped = 1
        LEFT JOIN decks AS holder_deck ON holder_deck.id = holder.deck_id
        WHERE dispositions.id IS NULL
          AND lots.card_name LIKE ? ESCAPE '\\'
          AND NOT EXISTS (SELECT 1 FROM deck_cards WHERE deck_id = ? AND lot_id = lots.id)
        ORDER BY lots.card_name COLLATE NOCASE, holder_deck.name, lots.id
        """,
        ("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", deck_id),
    ).fetchall()
    groups: dict[tuple, dict] = {}
    for row in rows:
        if row["id"] in taken:
            continue
        key = (row["card_name"], row["holder_id"])
        group = groups.setdefault(key, {
            "card_name": row["card_name"], "scryfall_id": row["scryfall_id"], "finish": row["finish"],
            "set_code": row["set_code"], "collector_number": row["collector_number"],
            "holder_id": row["holder_id"], "holder_name": row["holder_name"], "lot_id": row["id"], "qty": 0,
        })
        group["qty"] += 1
    return list(groups.values())


def owned_copies_of(conn: sqlite3.Connection, deck_id: int, name: str) -> list[dict]:
    """Insertable owned copies with exactly this name (for linking a planned change)."""
    return [c for c in owned_candidates(conn, deck_id, name)
            if scryfall._normalize(c["card_name"]) == scryfall._normalize(name)]


def cut_options(conn: sqlite3.Connection, deck_id: int) -> list[dict]:
    """Cards on the deck's list, one option per name, each with a copy not
    already being replaced (`lot_id`); names with every copy taken are left
    out (the template keeps each change's own current cut selectable)."""
    taken = _pending_lots(conn, deck_id, "cut_lot_id")
    rows = conn.execute(
        "SELECT lots.id, lots.card_name FROM deck_cards JOIN lots ON lots.id = deck_cards.lot_id"
        " WHERE deck_cards.deck_id = ? ORDER BY lots.card_name COLLATE NOCASE, lots.id",
        (deck_id,),
    ).fetchall()
    options: dict[str, dict] = {}
    for row in rows:
        option = options.setdefault(
            row["card_name"], {"card_name": row["card_name"], "lot_id": None, "qty": 0}
        )
        option["qty"] += 1
        if option["lot_id"] is None and row["id"] not in taken:
            option["lot_id"] = row["id"]
    return [o for o in options.values() if o["lot_id"] is not None]


def list_size(conn: sqlite3.Connection, deck_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM deck_cards WHERE deck_id = ?", (deck_id,)).fetchone()[0]


# --- applying -----------------------------------------------------------------

def apply_changes(conn: sqlite3.Connection, deck_id: int, note: str | None = None) -> int:
    """Apply every ready change as one new version. All or nothing: any
    problem raises ChangeError and nothing is written. Returns the version."""
    deck = _deck(conn, deck_id)
    ready = [c for c in pending_changes(conn, deck_id) if c["ready"]]
    if not ready:
        raise ChangeError("no ready changes to apply (planned cards need an owned copy first)")

    adds = [c["add_lot_id"] for c in ready]
    cuts = [c["cut_lot_id"] for c in ready if c["cut_lot_id"] is not None]
    if len(set(adds)) != len(adds) or len(set(cuts)) != len(cuts) or set(adds) & set(cuts):
        raise ChangeError("the same copy is used twice in these changes")
    for change in ready:
        lot = conn.execute(
            "SELECT lots.card_name, dispositions.id AS disposed FROM lots"
            " LEFT JOIN dispositions ON dispositions.lot_id = lots.id WHERE lots.id = ?",
            (change["add_lot_id"],),
        ).fetchone()
        if lot is None or lot["disposed"] is not None:
            raise ChangeError(f"{change['add_card_name']} is no longer in your collection")
        if _on_list(conn, deck_id, change["add_lot_id"]):
            raise ChangeError(f"that copy of {change['add_card_name']} is already on this deck's list")
        if change["cut_lot_id"] is not None and not _on_list(conn, deck_id, change["cut_lot_id"]):
            raise ChangeError(f"{change['cut_card_name']} is no longer on this deck's list")

    try:
        for change in ready:
            # Unequip wherever it is first, so the one-equipped-per-lot index
            # never sees two; the other deck keeps it on its list (a borrow).
            conn.execute("UPDATE deck_cards SET equipped = 0 WHERE lot_id = ?", (change["add_lot_id"],))
            conn.execute(
                "INSERT INTO deck_cards (deck_id, lot_id, equipped) VALUES (?, ?, 1)",
                (deck_id, change["add_lot_id"]),
            )
            if change["cut_lot_id"] is not None:
                conn.execute(
                    "DELETE FROM deck_cards WHERE deck_id = ? AND lot_id = ?", (deck_id, change["cut_lot_id"])
                )
        if deck["commander"] and not conn.execute(
            "SELECT 1 FROM deck_cards JOIN lots ON lots.id = deck_cards.lot_id"
            " WHERE deck_cards.deck_id = ? AND lots.card_name = ?",
            (deck_id, deck["commander"]),
        ).fetchone():
            conn.execute("UPDATE decks SET commander = NULL WHERE id = ?", (deck_id,))
        version = record_version(conn, deck_id, (note or "").strip() or None, commit=False)
        conn.executemany(
            "UPDATE deck_changes SET applied_version = ? WHERE id = ?", [(version, c["id"]) for c in ready]
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return version


# --- history ------------------------------------------------------------------

def _names(conn: sqlite3.Connection, lot_ids: set[int]) -> list[tuple[str, int]]:
    if not lot_ids:
        return []
    marks = ",".join("?" * len(lot_ids))
    rows = conn.execute(
        f"SELECT card_name, COUNT(*) FROM lots WHERE id IN ({marks}) GROUP BY card_name"
        " ORDER BY card_name COLLATE NOCASE",
        tuple(lot_ids),
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def versions(conn: sqlite3.Connection, deck_id: int) -> list[dict]:
    """Every recorded version, newest first, each with its card count and
    what changed since the previous version (`added`/`removed`: (name, qty))."""
    rows = conn.execute(
        "SELECT * FROM deck_versions WHERE deck_id = ? ORDER BY version", (deck_id,)
    ).fetchall()
    result = []
    previous: set[int] | None = None
    for row in rows:
        lots = {r[0] for r in conn.execute(
            "SELECT lot_id FROM deck_version_cards WHERE version_id = ?", (row["id"],)
        )}
        result.append({
            **dict(row),
            "card_count": len(lots),
            "added": _names(conn, lots - previous) if previous is not None else [],
            "removed": _names(conn, previous - lots) if previous is not None else [],
        })
        previous = lots
    return list(reversed(result))


def version_lot_ids(conn: sqlite3.Connection, deck_id: int, version: int) -> list[int] | None:
    row = conn.execute(
        "SELECT id FROM deck_versions WHERE deck_id = ? AND version = ?", (deck_id, version)
    ).fetchone()
    if row is None:
        return None
    rows = conn.execute("SELECT lot_id FROM deck_version_cards WHERE version_id = ?", (row[0],))
    return [r[0] for r in rows]
