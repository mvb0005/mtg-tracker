"""Suggested cards for a deck, to compare side by side before deciding.

A suggestion names a card to add, optionally the card on the list it would
replace, a reason, a theme (e.g. "Low-cost creatures", grouping a batch) and
a price snapshot. It is not part of the deck's list: never a lot, a want or
a change, so it stays out of counts, identity, breakdown, exports and
assembly. Accepting one queues a planned change (deckmods) on the Modify
page, paired with its cut; nothing is applied from here. Dismissed
suggestions are kept, so the same card isn't suggested again.

State shown to the user is derived (`state`): "open", "queued" (accepted,
its change still pending), "applied" (that change went into a version),
"on_list" (open, but the card has since reached the list some other way,
e.g. a whole list applied over the API) or "dismissed". An accepted
suggestion whose change was removed from the Modify page is open again.

Votes are the swipe review (`/decks/{id}/suggestions/review`): a thumbs up
or down with an optional note, the user's verdict for the suggester. Up
keeps the suggestion open; down dismisses it. Reopen clears either.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from mtgtracker import deckmods, scryfall

MAX_BATCH = 50
MAX_NAME = 200
MAX_THEME = 80
MAX_REASON = 2000
MAX_NOTE = 2000
VOTES = ("up", "down")


class SuggestionError(ValueError):
    """A suggestion that can't be made or acted on."""


class CardsNotFound(SuggestionError):
    def __init__(self, names: list[str]):
        super().__init__("Scryfall doesn't know: " + ", ".join(names))
        self.names = names


def _label(suggestion: dict) -> str:
    return suggestion["state"].replace("_", " ")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _text(item: dict, key: str, limit: int) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    value = str(value).strip()
    if len(value) > limit:
        raise SuggestionError(f"{key} is longer than {limit} characters")
    return value or None


def _list_names(conn: sqlite3.Connection, deck_id: int) -> list[str]:
    """Every card name on the deck's list, owned or wanted."""
    return [r[0] for r in conn.execute(
        "SELECT lots.card_name FROM deck_cards JOIN lots ON lots.id = deck_cards.lot_id"
        " WHERE deck_cards.deck_id = ?"
        " UNION ALL SELECT card_name FROM deck_wants WHERE deck_id = ?", (deck_id, deck_id),
    )]


def _matching(name: str, names: list[str]) -> str | None:
    keys = scryfall.name_keys(name)
    return next((n for n in names if scryfall.name_keys(n) & keys), None)


def create(
    conn: sqlite3.Connection, deck_id: int, items: list[dict], *, suggested_by: int | None,
    commit: bool = True,
) -> dict:
    """Add suggestions: items are {name, replaces?, reason?, theme?,
    price_cents?}. Every card must resolve on Scryfall (cache first) or the
    whole batch is refused (CardsNotFound), and every `replaces` must be on
    the list. A card already on the list, already suggested for the same
    slot, or dismissed before is skipped, not refused. Returns {created: [ids],
    skipped: [{name, reason}]}."""
    if conn.execute("SELECT 1 FROM decks WHERE id = ?", (deck_id,)).fetchone() is None:
        raise SuggestionError(f"no deck with id {deck_id}")
    if not items:
        raise SuggestionError("nothing to suggest")
    if len(items) > MAX_BATCH:
        raise SuggestionError(f"at most {MAX_BATCH} suggestions at a time")
    cleaned = []
    for item in items:
        name = _text(item, "name", MAX_NAME)
        if not name:
            raise SuggestionError("each suggestion needs a card name")
        price = item.get("price_cents")
        if price is not None and (not isinstance(price, int) or isinstance(price, bool) or price < 0):
            raise SuggestionError(f"price_cents for {name} must be a whole number of cents")
        cleaned.append({
            "name": name, "replaces": _text(item, "replaces", MAX_NAME), "price_cents": price,
            "reason": _text(item, "reason", MAX_REASON), "theme": _text(item, "theme", MAX_THEME),
        })

    found = scryfall.fetch_collection([{"name": c["name"]} for c in cleaned])
    missing = [c["name"] for c, card in zip(cleaned, found, strict=True) if card is None]
    if missing:
        raise CardsNotFound(missing)
    on_list = _list_names(conn, deck_id)
    for c in cleaned:
        if c["replaces"]:
            match = _matching(c["replaces"], on_list)
            if match is None:
                raise SuggestionError(f"{c['replaces']} isn't on this deck's list")
            c["replaces"] = match

    earlier = conn.execute(
        "SELECT card_name, cut_card_name, status FROM deck_suggestions WHERE deck_id = ?", (deck_id,)
    ).fetchall()
    created, skipped, seen = [], [], set()
    for c, card in zip(cleaned, found, strict=True):
        name = card["name"]
        keys = scryfall.name_keys(name)
        slot = (scryfall._normalize(name), scryfall._normalize(c["replaces"] or ""))
        if _matching(name, on_list):
            skipped.append({"name": name, "reason": "already on the list"})
            continue
        if any(r["status"] == "dismissed" and scryfall.name_keys(r["card_name"]) & keys for r in earlier):
            skipped.append({"name": name, "reason": "dismissed before"})
            continue
        if slot in seen or any(
            r["status"] != "dismissed" and scryfall.name_keys(r["card_name"]) & keys
            and (r["cut_card_name"] or "").casefold() == (c["replaces"] or "").casefold()
            for r in earlier
        ):
            skipped.append({"name": name, "reason": "already suggested"})
            continue
        seen.add(slot)
        cur = conn.execute(
            "INSERT INTO deck_suggestions (deck_id, card_name, scryfall_id, cut_card_name, theme, reason,"
            " price_cents, suggested_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (deck_id, name, card["id"], c["replaces"], c["theme"], c["reason"], c["price_cents"],
             suggested_by, _now()),
        )
        created.append(cur.lastrowid)
    if commit:
        conn.commit()
    return {"created": created, "skipped": skipped}


