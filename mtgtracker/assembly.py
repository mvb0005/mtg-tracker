"""Deck assembly: turn a deck's list into a physical deck from what its owner
has. `plan()` reads and sorts every entry on the list (owned copies plus
wanted cards) into one category; `apply()` makes the chosen lines happen as
one all-or-nothing transaction.

Categories (one per list entry):
- here:       an owned copy equipped in this deck. Nothing to do.
- fill:       a wanted card the owner has a *free copy* of. Apply swaps the
              want for that copy, equipped here.
- swap:       an owned copy on the list that sits in another deck, when a
              free copy of the same card exists. Apply puts the free copy on
              this list, equipped, instead; the other deck keeps its card.
- pull:       an owned copy on the list that sits in another deck, with no
              free copy. Apply equips it here (the other deck then shows it
              as away). Risky.
- equip:      an owned copy on the list that is in no deck. Apply equips it.
- borrow:     a wanted card a friend owns. Display only (there are no loans).
- acquire:    everything else; still to buy.

A *free copy* is a non-disposed lot of the deck's owner that is on no deck's
list at all and isn't the insert of any pending `deck_changes` row. One copy
fills one slot; wants are served before swaps, and a copy is preferred by:
pinned to the want's printing, then any pinned printing, then unpinned, then
lowest lot id. Money is never touched.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from mtgtracker import deckmods, scryfall
from mtgtracker.decks import record_version
from mtgtracker.printings import BASIC_LAND_NAMES

FILL, SWAP, PULL, EQUIP, HERE, BORROW, ACQUIRE = "fill", "swap", "pull", "equip", "here", "borrow", "acquire"
ACTIONS = (FILL, SWAP, EQUIP, PULL)  # the order sections appear in; only these change anything
CATEGORIES = (HERE, FILL, SWAP, EQUIP, PULL, BORROW, ACQUIRE)
COLLECTION = "your collection"


class AssemblyError(deckmods.ChangeError):
    """The request can't be carried out. `stale` names line ids that are no
    longer in the plan (the list changed since it was shown)."""

    def __init__(self, message: str, stale: list[str] | None = None):
        super().__init__(message)
        self.stale = stale or []


@dataclass
class Line:
    category: str
    card_name: str
    line_id: str | None = None  # None for lines you can't act on (here, borrow, acquire)
    lot_id: int | None = None  # the copy that ends up in the deck (fill/swap: the free one)
    old_lot_id: int | None = None  # swap: the away copy being replaced
    want_id: int | None = None
    scryfall_id: str | None = None
    set_code: str | None = None
    collector_number: str | None = None
    finish: str = "nonfoil"
    from_deck_id: int | None = None  # pull: where the copy is now; swap: where the old copy is
    from_deck_name: str | None = None
    source: str | None = None  # the new copy's origin: "from Corner Games, 2026-10-20"
    friends: list[dict] = field(default_factory=list)
    reason: str | None = None  # why an unavailable line can't be acted on

    @property
    def risky(self) -> bool:
        return self.category == PULL

    @property
    def default_selected(self) -> bool:
        return self.category in (FILL, SWAP, EQUIP)

    @property
    def needs_printing(self) -> bool:
        return self.lot_id is not None and self.scryfall_id is None and self.card_name not in BASIC_LAND_NAMES

    @property
    def printing(self) -> str | None:
        if self.set_code and self.collector_number:
            return f"{self.set_code.upper()} #{self.collector_number}"
        return None


@dataclass
class AssemblyPlan:
    deck_id: int
    owner_id: int
    lines: list[Line]
    counts: dict[str, int]  # here / away / wanted right now
    # on the list but not actionable: disposed of, or not the owner's
    unavailable: list[Line] = field(default_factory=list)

    def of(self, category: str) -> list[Line]:
        return [line for line in self.lines if line.category == category]

    def actionable(self) -> dict[str, Line]:
        return {line.line_id: line for line in self.lines if line.line_id}

    @property
    def acquire(self) -> list[tuple[str, int]]:
        """(card name, copies) still to buy, after free copies and friends."""
        return _count_names(self.of(ACQUIRE))

    @property
    def borrow(self) -> list[tuple[str, int]]:
        return _count_names(self.of(BORROW))

    def selected(self, line_ids: list[str] | None) -> list[Line]:
        """The lines to act on: these ids, or the pre-checked default."""
        if line_ids is None:
            return [line for line in self.lines if line.line_id and line.default_selected]
        by_id = self.actionable()
        return [by_id[i] for i in dict.fromkeys(line_ids) if i in by_id]

    def counts_after(self, line_ids: list[str] | None = None) -> dict[str, int]:
        here, away, wanted = self.counts["here"], self.counts["away"], self.counts["wanted"]
        for line in self.selected(line_ids):
            here += 1
            if line.category == FILL:
                wanted -= 1
            else:  # swap, pull and equip each turn one away copy into a copy here
                away -= 1
        return {"here": here, "away": away, "wanted": wanted}

    def pull_list(self, line_ids: list[str] | None = None) -> list[dict]:
        """Where to physically take each chosen card from: the other decks
        they sit in, then your collection (new copies, loose ones)."""
        places: dict[int | None, dict] = {}
        for line in self.selected(line_ids):
            if line.category == PULL:
                key, name = line.from_deck_id, line.from_deck_name
            else:
                key, name = None, COLLECTION
            place = places.setdefault(key, {"deck_id": key, "name": name, "items": []})
            place["items"].append(_pull_item(line))
        ordered = sorted((p for k, p in places.items() if k is not None), key=lambda p: p["name"].casefold())
        ordered += [places[None]] if None in places else []
        for place in ordered:
            place["items"].sort(key=lambda i: (i["card_name"].casefold(), i["lot_id"]))
            place["total"] = len(place["items"])
        return ordered


def _pull_item(line: Line) -> dict:
    if line.category == PULL:
        label = f"from {line.from_deck_name}"
    elif line.category == EQUIP:
        label = "on this list, in no deck"
    else:
        label = line.source or "from your collection"
    return {
        "line_id": line.line_id, "lot_id": line.lot_id, "card_name": line.card_name,
        "printing": line.printing, "finish": line.finish, "needs_printing": line.needs_printing,
        "label": label,
    }


def _count_names(lines: list[Line]) -> list[tuple[str, int]]:
    counter = Counter(line.card_name for line in lines)
    return sorted(counter.items(), key=lambda item: item[0].casefold())


@dataclass
class AssemblyResult:
    deck_id: int
    version: int | None  # None when nothing on the list changed (only copies moved)
    filled: int
    swapped: int
    pulled: int
    equipped: int
    counts_before: dict[str, int]
    counts_after: dict[str, int]
    pull_list: list[dict]
    line_ids: list[str]

    def summary(self) -> str:
        return (f"Assembled: {self.filled} filled, {self.swapped} swapped, {self.pulled} pulled, "
                f"{self.equipped} equipped")

    def message(self) -> str:
        return self.summary() + (f". Saved as version {self.version}." if self.version else ".")


# --- free copies and where they came from ---------------------------------------

def source_labels(conn: sqlite3.Connection, lot_ids: list[int]) -> dict[int, str]:
    """Where each lot came from, as text: a purchase (vendor and date), a
    collection import (file name) or, with neither, just your collection."""
    labels: dict[int, str] = {}
    for start in range(0, len(lot_ids), 500):
        chunk = lot_ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"""
            SELECT lots.id, transactions.vendor, transactions.created_at,
                   collection_imports.original_filename, collection_imports.imported_at
            FROM lots
            LEFT JOIN transactions ON transactions.id = lots.transaction_id
            LEFT JOIN collection_imports ON collection_imports.id = lots.collection_import_id
            WHERE lots.id IN ({marks})
            """,
            chunk,
        ):
            if row["vendor"]:
                labels[row["id"]] = f"from {row['vendor']}, {row['created_at'][:10]}"
            elif row["imported_at"]:
                name = f" ({row['original_filename']})" if row["original_filename"] else ""
                labels[row["id"]] = f"from your collection import{name}, {row['imported_at'][:10]}"
            else:
                labels[row["id"]] = "from your collection"
    return labels


def _key(card_name: str) -> str:
    return min(scryfall.name_keys(card_name), key=len)  # the front face's normalized name


class _FreePool:
    """The owner's free copies, indexed by name key; take() hands each out once."""

    def __init__(self, conn: sqlite3.Connection, owner_id: int):
        pending = {r[0] for r in conn.execute(
            "SELECT add_lot_id FROM deck_changes WHERE applied_version IS NULL AND add_lot_id IS NOT NULL"
        )}
        self._lots: dict[int, sqlite3.Row] = {}
        self._by_key: dict[str, list[int]] = {}
        for row in conn.execute(
            """
            SELECT lots.* FROM lots
            WHERE lots.owner_id = ?
              AND NOT EXISTS (SELECT 1 FROM dispositions WHERE dispositions.lot_id = lots.id)
              AND NOT EXISTS (SELECT 1 FROM deck_cards WHERE deck_cards.lot_id = lots.id)
            ORDER BY lots.id
            """,
            (owner_id,),
        ):
            if row["id"] in pending:
                continue
            self._lots[row["id"]] = row
            for key in scryfall.name_keys(row["card_name"]):
                self._by_key.setdefault(key, []).append(row["id"])

    def take(self, card_name: str, prefer_scryfall_id: str | None) -> sqlite3.Row | None:
        ids = {i for key in scryfall.name_keys(card_name) for i in self._by_key.get(key, ())}
        ids &= self._lots.keys()
        if not ids:
            return None

        def rank(lot_id: int):
            lot = self._lots[lot_id]
            exact = prefer_scryfall_id is not None and lot["scryfall_id"] == prefer_scryfall_id
            return (0 if exact else 1, 0 if lot["scryfall_id"] else 1, lot_id)

        return self._lots.pop(min(ids, key=rank))


