"""Record a purchase of singles (say, from a local game store) as cards the
buyer owns, with a correct cost basis.

A purchase only adds cards to the buyer's collection: one transaction
(`source = 'manual'`), one product per cart line, one lot per physical copy.
It never touches a deck -- putting the cards into decks is a separate,
deliberate step. `wanted_by` only *points out* which of the buyer's decks
want the new cards.

Two phases, so a preview can't leave a trace:
- `plan()` parses the cart, resolves every card on Scryfall (cache first),
  checks the money and does all the splits. It reads, and may fetch into the
  Scryfall cache, but writes nothing to the database.
- `record()` writes a plan in one transaction (`commit=False` leaves it open).

Money is integer cents. Tax and shipping are split over the lines pro rata
(largest remainder, ties by line order, like the Apple Wallet importer), and
each line's cost basis is split evenly over its copies (allocation.allocate).

Resolution is honest, not guessed. Every card must be found by name or the
whole purchase is refused (money has to attach to real cards; there are no
fuzzy retries). A printing is pinned only when the cart leaves no doubt:
- a set (by name, to a Scryfall set code) plus a collector number that
  Scryfall finds in that set under the same card name; or
- a set where the fully cached set data holds exactly one printing of the
  card (a set that isn't cached is never fetched whole just to count).
In both cases the printing must come in the line's finish. Anything else --
no set, an unknown set name, several printings in the set -- keeps the card's
name but leaves scryfall_id, set_code and collector_number NULL, for the
printings queue.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date as date_type

from mtgtracker import allocation, scryfall
from mtgtracker.importers import cart_text
from mtgtracker.money import format_cents, largest_remainder_split

MAX_VENDOR = 120


class PurchaseError(ValueError):
    """The purchase can't be recorded; nothing was written."""


class CardsNotFoundError(PurchaseError):
    """Some cards aren't on Scryfall; the whole purchase is refused."""

    def __init__(self, names: list[str]):
        shown = ", ".join(names[:8]) + ("…" if len(names) > 8 else "")
        super().__init__(f"{len(names)} card{'s' if len(names) != 1 else ''} not found on Scryfall: {shown}")
        self.names = names


# --- sets: store names -> Scryfall set codes -------------------------------------------

class SetIndex:
    """Scryfall sets by normalized name. A store's name matches only exactly,
    or with a trailing "decks" dropped ("... Commander Decks" is Scryfall's
    "... Commander"); a name two sets share matches neither."""

    def __init__(self, sets: list[dict]):
        self._by_name: dict[str, set[str]] = {}
        self._codes = {s["code"].lower() for s in sets}
        for s in sets:
            self._by_name.setdefault(scryfall._normalize(s["name"]), set()).add(s["code"].lower())

    def code_for(self, name: str | None) -> str | None:
        if not name:
            return None
        norm = scryfall._normalize(name)
        for candidate in (norm, norm.removesuffix(" decks") if norm.endswith(" decks") else None):
            codes = self._by_name.get(candidate or "", set())
            if len(codes) == 1:
                return next(iter(codes))
        if " " not in name.strip() and name.strip().lower() in self._codes:
            return name.strip().lower()
        return None


# --- resolution -------------------------------------------------------------------------

@dataclass(frozen=True)
class PlannedLine:
    line: cart_text.CartLine
    card_name: str  # canonical, from Scryfall
    scryfall_id: str | None
    set_code: str | None
    collector_number: str | None
    reason: str | None  # why the printing is unpinned, or None when pinned
    share_cents: int  # this line's share of tax + shipping
    cost_basis_cents: int
    copy_costs: tuple[int, ...]

    @property
    def pinned(self) -> bool:
        return self.scryfall_id is not None


@dataclass
class PurchasePlan:
    owner_id: int
    vendor: str
    date: str
    format: str
    subtotal_cents: int
    tax_cents: int
    shipping_cents: int
    total_cents: int
    lines: list[PlannedLine]
    wanted_by: list[dict] = field(default_factory=list)

    @property
    def copies(self) -> int:
        return sum(p.line.qty for p in self.lines)

    @property
    def unpinned(self) -> int:
        return sum(p.line.qty for p in self.lines if not p.pinned)