_SELECT = (
    "SELECT s.*, users.display_name AS suggested_by_name,"
    " deck_changes.id AS live_change_id, deck_changes.applied_version"
    " FROM deck_suggestions s"
    " LEFT JOIN users ON users.id = s.suggested_by"
    # The card name too: change ids are reused once a change is deleted, so
    # a link that outlived its change (e.g. after undo-batch) never matches
    # another card's change.
    " LEFT JOIN deck_changes ON deck_changes.id = s.change_id AND deck_changes.deck_id = s.deck_id"
    " AND deck_changes.add_card_name = s.card_name"
)


def _state(row: sqlite3.Row, on_list: list[str]) -> str:
    if row["status"] == "dismissed":
        return "dismissed"
    if row["status"] == "accepted" and row["live_change_id"] is not None:
        return "queued" if row["applied_version"] is None else "applied"
    if _matching(row["card_name"], on_list):
        return "on_list"
    return "open"


def _as_dict(row: sqlite3.Row, on_list: list[str]) -> dict:
    return {
        "id": row["id"], "deck_id": row["deck_id"], "card_name": row["card_name"],
        "scryfall_id": row["scryfall_id"], "cut_card_name": row["cut_card_name"], "theme": row["theme"],
        "reason": row["reason"], "price_cents": row["price_cents"], "suggested_by": row["suggested_by"],
        "suggested_by_name": row["suggested_by_name"], "state": _state(row, on_list),
        "change_id": row["live_change_id"], "applied_version": row["applied_version"],
        "created_at": row["created_at"], "decided_at": row["decided_at"],
        "vote": row["vote"], "vote_note": row["vote_note"], "voted_at": row["voted_at"],
    }


def list_for(conn: sqlite3.Connection, deck_id: int) -> list[dict]:
    """Every suggestion for the deck, oldest first."""
    rows = conn.execute(_SELECT + " WHERE s.deck_id = ? ORDER BY s.id", (deck_id,)).fetchall()
    on_list = _list_names(conn, deck_id)
    return [_as_dict(r, on_list) for r in rows]


def get(conn: sqlite3.Connection, deck_id: int, suggestion_id: int) -> dict:
    row = conn.execute(_SELECT + " WHERE s.id = ? AND s.deck_id = ?", (suggestion_id, deck_id)).fetchone()
    if row is None:
        raise SuggestionError("no such suggestion for this deck")
    return _as_dict(row, _list_names(conn, deck_id))