# --- the plan --------------------------------------------------------------------

def plan(conn: sqlite3.Connection, deck_id: int, *, exclude_friends: set[int] | None = None) -> AssemblyPlan:
    """Sort every entry on the deck's list into a category. Read-only.
    `exclude_friends` are user ids left out of the borrowable copies (the
    deck's owner always is); the deck page also leaves out its viewer."""
    from mtgtracker.web.queries import FriendCopies  # web read-model; imported late to avoid a cycle

    deck = conn.execute("SELECT * FROM decks WHERE id = ?", (deck_id,)).fetchone()
    if deck is None:
        raise AssemblyError(f"no deck with id {deck_id}")
    owner_id = deck["owner_id"]
    pool = _FreePool(conn, owner_id)
    # What a queued Modify change is about to replace is left alone, so applying
    # that change doesn't fail later.
    cut_wants, cut_lots = set(), set()
    for row in conn.execute(
        "SELECT cut_want_id, cut_lot_id FROM deck_changes WHERE deck_id = ? AND applied_version IS NULL",
        (deck_id,),
    ):
        cut_wants.add(row["cut_want_id"])
        cut_lots.add(row["cut_lot_id"])
    queued = "queued to be replaced on the Modify page"
    friends = FriendCopies(conn, {owner_id, *(exclude_friends or ())})

    owned = conn.execute(
        """
        SELECT lots.*, deck_cards.equipped AS here, holder.deck_id AS holder_id,
               holder_deck.name AS holder_name, dispositions.id IS NOT NULL AS disposed
        FROM deck_cards
        JOIN lots ON lots.id = deck_cards.lot_id
        LEFT JOIN deck_cards AS holder ON holder.lot_id = lots.id AND holder.equipped = 1
        LEFT JOIN decks AS holder_deck ON holder_deck.id = holder.deck_id
        LEFT JOIN dispositions ON dispositions.lot_id = lots.id
        WHERE deck_cards.deck_id = ?
        ORDER BY lots.id
        """,
        (deck_id,),
    ).fetchall()
    wants = conn.execute("SELECT * FROM deck_wants WHERE deck_id = ? ORDER BY id", (deck_id,)).fetchall()

    lines: list[Line] = []
    unavailable: list[Line] = []
    away: list[sqlite3.Row] = []
    for lot in owned:
        base = {
            "card_name": lot["card_name"], "scryfall_id": lot["scryfall_id"], "set_code": lot["set_code"],
            "collector_number": lot["collector_number"], "finish": lot["finish"],
        }
        if lot["here"]:
            lines.append(Line(HERE, lot_id=lot["id"], **base))
        elif lot["id"] in cut_lots:
            unavailable.append(Line(PULL, lot_id=lot["id"], reason=queued, **base))
        elif lot["owner_id"] != owner_id:  # someone else's copy: not ours to move
            unavailable.append(Line(HERE, lot_id=lot["id"], **base))
        elif lot["holder_id"] is None:
            if lot["disposed"]:
                unavailable.append(Line(EQUIP, lot_id=lot["id"], **base))
            else:
                lines.append(Line(EQUIP, line_id=f"equip:lot:{lot['id']}", lot_id=lot["id"], **base))
        else:
            away.append(lot)

    # Wants first (the cards you bought for them), then away copies.
    pending_fills: list[tuple[sqlite3.Row, sqlite3.Row]] = []
    leftover: list[sqlite3.Row] = []
    for want in wants:
        if want["id"] in cut_wants:
            unavailable.append(Line(FILL, card_name=want["card_name"], want_id=want["id"],
                                    scryfall_id=want["scryfall_id"], reason=queued))
            continue
        free = pool.take(want["card_name"], want["scryfall_id"])
        if free is None:
            leftover.append(want)
        else:
            pending_fills.append((want, free))
    pending_swaps: list[tuple[sqlite3.Row, sqlite3.Row]] = []
    pulls: list[sqlite3.Row] = []
    for lot in away:
        free = pool.take(lot["card_name"], lot["scryfall_id"])
        if free is None and lot["disposed"]:  # sold or lost: can't be pulled, nothing to swap in
            unavailable.append(Line(PULL, lot_id=lot["id"], card_name=lot["card_name"]))
        elif free is None:
            pulls.append(lot)
        else:
            pending_swaps.append((lot, free))

    labels = source_labels(conn, [free["id"] for _, free in pending_fills + pending_swaps])
    for want, free in pending_fills:
        lines.append(Line(
            FILL, line_id=f"fill:want:{want['id']}:lot:{free['id']}", lot_id=free["id"], want_id=want["id"],
            card_name=free["card_name"], scryfall_id=free["scryfall_id"], set_code=free["set_code"],
            collector_number=free["collector_number"], finish=free["finish"], source=labels[free["id"]],
        ))
    for old, free in pending_swaps:
        lines.append(Line(
            SWAP, line_id=f"swap:lot:{old['id']}:lot:{free['id']}", lot_id=free["id"], old_lot_id=old["id"],
            card_name=free["card_name"], scryfall_id=free["scryfall_id"], set_code=free["set_code"],
            collector_number=free["collector_number"], finish=free["finish"], source=labels[free["id"]],
            from_deck_id=old["holder_id"], from_deck_name=old["holder_name"],
        ))
    for lot in pulls:
        lines.append(Line(
            PULL, line_id=f"pull:lot:{lot['id']}", lot_id=lot["id"], card_name=lot["card_name"],
            scryfall_id=lot["scryfall_id"], set_code=lot["set_code"],
            collector_number=lot["collector_number"], finish=lot["finish"],
            from_deck_id=lot["holder_id"], from_deck_name=lot["holder_name"],
        ))

    # What's left of the wants: a friend's copy covers each one (one friend
    # copy per slot, however the want is spelled), otherwise it's to buy.
    lent: Counter = Counter()
    for want in leftover:
        who = friends(want["card_name"])
        key = _key(want["card_name"])
        if sum(f["qty"] for f in who) - lent[key] > 0:
            lent[key] += 1
            lines.append(Line(BORROW, card_name=want["card_name"], want_id=want["id"],
                              scryfall_id=want["scryfall_id"], friends=who))
        else:
            lines.append(Line(ACQUIRE, card_name=want["card_name"], want_id=want["id"],
                              scryfall_id=want["scryfall_id"]))

    order = {c: i for i, c in enumerate(CATEGORIES)}
    lines.sort(key=lambda ln: (order[ln.category], ln.card_name.casefold(), ln.lot_id or 0, ln.want_id or 0))
    return AssemblyPlan(deck_id, owner_id, lines, deckmods.list_counts(conn, deck_id), unavailable)


