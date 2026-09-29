"""Parse plain-text decklists, as exported by Moxfield, Archidekt, MTG Arena
and most deck sites:

    1 Academy Ruins
    1x Sol Ring
    1 Sol Ring (CMM) 400
    1 Sol Ring (CMM) 400 *F*
    Commander
    1 Shuri, Wakandan Inventor
    Sideboard
    1 Some Card

- A leading quantity is optional ("Sol Ring" means 1), with or without "x".
- "(SET) number" pins a printing; "*F*" / "*E*" mark foil / etched.
- Section headers ("Commander", "Deck", "Sideboard", ...; with or without a
  trailing ":" or count) switch sections. Sideboard, maybeboard and similar
  sections are skipped, since they aren't part of the deck.
- Blank lines and "//" or "#" comments are ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_LINE_RE = re.compile(
    r"""^
    (?:(?P<qty>\d+)\s*[xX]?\s+)?          # optional quantity: "1 ", "1x "
    (?P<name>.+?)                          # card name (lazy, trimmed below)
    (?:\s+\((?P<set>[A-Za-z0-9]{2,6})\)    # optional (SET)
       (?:\s+(?P<number>[^\s*]+))?)?       #   and collector number
    (?:\s+\*(?P<finish>[FfEe])\*)?         # optional *F* / *E*
    \s*$""",
    re.VERBOSE,
)
_HEADER_RE = re.compile(r"^(?P<title>[A-Za-z][A-Za-z ]*?)\s*(?:\(\d+\))?\s*:?\s*$")

DECK_SECTIONS = {"commander", "commanders", "companion", "deck", "main", "mainboard", "main deck"}
SKIPPED_SECTIONS = {"sideboard", "maybeboard", "maybe", "considering", "tokens", "token", "attractions",
                    "stickers", "planes", "schemes"}


@dataclass
class Entry:
    qty: int
    name: str
    set_code: str | None = None
    collector_number: str | None = None
    finish: str = "nonfoil"


@dataclass
class ParsedList:
    entries: list[Entry] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # lines in sideboard-like sections
    errors: list[str] = field(default_factory=list)  # lines that couldn't be read

    @property
    def card_count(self) -> int:
        return sum(e.qty for e in self.entries)


def parse(text: str) -> ParsedList:
    result = ParsedList()
    skipping = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("//", "#")):
            continue
        header = _HEADER_RE.match(line)
        if header and header.group("title").strip().lower() in DECK_SECTIONS | SKIPPED_SECTIONS:
            skipping = header.group("title").strip().lower() in SKIPPED_SECTIONS
            continue
        if skipping:
            result.skipped.append(line)
            continue
        match = _LINE_RE.match(line)
        name = match.group("name").strip() if match else ""
        qty = int(match.group("qty")) if match and match.group("qty") else 1
        if not match or not name or qty < 1:
            result.errors.append(line)
            continue
        finish = {"f": "foil", "e": "etched"}.get((match.group("finish") or "").lower(), "nonfoil")
        result.entries.append(Entry(
            qty=qty, name=name,
            set_code=match.group("set").lower() if match.group("set") else None,
            collector_number=match.group("number"),
            finish=finish,
        ))
    return result
