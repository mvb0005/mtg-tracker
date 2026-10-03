"""Import a collection export (CSV) as cards a user owns.

Understands ManaBox, Moxfield and Archidekt exports, and any CSV with a
name column and (optionally) a quantity column. Columns are matched by
header name, case-insensitively, so column order doesn't matter.

Each copy becomes one lot (one lot = one physical card) owned by the
target user, with no transaction: a collection import isn't a purchase.
When the file has a purchase price, that copy's cost basis is that price
(private to its owner, like all money). Cards are resolved on Scryfall by
the most exact thing the file gives -- Scryfall ID, then set + collector
number, then name -- and a card that can't be resolved still gets a lot,
honestly unpinned (scryfall_id NULL), for the printings queue.

A file is imported once per user (by sha256); importing it again is an
error rather than a duplicate collection.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from mtgtracker import scryfall
from mtgtracker.money import parse_cents

MAX_ROWS = 20_000
MAX_COPIES_PER_ROW = 1_000

# Header aliases, lowercased. First match wins.
COLUMNS = {
    "name": ("name", "card name", "card"),
    "qty": ("quantity", "count", "qty"),
    "set": ("set code", "edition code", "edition", "set"),
    "number": ("collector number", "collector_number", "card number", "number"),
    "finish": ("foil", "finish", "printing"),
    "scryfall_id": ("scryfall id", "scryfall_id", "scryfall uuid"),
    "price": ("purchase price", "purchase_price", "price"),
    "currency": ("purchase price currency", "currency"),
    "added": ("added", "date added", "last modified"),
    "condition": ("condition",),
    "language": ("language",),
    "proxy": ("proxy",),
}

NONFOIL = {"", "normal", "nonfoil", "non-foil", "false", "no", "0", "n"}
FOIL = {"foil", "true", "yes", "1", "y", "f", "traditional foil"}
ETCHED = {"etched", "foil etched", "etched foil", "e"}
CONDITION_OK = {"", "near_mint", "near mint", "nm", "m", "mint"}
LANGUAGE_OK = {"", "en", "english"}


class CollectionImportError(ValueError):
    """The file can't be imported; nothing was written."""


@dataclass(frozen=True)
class Row:
    line: int
    name: str
    qty: int
    set_code: str | None
    collector_number: str | None
    finish: str
    scryfall_id: str | None
    price_cents: int | None
    acquired_date: str
    notes: str | None


@dataclass
class ParsedFile:
    format: str
    rows: list[Row]


@dataclass
class ImportSummary:
    import_id: int
    format: str
    copies: int
    distinct: int
    priced: int
    cost_cents: int
    unresolved: list[str] = field(default_factory=list)

    def message(self) -> str:
        parts = [f"Imported {self.copies} card{'s' if self.copies != 1 else ''}"
                 f" ({self.distinct} distinct) from a {self.format} export."]
        if self.unresolved:
            shown = ", ".join(self.unresolved[:5]) + ("…" if len(self.unresolved) > 5 else "")
            parts.append(
                f"{len(self.unresolved)} couldn't be matched on Scryfall and need a printing: {shown}."
            )
        return " ".join(parts)


def _detect_format(headers: set[str]) -> str:
    if "manabox id" in headers:
        return "ManaBox"
    if "tradelist count" in headers:
        return "Moxfield"
    if "edition code" in headers or "edition name" in headers:
        return "Archidekt"
    return "CSV"


def _finish(raw: str, line: int) -> str:
    value = raw.strip().lower()
    if value in NONFOIL:
        return "nonfoil"
    if value in ETCHED:
        return "etched"
    if value in FOIL:
        return "foil"
    raise CollectionImportError(f"line {line}: unknown foil/finish value {raw!r}")


