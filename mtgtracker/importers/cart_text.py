"""Parse a store cart or receipt into lines to record as a purchase.

Three shapes are understood, detected from the first non-blank line:

1. A store CSV: `Card Name,Set / Edition,Rarity,Condition,Quantity,Price`
   (Price is per copy).
2. A store checkout paste: `Description  Cond  Qty  Stock  Price  Total`,
   columns separated by tabs or runs of spaces; the description is
   "Set name: Card name" and Total (a line total) must equal Qty x Price.
3. A plain list, one `N Name[ (set, condition)], $price` per line (price per
   copy, or a line total when it ends in "total"), with an optional
   `TOTAL: N items, $X subtotal` line that must match what was parsed.

Parsing is all or nothing: anything it can't read raises CartParseError
naming the line. This module reads text only -- it knows nothing about
Scryfall or the database. A parenthesized suffix on a name carries hints
("(0064 - Showcase)": a collector number and a treatment; "(Foil)": the
finish; "(NM)": a condition). The collector number and the set name are only
hints for pinning a printing (purchases.py decides whether they are enough);
anything else the text says goes to the lot's notes.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from mtgtracker.money import parse_cents

MAX_LINES = 2_000
MAX_QTY = 1_000
MAX_COPIES = 5_000


class CartParseError(ValueError):
    """The cart can't be read; nothing was recorded."""


@dataclass(frozen=True)
class CartLine:
    line: int  # 1-based line number in the pasted text
    raw: str  # the line as written (the product title)
    name: str
    qty: int
    unit_cents: int
    total_cents: int  # qty * unit_cents
    set_name: str | None = None
    # Other ways to split "Set: Card" when the card or the set has a colon of
    # its own, as (set name, card name), most likely first. Only checkout text.
    set_alternatives: tuple[tuple[str, str], ...] = ()
    collector_number: str | None = None  # a hint, as printed ("0064")
    finish: str = "nonfoil"
    condition: str | None = None  # None means NM
    treatments: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()

    @property
    def notes(self) -> str | None:
        """What goes to lots.notes: a non-NM condition, and anything else the text said."""
        parts = ([f"Condition: {self.condition}"] if self.condition else [])
        parts += [*self.treatments, *self.extra]
        return "; ".join(parts) or None


@dataclass
class ParsedCart:
    format: str
    lines: list[CartLine] = field(default_factory=list)

    @property
    def copies(self) -> int:
        return sum(line.qty for line in self.lines)

    @property
    def subtotal_cents(self) -> int:
        return sum(line.total_cents for line in self.lines)


# --- small vocabularies ------------------------------------------------------------

_CONDITIONS = {
    "nm": None, "near mint": None, "nm-mint": None, "mint": None, "m": None, "nm/m": None,
    "lp": "LP", "light play": "LP", "lightly played": "LP", "sp": "LP", "slightly played": "LP",
    "ex": "EX", "excellent": "EX", "mp": "MP", "moderate play": "MP", "moderately played": "MP",
    "hp": "HP", "heavy play": "HP", "heavily played": "HP", "pl": "HP", "played": "HP",
    "vg": "VG", "very good": "VG", "gd": "GD", "good": "GD", "poor": "Poor",
    "dmg": "DMG", "damaged": "DMG",
}
_TREATMENT_WORDS = {
    "showcase", "borderless", "extended", "art", "full", "retro", "frame", "promo", "prerelease",
    "serialized", "textless", "alternate", "alt", "anime", "gilded", "galaxy", "surge", "ripple",
    "halo", "step-and-compleat", "oil", "slick", "concept", "praetor", "japanese", "etched",
    "foil", "non-foil", "nonfoil", "stamped", "release", "buy-a-box", "bundle", "judge", "textured",
    "raised", "confetti", "fracture", "neon", "ink", "mana", "scroll", "rainbow", "double",
}
_ETCHED = re.compile(r"\b(foil[ -]etched|etched([ -]foil)?)\b", re.I)
_FOIL = re.compile(r"(?<![a-z])(?<!non-)(?<!non )foil\b", re.I)
_NUMBER = re.compile(r"^[A-Za-z]{0,3}\d{1,4}[A-Za-z★]{0,2}$")
_MARKER = re.compile(r"\s+\*([FE])\*$")
_FINISH_SUFFIX = re.compile(r"\s+-\s+((?:non-?foil|foil[ -]etched|etched(?: foil)?|foil))$", re.I)
_PAREN = re.compile(r"\s*\(([^()]*)\)$")


