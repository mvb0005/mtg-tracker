"""Shared helpers for the deckbuilding scripts (run from the repo root with
.venv/bin/python so `mtgtracker` imports)."""
import collections
import re
import sys
import time

import httpx

from mtgtracker import scryfall
from mtgtracker.importers.decklist_text import parse

BASICS = {"Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes"}


def key(name: str) -> str:
    """Normalized front-face name, the way decks and lots are matched."""
    return scryfall._normalize(re.sub(r"\s*\(.*\)$", "", name).split(" // ")[0])


def read_names(args: list[str]) -> list[tuple[int, str]]:
    """(qty, name) from args, or from stdin when args == ['-']. Lines may be
    'N Name', 'Name', or a cart line 'N Name, $1.23'."""
    lines = sys.stdin.read().splitlines() if args == ["-"] else args
    out = []
    for line in lines:
        line = re.sub(r",\s*\$[\d.]+\s*$", "", line.strip())
        if not line or line.lower() in ("commander", "deck", "sideboard"):
            continue
        m = re.match(r"^(\d+)x?\s+(.+)$", line)
        out.append((int(m.group(1)), m.group(2)) if m else (1, line))
    return out


def read_list(path: str) -> tuple[collections.Counter, dict, str | None]:
    """A decklist file -> (Counter by key, display names, commander name)."""
    with open(path) as f:
        parsed = parse(f.read())
    counts, names, commander = collections.Counter(), {}, None
    for e in parsed.entries:
        counts[key(e.name)] += e.qty
        names[key(e.name)] = e.name
        if e.commander:
            commander = e.name
    return counts, names, commander


def cheapest_usd(client: httpx.Client, name: str) -> float | None:
    """Cheapest paper USD price over all printings; backs off on 429."""
    for attempt in range(6):
        r = client.get("https://api.scryfall.com/cards/search",
                       params={"q": f'!"{name}" game:paper', "order": "usd", "unique": "prints"})
        if r.status_code != 429:
            break
        time.sleep(2 * (attempt + 1))
    time.sleep(0.3)
    if r.status_code != 200:
        return None
    usd = [float(p["prices"]["usd"]) for p in r.json().get("data", []) if p["prices"].get("usd")]
    return min(usd) if usd else None


def scryfall_client() -> httpx.Client:
    return httpx.Client(timeout=20, headers={"User-Agent": scryfall.USER_AGENT, "Accept": "application/json"})