def parse(text: str) -> ParsedFile:
    """Rows of a collection CSV. Raises CollectionImportError, naming the
    line, on anything it can't read -- an import is all or nothing."""
    text = text.lstrip("﻿")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise CollectionImportError("the file is empty") from None
    lowered = [h.strip().lower() for h in header]
    index: dict[str, int] = {}
    for key, aliases in COLUMNS.items():
        for alias in aliases:
            if alias in lowered:
                index[key] = lowered.index(alias)
                break
    if "name" not in index:
        raise CollectionImportError("no Name column found -- is this a collection CSV?")
    fmt = _detect_format(set(lowered))
    today = date.today().isoformat()

    rows: list[Row] = []
    for line, record in enumerate(reader, start=2):
        if not any(cell.strip() for cell in record):
            continue
        if len(rows) >= MAX_ROWS:
            raise CollectionImportError(f"more than {MAX_ROWS} rows; split the file")

        def get(key: str, record=record) -> str:
            i = index.get(key)
            return record[i].strip() if i is not None and i < len(record) else ""

        name = get("name")
        if not name:
            raise CollectionImportError(f"line {line}: no card name")
        qty_raw = get("qty") or "1"
        if not qty_raw.isdigit() or not 1 <= int(qty_raw) <= MAX_COPIES_PER_ROW:
            raise CollectionImportError(f"line {line}: quantity {qty_raw!r} for {name}")

        price_cents = None
        price_raw = get("price").lstrip("$")
        currency = get("currency").upper()
        if price_raw and currency in ("", "USD"):
            try:
                price_cents = parse_cents(price_raw)
            except (ArithmeticError, ValueError):
                raise CollectionImportError(f"line {line}: price {price_raw!r} for {name}") from None
            if price_cents < 0:
                raise CollectionImportError(f"line {line}: negative price for {name}")

        notes = []
        if get("condition").lower() not in CONDITION_OK:
            notes.append(f"Condition: {get('condition')}")
        if get("language").lower() not in LANGUAGE_OK:
            notes.append(f"Language: {get('language')}")
        if get("proxy").lower() in ("true", "yes", "1"):
            notes.append("Proxy")
        if price_raw and price_cents is None:
            notes.append(f"Purchase price: {price_raw} {currency}")

        added = get("added")[:10]
        try:
            acquired = date.fromisoformat(added).isoformat() if added else today
        except ValueError:
            acquired = today

        rows.append(Row(
            line=line, name=name, qty=int(qty_raw),
            set_code=get("set").lower() or None, collector_number=get("number") or None,
            finish=_finish(get("finish"), line), scryfall_id=get("scryfall_id").lower() or None,
            price_cents=price_cents, acquired_date=acquired, notes="; ".join(notes) or None,
        ))
    if not rows:
        raise CollectionImportError("no cards found in the file")
    return ParsedFile(format=fmt, rows=rows)


def _identifier(row: Row) -> dict:
    if row.scryfall_id:
        return {"id": row.scryfall_id}
    if row.set_code and row.collector_number:
        return {"set": row.set_code, "collector_number": row.collector_number}
    return {"name": row.name}


def _resolve(rows: list[Row]) -> list[dict | None]:
    """Scryfall card (or None) per row: one batched, cache-first lookup by
    each row's most exact identifier, then a by-name lookup for misses that
    had an id or set + number (a stale id or an odd collector number), and
    finally a fuzzy retry for up to 10 remaining misses."""
    cards = scryfall.fetch_collection([_identifier(r) for r in rows])
    retry = [i for i, (r, c) in enumerate(zip(rows, cards, strict=True))
             if c is None and _identifier(r).keys() != {"name"}]
    if retry:
        for i, card in zip(retry, scryfall.fetch_collection([{"name": rows[i].name} for i in retry]),
                           strict=True):
            cards[i] = card
    misses = 0
    for i, row in enumerate(rows):
        if cards[i] is None and misses < 10:
            misses += 1
            with contextlib.suppress(scryfall.CardLookupError):
                cards[i] = scryfall.fetch_named_card(row.name)
    return cards


def import_collection(
    conn: sqlite3.Connection, data: bytes, *, owner_id: int, imported_by: int,
    filename: str | None = None,
) -> ImportSummary:
    """Import a collection CSV for owner_id. Parses and resolves everything
    first (so a bad row or a Scryfall outage writes nothing), then writes
    the import record and every lot in one transaction."""
    sha256 = hashlib.sha256(data).hexdigest()
    if conn.execute(
        "SELECT 1 FROM collection_imports WHERE owner_id = ? AND sha256 = ?", (owner_id, sha256)
    ).fetchone():
        raise CollectionImportError("that file was already imported into this collection")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise CollectionImportError("the file isn't UTF-8 text -- export it as CSV") from None
    parsed = parse(text)
    cards = _resolve(parsed.rows)

    lots = []
    unresolved: list[str] = []
    for row, card in zip(parsed.rows, cards, strict=True):
        if card is None:
            unresolved.append(row.name)
            values = (None, row.name, None, None)
        else:
            values = (card["id"], card["name"], card["set"], card["collector_number"])
        lot = (owner_id, *values, row.finish, row.price_cents, row.acquired_date, row.notes)
        lots.extend([lot] * row.qty)

    now = datetime.now(UTC).isoformat()
    with conn:
        cur = conn.execute(
            "INSERT INTO collection_imports (owner_id, sha256, original_filename, format, card_count,"
            " imported_by, imported_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (owner_id, sha256, filename, parsed.format, len(lots), imported_by, now),
        )
        import_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO lots (owner_id, scryfall_id, card_name, set_code, collector_number, finish,"
            " allocated_cost_cents, acquired_date, notes, collection_import_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(*lot, import_id) for lot in lots],
        )
    priced = [lot[6] for lot in lots if lot[6] is not None]
    return ImportSummary(
        import_id=import_id, format=parsed.format, copies=len(lots),
        distinct=len({lot[2] for lot in lots}), priced=len(priced), cost_cents=sum(priced),
        unresolved=sorted(set(unresolved)),
    )