def _finish_of(text: str) -> str | None:
    if _ETCHED.search(text):
        return "etched"
    if _FOIL.search(text):
        return "foil"
    return None


def _classify(part: str) -> dict | None:
    """What one comma-separated piece of a parenthetical says, or None if any
    part of it is something else (so it isn't metadata)."""
    out: dict = {"number": None, "condition": False, "finish": None, "treatments": []}
    for token in (t.strip() for t in part.split(" - ")):
        key = token.lower()
        if not token:
            return None
        if _NUMBER.match(token) and any(c.isdigit() for c in token):
            out["number"] = token
        elif key in ("non-foil", "nonfoil", "non foil"):
            pass
        elif key in _CONDITIONS:
            out["condition"] = True
            out["cond_value"] = _CONDITIONS[key]
        elif _finish_of(token) and all(w in _TREATMENT_WORDS for w in key.replace("-", " ").split()):
            out["finish"] = _finish_of(token)
        elif all(w in _TREATMENT_WORDS for w in key.split()):
            out["treatments"].append(token)
        else:
            return None
    return out


@dataclass
class _Hints:
    name: str
    number: str | None = None
    finish: str | None = None
    condition: str | None = None
    set_name: str | None = None
    treatments: list[str] = field(default_factory=list)


def _clean_name(text: str, *, allow_set: bool) -> _Hints:
    """Split trailing hints off a card name: `*F*`/`*E*`, " - Foil", and a
    trailing parenthetical. With allow_set the unrecognized remainder of the
    parenthetical is the set name (plain list); otherwise a parenthetical
    holding anything unrecognized stays in the name (a card name can end in one)."""
    hints = _Hints(name=text.strip())
    for _ in range(4):
        name = hints.name
        if m := _MARKER.search(name):
            hints.finish = hints.finish or ("etched" if m.group(1) == "E" else "foil")
            hints.name = name[: m.start()].strip()
            continue
        if m := _FINISH_SUFFIX.search(name):
            hints.finish = hints.finish or _finish_of(m.group(1))
            hints.name = name[: m.start()].strip()
            continue
        m = _PAREN.search(name)
        if not m or not name[: m.start()].strip():
            break
        parts = [p.strip() for p in m.group(1).split(", ")]
        peeled = []
        while parts and (info := _classify(parts[-1])) is not None:
            peeled.append(info)
            parts.pop()
        remainder = ", ".join(parts)
        if remainder and not allow_set:
            break
        if not peeled and not (remainder and allow_set):
            break
        for info in peeled:
            hints.number = hints.number or info["number"]
            hints.finish = hints.finish or info["finish"]
            if info["condition"] and info["cond_value"]:
                hints.condition = info["cond_value"]
            hints.treatments.extend(info["treatments"])
        if remainder:
            hints.set_name = hints.set_name or remainder
        hints.name = name[: m.start()].strip()
    return hints


# --- amounts ---------------------------------------------------------------------

_AMOUNT = re.compile(r"^\$?\s*(\d[\d,]*(?:\.\d{1,2})?)$")


def _cents(text: str, line: int, what: str) -> int:
    m = _AMOUNT.match(text.strip())
    if not m:
        raise CartParseError(f"line {line}: can't read the {what} {text.strip()!r}")
    try:
        return parse_cents(m.group(1).replace(",", ""))
    except (ArithmeticError, ValueError):
        raise CartParseError(f"line {line}: can't read the {what} {text.strip()!r}") from None


