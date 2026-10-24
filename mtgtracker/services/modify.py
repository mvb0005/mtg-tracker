"""Edits to a deck's change list, and applying it as a version. Each returns
the notice to show; the caller has already run the ownership guards on the
deck and on any lot id."""

from __future__ import annotations

import sqlite3

from mtgtracker import deckmods
from mtgtracker.services import CodedError, ServiceError, translate_errors
from mtgtracker.services.decks import MAX_LIST_CHARS


def add_change(
    conn: sqlite3.Connection, deck_id: int, *, lot_id: int | None = None, planned_name: str = "",
    commit: bool = True,
) -> str:
    """Queue an owned copy (lot_id) or, without one, a card to acquire by name."""
    with translate_errors():
        if lot_id is not None:
            deckmods.add_owned(conn, deck_id, lot_id, commit=commit)
            return "Added to the change list."
        deckmods.add_planned(conn, deck_id, planned_name, commit=commit)
        return "Added to the change list as to acquire."


def import_list(conn: sqlite3.Connection, deck_id: int, text: str, *, commit: bool = True) -> str:
    if len(text) > MAX_LIST_CHARS:
        raise ServiceError("That list is too long")
    with translate_errors():
        return deckmods.import_list(conn, deck_id, text, commit=commit).message()


def set_cut(
    conn: sqlite3.Connection, deck_id: int, change_id: int, cut_lot_id: int | None,
    cut_want_id: int | None, *, commit: bool = True,
) -> str:
    with translate_errors():
        deckmods.set_cut(conn, deck_id, change_id, cut_lot_id, cut_want_id=cut_want_id, commit=commit)
    return "Replacement updated."


def add_swap(
    conn: sqlite3.Connection, deck_id: int, lot_id: int | None, want_id: int | None, *, commit: bool = True
) -> str:
    """Replace a card the deck lists but you don't own with one you do."""
    if lot_id is None or want_id is None:
        raise ServiceError("Pick a card to replace and one of yours to use")
    with translate_errors():
        deckmods.add_swap(conn, deck_id, lot_id, want_id, commit=commit)
    return "Queued the swap; apply to make it the next version."


def link_owned(
    conn: sqlite3.Connection, deck_id: int, change_id: int, lot_id: int | None, *, commit: bool = True
) -> str:
    if lot_id is None:
        raise ServiceError("Pick a copy to use")
    with translate_errors():
        deckmods.link_owned(conn, deck_id, change_id, lot_id, commit=commit)
    return "Linked your copy; the change is ready."


def remove_change(conn: sqlite3.Connection, deck_id: int, change_id: int, *, commit: bool = True) -> str:
    with translate_errors():
        deckmods.remove_change(conn, deck_id, change_id, commit=commit)
    return "Removed from the change list."


def apply(conn: sqlite3.Connection, deck_id: int, note: str, *, commit: bool = True) -> str:
    with translate_errors():
        version = deckmods.apply_changes(conn, deck_id, note, commit=commit)
    return f"Applied as version {version}."


# --- the whole edit in one call (the JSON API) --------------------------------

class VersionConflict(CodedError):
    code = "version_conflict"
    status = 409


class PendingChanges(CodedError):
    code = "pending_changes"
    status = 409


class PureCuts(CodedError):
    code = "pure_cuts_unsupported"


def _pick_cut(conn: sqlite3.Connection, deck_id: int, name: str, used_wants: set, used_lots: set) -> dict:
    """The entry a `{"name": ...}` cut means (deckmods.pick_cut), or a refusal."""
    cut = deckmods.pick_cut(conn, deck_id, name, used_wants, used_lots)
    if cut is None:
        raise ServiceError(f"{name} isn't on this deck's list (or every copy is already being cut)")
    return cut


def apply_batch(
    conn: sqlite3.Connection, deck_id: int, *, adds: list[dict], cuts: list[dict], note: str | None,
    expect_version: int, commit: bool = True,
) -> dict:
    """Queue each add (`{"name"}`: to acquire; `{"lot_id"}`: an owned copy),
    pair adds[i] with cuts[i] (`{"lot_id"}`, `{"want_id"}` or `{"name"}`; extra
    adds just grow the deck), and apply them all as ONE new version. Refuses
    if the deck isn't at expect_version (409 version_conflict), already has
    pending Modify-page changes (409 pending_changes), or has more cuts than
    adds (the change model needs an insert per cut). All or nothing. The caller
    owns the guards on the deck and on every lot id. Returns
    {version, added, removed, counts}."""
    if len(cuts) > len(adds):
        raise PureCuts(
            f"{len(cuts)} cuts but only {len(adds)} adds: every cut needs an add to replace it"
            " (a change inserts a card, optionally replacing one)"
        )
    if not adds:
        raise ServiceError("nothing to change")
    version = conn.execute("SELECT version FROM decks WHERE id = ?", (deck_id,)).fetchone()["version"]
    if version != expect_version:
        raise VersionConflict(
            f"the deck is at version {version}, not {expect_version}; re-read it", current_version=version
        )
    pending = deckmods.pending_changes(conn, deck_id)
    if pending:
        raise PendingChanges(
            f"the deck has {len(pending)} pending change(s) from the Modify page;"
            " apply or remove them first", pending=len(pending)
        )
    before = deckmods.list_names(conn, deck_id)
    try:
        with translate_errors():
            new_version = _queue_and_apply(conn, deck_id, adds, cuts, note)
    except Exception:
        if commit:
            conn.rollback()
        raise
    after = deckmods.list_names(conn, deck_id)
    if commit:
        conn.commit()
    return {
        "version": new_version, "previous_version": version,
        "added": sorted((after - before).elements(), key=str.casefold),
        "removed": sorted((before - after).elements(), key=str.casefold),
        "counts": deckmods.list_counts(conn, deck_id),
    }


def _queue_and_apply(conn: sqlite3.Connection, deck_id: int, adds: list[dict], cuts: list[dict], note) -> int:
    used_wants = {c["want_id"] for c in cuts if c.get("want_id") is not None}
    used_lots = {c["lot_id"] for c in cuts if c.get("lot_id") is not None}
    resolved = []
    for cut in cuts:
        if cut.get("name") is not None:
            cut = _pick_cut(conn, deck_id, cut["name"], used_wants, used_lots)
            if "want_id" in cut:
                used_wants.add(cut["want_id"])
            else:
                used_lots.add(cut["lot_id"])
        resolved.append(cut)
    change_ids = []
    for add in adds:
        if add.get("lot_id") is not None:
            change_ids.append(deckmods.add_owned(conn, deck_id, add["lot_id"], commit=False))
        elif (add.get("name") or "").strip():
            change_ids.append(deckmods.add_planned(conn, deck_id, add["name"], commit=False))
        else:
            raise ServiceError("each add needs a card name or a lot_id")
    for change_id, cut in zip(change_ids, resolved, strict=False):
        deckmods.set_cut(conn, deck_id, change_id, cut.get("lot_id"), cut_want_id=cut.get("want_id"),
                         commit=False)
    return deckmods.apply_changes(conn, deck_id, note, commit=False)
