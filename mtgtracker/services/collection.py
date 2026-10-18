"""Collection imports, after the fact: look one up, and undo it."""

from __future__ import annotations

import sqlite3

from mtgtracker.services import CodedError, NotFoundError


class ImportInUseError(CodedError):
    """Some of the import's cards are already part of something else."""

    code = "import_in_use"
    status = 409


# What keeps a lot from being deleted, with the SQL counting the import's
# lots that are in it. Every table that references lots(id) is here (a
# deck_changes row counts whether pending or applied: it holds a foreign key).
_BLOCKERS = {
    "on_deck_lists": "SELECT COUNT(DISTINCT x.lot_id) FROM deck_cards x JOIN lots ON lots.id = x.lot_id",
    "disposed": "SELECT COUNT(DISTINCT x.lot_id) FROM dispositions x JOIN lots ON lots.id = x.lot_id",
    "in_version_snapshots": (
        "SELECT COUNT(DISTINCT x.lot_id) FROM deck_version_cards x JOIN lots ON lots.id = x.lot_id"
    ),
    "in_deck_changes": (
        "SELECT COUNT(DISTINCT lots.id) FROM deck_changes x"
        " JOIN lots ON lots.id IN (x.add_lot_id, x.cut_lot_id)"
    ),
}
_BLOCKER_TEXT = {
    "on_deck_lists": "on a deck's list",
    "disposed": "disposed of",
    "in_version_snapshots": "in a deck version",
    "in_deck_changes": "in a deck's change list",
}


def lot_blockers(conn: sqlite3.Connection, lot_filter: str, params: tuple) -> dict[str, int]:
    """For the lots matched by `lot_filter` (a WHERE clause over `lots`), how
    many are in each thing that forbids deleting them. Empty when none is."""
    found = {}
    for name, sql in _BLOCKERS.items():
        n = conn.execute(f"{sql} WHERE {lot_filter}", params).fetchone()[0]
        if n:
            found[name] = n
    return found


def blockers_text(blockers: dict[str, int]) -> str:
    return ", ".join(f"{n} {_BLOCKER_TEXT[k]}" for k, n in blockers.items())


def get_import(conn: sqlite3.Connection, import_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT collection_imports.*, users.display_name AS owner_name FROM collection_imports"
        " JOIN users ON users.id = collection_imports.owner_id WHERE collection_imports.id = ?",
        (import_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("No such import")
    return row


def delete_import(
    conn: sqlite3.Connection, import_id: int, *, commit: bool = True
) -> tuple[str, dict]:
    """Undo a collection import: delete its lots and its record, in one
    transaction. Refuses (ImportInUseError) if any of its lots is on a deck's
    list (equipped or not), disposed of, in a version snapshot, or in a
    change; nothing is written then. The caller checks that the actor may
    (the import's owner). Returns (notice, preview); the preview is
    deterministic (no timestamps) so it can be confirmed by digest."""
    imp = get_import(conn, import_id)
    count = conn.execute(
        "SELECT COUNT(*) FROM lots WHERE collection_import_id = ?", (import_id,)
    ).fetchone()[0]
    blockers = lot_blockers(conn, "lots.collection_import_id = ?", (import_id,))
    if blockers:
        raise ImportInUseError(f"Can't undo that import: {blockers_text(blockers)}", blockers=blockers)
    preview = {
        "action": "delete_import", "import_id": imp["id"], "owner_id": imp["owner_id"],
        "owner": imp["owner_name"], "filename": imp["original_filename"], "format": imp["format"],
        "lots": count, "effect": "deletes these lots and the import record; the file can be imported again",
    }
    conn.execute("DELETE FROM lots WHERE collection_import_id = ?", (import_id,))
    conn.execute("DELETE FROM collection_imports WHERE id = ?", (import_id,))
    if commit:
        conn.commit()
    n = count
    return f"Removed the import: {n} card{'s' if n != 1 else ''}.", preview