def _qty(text: str, line: int, name: str) -> int:
    text = text.strip()
    if not text.isdigit() or not 1 <= int(text) <= MAX_QTY:
        raise CartParseError(f"line {line}: quantity {text!r} for {name}")
    return int(text)


def _condition(text: str, line: int) -> str | None:
    key = text.strip().lower()
    if key in ("", "-"):
        return None
    if key in _CONDITIONS:
        return _CONDITIONS[key]
    raise CartParseError(f"line {line}: unknown condition {text.strip()!r}")


def _build(
    line: int, raw: str, hints: _Hints, qty: int, unit: int, *, set_name: str | None = None,
    condition: str | None = None, finish: str | None = None, alternatives=(), extra=(),
) -> CartLine:
    if not hints.name:
        raise CartParseError(f"line {line}: no card name")
    return CartLine(
        line=line, raw=raw.strip(), name=hints.name, qty=qty, unit_cents=unit, total_cents=qty * unit,
        set_name=hints.set_name or set_name or None, set_alternatives=tuple(alternatives),
        collector_number=hints.number, finish=hints.finish or finish or "nonfoil",
        condition=hints.condition or condition, treatments=tuple(hints.treatments), extra=tuple(extra),
    )


# --- the three formats -------------------------------------------------------------

def _parse_csv(text_lines: list[tuple[int, str]]) -> list[CartLine]:
    reader = csv.reader(io.StringIO("\n".join(t for _, t in text_lines)))
    header = [h.strip().lower() for h in next(reader)]

    def col(*names: str) -> int | None:
        return next((header.index(n) for n in names if n in header), None)

    c_name, c_set = col("card name", "name"), col("set / edition", "set", "edition")
    c_cond, c_qty, c_price = col("condition", "cond"), col("quantity", "qty"), col("price")
    if c_name is None or c_qty is None or c_price is None:
        raise CartParseError("line 1: the CSV needs Card Name, Quantity and Price columns")
    lines = []
    for record in reader:
        number = text_lines[reader.line_num - 1][0] if reader.line_num <= len(text_lines) else reader.line_num
        if not any(cell.strip() for cell in record):
            continue

        def get(i: int | None, record=record) -> str:
            return record[i].strip() if i is not None and i < len(record) else ""

        name = get(c_name)
        if not name:
            raise CartParseError(f"line {number}: no card name")
        hints = _clean_name(name, allow_set=False)
        set_cell = get(c_set)
        finish = _finish_of(set_cell)
        if finish:  # "Some Set (Foil)": the finish is not part of the set's name
            set_cell = re.sub(r"\s*[-(]*\s*(foil[ -]etched|etched(?: foil)?|foil)\)?\s*$", "", set_cell,
                              flags=re.I).strip()
        lines.append(_build(
            number, ",".join(record), hints, _qty(get(c_qty) or "1", number, name),
            _cents(get(c_price), number, "price"), set_name=set_cell or None,
            condition=_condition(get(c_cond), number), finish=finish,
        ))
    return lines


_CELL_SPLIT = re.compile(r"\t+|\s{2,}")


def _splits(description: str) -> list[tuple[str, str]]:
    """Every way to cut "Set: Card" at a colon, the last colon first."""
    cuts = [m.start() for m in re.finditer(r": ", description)]
    return [(description[:i].strip(), description[i + 2:].strip()) for i in reversed(cuts)]