@dataclass
class PurchaseSummary:
    transaction_id: int | None  # None for a plan that wasn't recorded
    lines: int
    copies: int
    total_cents: int
    unpinned: int
    wanted_by: list[dict]
    detail: list[dict]

    def message(self, *, dry_run: bool = False) -> str:
        verb = "Would record" if dry_run else "Recorded"
        parts = [f"{verb} {self.copies} card{'s' if self.copies != 1 else ''} on {self.lines} "
                 f"line{'s' if self.lines != 1 else ''} for {format_cents(self.total_cents)}."]
        if self.unpinned:
            parts.append(f"{self.unpinned} need{'s' if self.unpinned == 1 else ''} a printing picked.")
        wanted = sum(w["copies"] for w in self.wanted_by)
        if wanted:
            parts.append(f"{wanted} match{'es' if wanted == 1 else ''} cards your decks want "
                         f"(nothing {'would be' if dry_run else 'was'} added to a deck).")
        return " ".join(parts)


def _numbers(hint: str) -> list[str]:
    """The collector number as printed, and without the zero padding stores
    use ("0064" is Scryfall's "64")."""
    stripped = hint.lstrip("0") or "0"
    return [stripped] if stripped == hint else [stripped, hint]


def _pin(
    line: cart_text.CartLine, code: str | None, card: dict, set_unknown: bool,
) -> tuple[dict | None, str | None]:
    """The one printing this line is, or (None, why not)."""
    if not line.set_name:
        return None, "the cart gave no set"
    if code is None:
        return None, f"set {line.set_name!r} isn't a Scryfall set" if set_unknown else "set unknown"
    names = scryfall._names(card)
    found: dict | None = None
    if line.collector_number:
        for number in _numbers(line.collector_number):
            [hit] = scryfall.fetch_collection([{"set": code, "collector_number": number}])
            if hit is not None and scryfall._names(hit) & names:
                found = hit
                break
        if found is None:
            return None, f"{card['name']} #{line.collector_number} isn't in {code.upper()}"
    else:
        if not (scryfall.CACHE_DIR / f"{code}.json").exists():
            return None, f"set {code.upper()} isn't cached, so its printings can't be counted"
        printings = {c["id"]: c for c in scryfall.fetch_set_cards(code) if scryfall._names(c) & names}
        if len(printings) != 1:
            return None, (f"{len(printings)} printings in {code.upper()}" if printings
                          else f"{card['name']} isn't in {code.upper()}")
        found = next(iter(printings.values()))
    finishes = found.get("finishes")
    if finishes and line.finish not in finishes:
        return None, f"that printing doesn't come in {line.finish}"
    return found, None


def _resolve(lines: list[cart_text.CartLine]) -> list[tuple[dict, dict | None, str | None]]:
    """(card by name, pinned printing or None, why unpinned) per line.
    Raises CardsNotFoundError listing every name Scryfall doesn't know."""
    index: SetIndex | None = None
    if any(line.set_name for line in lines):
        index = SetIndex(scryfall.fetch_sets())

    def code_of(set_name: str | None) -> str | None:
        nonlocal index
        code = index.code_for(set_name) if index else None
        if code is None and set_name and index is not None:
            # An unknown name may be a set newer than the cached list.
            fresh = scryfall.fetch_sets(refresh=True)
            index = SetIndex(fresh)
            code = index.code_for(set_name)
        return code

    chosen: list[tuple[str | None, str, str | None, list[tuple[str | None, str]]]] = []
    for line in lines:
        options = [(line.set_name, line.name), *line.set_alternatives]
        pick = next(((s, n) for s, n in options if code_of(s)), options[0])
        chosen.append((pick[0], pick[1], code_of(pick[0]), [o for o in options if o != pick]))

    cards = scryfall.fetch_collection([{"name": name} for _, name, _, _ in chosen])
    for i, card in enumerate(cards):  # another way to cut "Set: Card" may name a real card
        for _, name in chosen[i][3]:
            if card is None:
                [card] = scryfall.fetch_collection([{"name": name}])
        cards[i] = card
    missing = [name for (_, name, _, _), card in zip(chosen, cards, strict=True) if card is None]
    if missing:
        raise CardsNotFoundError(sorted(set(missing), key=str.casefold))

    out = []
    for line, (set_name, _name, code, _), card in zip(lines, chosen, cards, strict=True):
        probe = line if set_name == line.set_name else _with_set(line, set_name)
        pinned, reason = _pin(probe, code, card, set_unknown=bool(set_name) and code is None)
        out.append((card, pinned, reason))
    return out