# --- applying ---------------------------------------------------------------------

def apply(
    conn: sqlite3.Connection, deck_id: int, line_ids: list[str], *, note: str | None = None,
    commit: bool = True, exclude_friends: set[int] | None = None,
) -> AssemblyResult:
    """Carry out the chosen plan lines as one transaction. The plan is made
    again inside it: a line id that isn't in the fresh plan (or isn't an
    action) is refused by name and nothing is written. Records one new deck
    version when the list's contents changed (a fill or a swap); pulls and
    equips only move copies, which a snapshot doesn't show."""
    ids = list(dict.fromkeys(line_ids))
    if not ids:
        raise AssemblyError("Nothing selected to assemble")
    fresh = plan(conn, deck_id, exclude_friends=exclude_friends)
    by_id = fresh.actionable()
    stale = [i for i in ids if i not in by_id]
    if stale:
        raise AssemblyError(
            f"The plan has changed; these steps no longer apply: {', '.join(stale)}. "
            "Reload and choose again.",
            stale,
        )
    chosen = [by_id[i] for i in ids]
    before = fresh.counts
    try:
        for line in chosen:
            if line.category == FILL:
                conn.execute("DELETE FROM deck_wants WHERE id = ? AND deck_id = ?", (line.want_id, deck_id))
            elif line.category == SWAP:
                conn.execute(
                    "DELETE FROM deck_cards WHERE deck_id = ? AND lot_id = ?", (deck_id, line.old_lot_id)
                )
            if line.category in (PULL, EQUIP):
                # Unequip wherever it is first so the one-equipped-per-lot index never sees two.
                conn.execute("UPDATE deck_cards SET equipped = 0 WHERE lot_id = ?", (line.lot_id,))
                conn.execute(
                    "UPDATE deck_cards SET equipped = 1 WHERE deck_id = ? AND lot_id = ?",
                    (deck_id, line.lot_id),
                )
            else:
                conn.execute(
                    "INSERT INTO deck_cards (deck_id, lot_id, equipped) VALUES (?, ?, 1)",
                    (deck_id, line.lot_id),
                )
        tally = Counter(line.category for line in chosen)
        result = AssemblyResult(
            deck_id=deck_id, version=None, filled=tally[FILL], swapped=tally[SWAP], pulled=tally[PULL],
            equipped=tally[EQUIP], counts_before=before, counts_after=deckmods.list_counts(conn, deck_id),
            pull_list=fresh.pull_list(ids), line_ids=ids,
        )
        if tally[FILL] or tally[SWAP]:
            result.version = record_version(
                conn, deck_id, (note or "").strip() or result.summary(), commit=False
            )
        if commit:
            conn.commit()
    except Exception:
        if commit:
            conn.rollback()
        raise
    return result