def accept(conn: sqlite3.Connection, deck_id: int, suggestion_id: int, *, commit: bool = True) -> int:
    """Queue the suggestion on the Modify page: a planned insert, replacing
    an entry for its cut card that no other pending change replaces. Returns
    the change id."""
    suggestion = get(conn, deck_id, suggestion_id)
    if suggestion["state"] != "open":
        raise SuggestionError(f"{suggestion['card_name']} is already {_label(suggestion)}")
    cut = None
    if suggestion["cut_card_name"]:
        used_wants, used_lots = deckmods.pending_cuts(conn, deck_id)
        cut = deckmods.pick_cut(conn, deck_id, suggestion["cut_card_name"], used_wants, used_lots)
        if cut is None:
            raise SuggestionError(
                f"{suggestion['cut_card_name']} isn't on the list any more"
                " (or another change already replaces it)"
            )
    try:
        change_id = deckmods.add_planned_card(
            conn, deck_id, suggestion["card_name"], suggestion["scryfall_id"], commit=False
        )
        if cut is not None:
            deckmods.set_cut(conn, deck_id, change_id, cut.get("lot_id"), cut_want_id=cut.get("want_id"),
                             commit=False)
        conn.execute(
            "UPDATE deck_suggestions SET status = 'accepted', change_id = ?, decided_at = ? WHERE id = ?",
            (change_id, _now(), suggestion_id),
        )
    except Exception:
        if commit:
            conn.rollback()
        raise
    if commit:
        conn.commit()
    return change_id


def dismiss(conn: sqlite3.Connection, deck_id: int, suggestion_id: int, *, commit: bool = True) -> None:
    suggestion = get(conn, deck_id, suggestion_id)
    if suggestion["state"] != "open":
        raise SuggestionError(f"{suggestion['card_name']} is {_label(suggestion)}, not open")
    conn.execute(
        "UPDATE deck_suggestions SET status = 'dismissed', change_id = NULL, decided_at = ? WHERE id = ?",
        (_now(), suggestion_id),
    )
    if commit:
        conn.commit()


def vote(
    conn: sqlite3.Connection, deck_id: int, suggestion_id: int, verdict: str, note: str | None = None,
    *, commit: bool = True,
) -> dict:
    """Thumbs up (stays open) or down (dismissed), with an optional note.
    A vote can be changed, but not on a suggestion that's queued, applied
    or already on the list: that's decided on the Modify page."""
    if verdict not in VOTES:
        raise SuggestionError("vote must be up or down")
    note = _text({"note": note}, "note", MAX_NOTE)
    suggestion = get(conn, deck_id, suggestion_id)
    if suggestion["state"] not in ("open", "dismissed"):
        raise SuggestionError(f"{suggestion['card_name']} is already {_label(suggestion)}")
    now = _now()
    conn.execute(
        "UPDATE deck_suggestions SET status = ?, change_id = NULL, decided_at = ?,"
        " vote = ?, vote_note = ?, voted_at = ? WHERE id = ?",
        ("open" if verdict == "up" else "dismissed", None if verdict == "up" else now,
         verdict, note, now, suggestion_id),
    )
    if commit:
        conn.commit()
    return get(conn, deck_id, suggestion_id)


def to_review(conn: sqlite3.Connection, deck_id: int) -> list[dict]:
    """Open suggestions without a vote, in the order they were suggested
    (so a batch's theme stays together)."""
    return [s for s in list_for(conn, deck_id) if s["state"] == "open" and s["vote"] is None]


def last_voted(conn: sqlite3.Connection, deck_id: int) -> dict | None:
    """The most recent vote that can still be undone (by reopen)."""
    voted = [s for s in list_for(conn, deck_id)
             if s["vote"] is not None and s["state"] in ("open", "dismissed")]
    return max(voted, key=lambda s: (s["voted_at"], s["id"]), default=None)


def reopen(conn: sqlite3.Connection, deck_id: int, suggestion_id: int, *, commit: bool = True) -> None:
    """Back to open and unvoted: undoes a dismiss or either vote."""
    suggestion = get(conn, deck_id, suggestion_id)
    if suggestion["state"] != "dismissed" and not (suggestion["state"] == "open" and suggestion["vote"]):
        raise SuggestionError(f"{suggestion['card_name']} isn't dismissed or voted on")
    conn.execute(
        "UPDATE deck_suggestions SET status = 'open', decided_at = NULL,"
        " vote = NULL, vote_note = NULL, voted_at = NULL WHERE id = ?", (suggestion_id,)
    )
    if commit:
        conn.commit()