def _with_set(line: cart_text.CartLine, set_name: str | None) -> cart_text.CartLine:
    return replace(line, set_name=set_name)


# --- the plan -----------------------------------------------------------------------------

def _check_money(subtotal: int, tax: int, shipping: int, total: int) -> None:
    if min(tax, shipping, total) < 0:
        raise PurchaseError("tax, shipping and total can't be negative")
    expected = subtotal + tax + shipping
    if expected != total:
        diff = total - expected
        raise PurchaseError(
            f"lines add up to {format_cents(subtotal)}; with tax {format_cents(tax)} and shipping "
            f"{format_cents(shipping)} that is {format_cents(expected)}, but the total is "
            f"{format_cents(total)}; off by {format_cents(abs(diff))}"
            f" ({'total is higher' if diff > 0 else 'total is lower'})"
        )


def wanted_by(conn: sqlite3.Connection, owner_id: int, copies_by_name: Counter) -> list[dict]:
    """For each of the owner's decks, how many of the new copies match its
    deck_wants by normalized name or front face: per distinct new card,
    min(new copies, matching wants). Display only."""
    new = [(name, n, scryfall.name_keys(name)) for name, n in copies_by_name.items()]
    result = []
    for deck in conn.execute("SELECT id, name FROM decks WHERE owner_id = ? ORDER BY id", (owner_id,)):
        wants = Counter(r["card_name"] for r in conn.execute(
            "SELECT card_name FROM deck_wants WHERE deck_id = ?", (deck["id"],)))
        cards = []
        for name, n, keys in new:
            matching = sum(c for want, c in wants.items() if scryfall.name_keys(want) & keys)
            if matching:
                cards.append({"name": name, "copies": min(n, matching)})
        if cards:
            result.append({"deck_id": deck["id"], "deck": deck["name"],
                           "copies": sum(c["copies"] for c in cards), "cards": cards})
    return result


def plan(
    conn: sqlite3.Connection, cart: cart_text.ParsedCart | str, *, owner_id: int, vendor: str,
    date: str | None = None, tax_cents: int, shipping_cents: int, total_cents: int,
) -> PurchasePlan:
    """Resolve, reconcile and split a cart. Nothing is written to the
    database. Raises PurchaseError (a CardsNotFoundError lists the names
    Scryfall doesn't have), or cart_text.CartParseError for a bad cart."""
    vendor = (vendor or "").strip()
    if not vendor or len(vendor) > MAX_VENDOR:
        raise PurchaseError(f"the vendor is required (at most {MAX_VENDOR} characters)")
    try:
        when = date_type.fromisoformat(date).isoformat() if date else date_type.today().isoformat()
    except ValueError:
        raise PurchaseError(f"the date {date!r} isn't YYYY-MM-DD") from None
    parsed = cart_text.parse(cart) if isinstance(cart, str) else cart
    subtotal = parsed.subtotal_cents
    _check_money(subtotal, tax_cents, shipping_cents, total_cents)

    extra = tax_cents + shipping_cents
    weights = [line.total_cents for line in parsed.lines]
    if extra == 0:
        shares = [0] * len(weights)
    elif subtotal == 0:
        raise PurchaseError("tax and shipping can't be spread over lines that cost nothing")
    else:
        shares = largest_remainder_split(extra, weights)

    resolved = _resolve(parsed.lines)
    planned = []
    for line, share, (card, pinned, reason) in zip(parsed.lines, shares, resolved, strict=True):
        basis = line.total_cents + share
        planned.append(PlannedLine(
            line=line, card_name=card["name"],
            scryfall_id=pinned["id"] if pinned else None,
            set_code=pinned["set"] if pinned else None,
            collector_number=pinned["collector_number"] if pinned else None,
            reason=reason, share_cents=share, cost_basis_cents=basis,
            copy_costs=tuple(largest_remainder_split(basis, [1] * line.qty)),
        ))
    by_name = Counter()
    for p in planned:
        by_name[p.card_name] += p.line.qty
    return PurchasePlan(
        owner_id=owner_id, vendor=vendor, date=when, format=parsed.format, subtotal_cents=subtotal,
        tax_cents=tax_cents, shipping_cents=shipping_cents, total_cents=total_cents, lines=planned,
        wanted_by=wanted_by(conn, owner_id, by_name),
    )