# --- the pull list in a URL (so a reload of the deck page still shows it) --------------

def encode_pull_list(pull_list: list[dict]) -> str:
    """"12-345,0-678": place (deck id, 0 for your collection) and lot id."""
    return ",".join(f"{p['deck_id'] or 0}-{i['lot_id']}" for p in pull_list for i in p["items"])


def decode_pull_list(conn: sqlite3.Connection, deck_id: int, text: str) -> list[dict]:
    """The pull list for lots now on this deck's list, rebuilt from encode_pull_list.
    Anything that isn't one of the deck's own lots (or a real deck) is dropped."""
    pairs = []
    for part in text.split(",")[:500]:
        place, _, lot = part.partition("-")
        if place.isdigit() and lot.isdigit():
            pairs.append((int(place), int(lot)))
    if not pairs:
        return []
    marks = ",".join("?" * len(pairs))
    lots = {
        r["id"]: r for r in conn.execute(
            f"SELECT lots.* FROM deck_cards JOIN lots ON lots.id = deck_cards.lot_id"
            f" WHERE deck_cards.deck_id = ? AND lots.id IN ({marks})",
            (deck_id, *(lot for _, lot in pairs)),
        )
    }
    decks = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM decks")}
    labels = source_labels(conn, list(lots))
    places: dict[int, dict] = {}
    for place, lot_id in pairs:
        lot = lots.get(lot_id)
        if lot is None or (place and place not in decks):
            continue
        line = Line(PULL if place else FILL, card_name=lot["card_name"], lot_id=lot_id,
                    scryfall_id=lot["scryfall_id"], set_code=lot["set_code"],
                    collector_number=lot["collector_number"], finish=lot["finish"],
                    from_deck_id=place or None, from_deck_name=decks.get(place),
                    source=labels[lot_id])
        name = decks[place] if place else COLLECTION
        group = places.setdefault(place, {"deck_id": place or None, "name": name, "items": []})
        group["items"].append(_pull_item(line))
    ordered = sorted((p for p in places.values() if p["deck_id"]), key=lambda p: p["name"].casefold())
    ordered += [p for p in places.values() if not p["deck_id"]]
    for group in ordered:
        group["items"].sort(key=lambda i: (i["card_name"].casefold(), i["lot_id"]))
        group["total"] = len(group["items"])
    return ordered