def _parse_checkout(text_lines: list[tuple[int, str]]) -> list[CartLine]:
    lines = []
    for number, text in text_lines[1:]:
        if not text.strip():
            continue
        cells = [c.strip() for c in _CELL_SPLIT.split(text.strip())]
        if len(cells) == 5:  # no Stock column
            cells.insert(3, "")
        if len(cells) != 6:
            raise CartParseError(
                f"line {number}: expected Description, Cond, Qty, Stock, Price, Total;"
                f" got {len(cells)} columns"
            )
        description, cond, qty_text, _stock, price_text, total_text = cells
        splits = _splits(description)
        set_name, card = splits[0] if splits else (None, description)
        hints = _clean_name(card, allow_set=False)
        qty = _qty(qty_text, number, card)
        unit, total = _cents(price_text, number, "price"), _cents(total_text, number, "total")
        if total != qty * unit:
            raise CartParseError(
                f"line {number}: {card}: total {total_text} isn't {qty} x {price_text}"
            )
        lines.append(_build(
            number, text, hints, qty, unit, set_name=set_name, condition=_condition(cond, number),
            alternatives=[(s, _clean_name(c, allow_set=False).name) for s, c in splits[1:]],
        ))
    return lines


_LIST_LINE = re.compile(
    r"^(?P<qty>\d+)\s*x?\s+(?P<name>.+?)\s*,\s*\$?(?P<price>\d[\d,]*(?:\.\d{1,2})?)"
    r"(?:\s+(?P<kind>each|ea\.?|total))?$", re.I)
_TOTAL_LINE = re.compile(
    r"^TOTAL:\s*(?P<items>\d+)\s+items?\s*,\s*\$?(?P<sub>\d[\d,]*\.\d\d)\s+subtotal\b.*$", re.I)


def _parse_list(text_lines: list[tuple[int, str]]) -> list[CartLine]:
    lines = []
    declared: tuple[int, int, int] | None = None
    for number, text in text_lines:
        stripped = text.strip()
        if not stripped:
            continue
        if stripped.upper().startswith("TOTAL:"):
            m = _TOTAL_LINE.match(stripped)
            if not m:
                raise CartParseError(f"line {number}: can't read the TOTAL line {stripped!r}")
            declared = (number, int(m["items"]), _cents(m["sub"], number, "subtotal"))
            continue
        m = _LIST_LINE.match(stripped)
        if not m:
            raise CartParseError(f"line {number}: can't read {stripped!r} (expected '2 Card Name, $1.25')")
        qty = _qty(m["qty"], number, m["name"])
        amount = _cents(m["price"], number, "price")
        if (m["kind"] or "").lower() == "total":
            if amount % qty:
                raise CartParseError(f"line {number}: {amount} cents doesn't split evenly over {qty} copies")
            amount //= qty
        hints = _clean_name(m["name"], allow_set=True)
        lines.append(_build(number, stripped, hints, qty, amount))
    if declared is not None:
        number, items, subtotal = declared
        copies, got = sum(line.qty for line in lines), sum(line.total_cents for line in lines)
        if items != copies:
            raise CartParseError(f"line {number}: TOTAL says {items} items but the list has {copies}")
        if subtotal != got:
            raise CartParseError(
                f"line {number}: TOTAL says {subtotal} cents subtotal but the lines add up to {got} cents"
            )
    return lines


def parse(text: str) -> ParsedCart:
    """Lines of a cart in any of the three formats. Raises CartParseError,
    naming the line, on anything it can't read."""
    numbered = [(n, t) for n, t in enumerate(text.lstrip("﻿").splitlines(), start=1)]
    first = next(((n, t) for n, t in numbered if t.strip()), None)
    if first is None:
        raise CartParseError("the cart is empty")
    numbered = [(n, t) for n, t in numbered if n >= first[0]]
    head = first[1].strip().lower()
    if "," in head and "card name" in [c.strip() for c in head.split(",")]:
        fmt, lines = "store CSV", _parse_csv(numbered)
    elif re.match(r"^description\s+cond", head):
        fmt, lines = "checkout", _parse_checkout(numbered)
    else:
        fmt, lines = "list", _parse_list(numbered)
    if not lines:
        raise CartParseError("no cards found in the cart")
    if len(lines) > MAX_LINES or sum(line.qty for line in lines) > MAX_COPIES:
        raise CartParseError(f"more than {MAX_LINES} lines or {MAX_COPIES} cards; split the cart")
    return ParsedCart(format=fmt, lines=lines)