# --- writing ------------------------------------------------------------------------------------

def line_detail(p: PlannedLine, lot_ids: list[int] | None = None) -> dict:
    out = {
        "line": p.line.line, "text": p.line.raw, "card": p.card_name, "qty": p.line.qty,
        "finish": p.line.finish, "unit_cents": p.line.unit_cents, "subtotal_cents": p.line.total_cents,
        "tax_shipping_cents": p.share_cents, "cost_basis_cents": p.cost_basis_cents,
        "copy_costs_cents": list(p.copy_costs), "pinned": p.pinned,
        "scryfall_id": p.scryfall_id, "set_code": p.set_code, "collector_number": p.collector_number,
        "needs_printing_because": p.reason, "notes": p.line.notes,
    }
    if lot_ids is not None:
        out["lot_ids"] = lot_ids
    return out


def summarize(plan_: PurchasePlan, transaction_id: int | None = None,
              lot_ids: list[list[int]] | None = None) -> PurchaseSummary:
    return PurchaseSummary(
        transaction_id=transaction_id, lines=len(plan_.lines), copies=plan_.copies,
        total_cents=plan_.total_cents, unpinned=plan_.unpinned, wanted_by=plan_.wanted_by,
        detail=[line_detail(p, lot_ids[i] if lot_ids else None) for i, p in enumerate(plan_.lines)],
    )


def record(conn: sqlite3.Connection, plan_: PurchasePlan, *, commit: bool = True) -> PurchaseSummary:
    """Write a plan: the transaction, a product per line, a lot per copy
    (unassigned: no deck_cards row, nothing in any deck changes). With
    commit=False nothing is committed or rolled back."""
    try:
        cur = conn.execute(
            """
            INSERT INTO transactions (vendor, created_at, updated_at, subtotal_cents, tax_cents,
                shipping_cents, total_cents, currency, source, owner_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'USD', 'manual', ?)
            """,
            (plan_.vendor, plan_.date, plan_.date, plan_.subtotal_cents, plan_.tax_cents,
             plan_.shipping_cents, plan_.total_cents, plan_.owner_id),
        )
        transaction_id = cur.lastrowid
        lot_ids: list[list[int]] = []
        for p in plan_.lines:
            product_id = conn.execute(
                """
                INSERT INTO products (transaction_id, title, display_name, qty, unit_price_cents,
                    line_subtotal_cents, tax_allocated_cents, cost_basis_cents, product_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'single')
                """,
                (transaction_id, p.line.raw, p.card_name, p.line.qty, p.line.unit_cents,
                 p.line.total_cents, p.share_cents, p.cost_basis_cents),
            ).lastrowid
            ids = [
                conn.execute(
                    """
                    INSERT INTO lots (owner_id, product_id, transaction_id, scryfall_id, card_name,
                        set_code, collector_number, finish, acquired_date, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (plan_.owner_id, product_id, transaction_id, p.scryfall_id, p.card_name,
                     p.set_code, p.collector_number, p.line.finish, plan_.date, p.line.notes),
                ).lastrowid
                for _ in range(p.line.qty)
            ]
            allocation.allocate(conn, product_id, commit=False)  # the line's cost, evenly over its copies
            lot_ids.append(ids)
        if commit:
            conn.commit()
    except Exception:
        if commit:
            conn.rollback()
        raise
    return summarize(plan_, transaction_id, lot_ids)
