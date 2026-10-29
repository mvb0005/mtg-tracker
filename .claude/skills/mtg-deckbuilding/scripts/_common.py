"""Shared helpers for the deckbuilding scripts (run from the repo root with
.venv/bin/python so `mtgtracker` imports)."""
import collections
import gzip
import json
import os
import re
import sys
import time

import httpx

from mtgtracker import scryfall
from mtgtracker.importers.decklist_text import parse
from mtgtracker.money import parse_cents

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


# Prices come from Scryfall's daily "Default Cards" bulk file (every printing,
# https://scryfall.com/docs/api/bulk-data), boiled down to the cheapest paper
# USD per name and refreshed at most once a day (prices only change daily).
# Only a name the file doesn't know falls back to the search API, paced by
# scryfall._send (2 searches a second, a 30s stop after any 429).
PRICES_MAX_AGE = 24 * 3600
NOT_CARDS = {"art_series", "token", "double_faced_token", "emblem", "planar", "scheme", "vanguard"}
_prices: dict | None = None


def _paper_printing(card: dict) -> bool:
    return ("paper" in card.get("games", ()) and card.get("layout") not in NOT_CARDS
            and not card.get("oversized") and card.get("set_type") != "memorabilia")


def bulk_dir():
    """In a subdirectory: cached_cards() reads every CACHE_DIR/*.json as cards."""
    return scryfall.CACHE_DIR / "bulk"


def _build_price_index(client: httpx.Client, path) -> dict:
    meta = scryfall._send(client, "GET", "https://api.scryfall.com/bulk-data/default-cards")
    meta.raise_for_status()
    info = meta.json()
    url = info["jsonl_download_uri"]  # on data.scryfall.io: no rate limit
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / "default-cards.jsonl.gz.part"
    size = info.get("compressed_size", 0) // 1_000_000
    print(f"Downloading Scryfall's daily card data ({size} MB) for prices...", file=sys.stderr)
    with client.stream("GET", url, timeout=300) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in resp.iter_bytes():
                f.write(chunk)
    names: dict[str, float] = {}
    faces: dict[str, float] = {}
    with gzip.open(tmp, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip().rstrip(",")
            if not line.startswith("{"):
                continue
            card = json.loads(line)
            usd = (card.get("prices") or {}).get("usd")
            if not usd or not _paper_printing(card):
                continue
            usd = float(usd)
            name = scryfall._normalize(card["name"])
            names[name] = min(usd, names.get(name, usd))
            for face in card.get("card_faces") or []:
                fname = scryfall._normalize(face["name"])
                faces[fname] = min(usd, faces.get(fname, usd))
    tmp.unlink()
    index = {"updated_at": info.get("updated_at"), "names": names, "faces": faces}
    part = path.with_suffix(".part")
    part.write_text(json.dumps(index))
    os.replace(part, path)
    return index


def price_index(client: httpx.Client) -> dict:
    """{names, faces}: cheapest paper USD (nonfoil) per normalized name,
    from the bulk file, rebuilt when over a day old. Empty if the download
    fails, so callers fall back to the search API."""
    global _prices
    if _prices is None:
        path = bulk_dir() / "prices.json"
        try:
            if path.exists() and time.time() - path.stat().st_mtime < PRICES_MAX_AGE:
                _prices = json.loads(path.read_text())
            else:
                _prices = _build_price_index(client, path)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            print(f"Couldn't get Scryfall's bulk prices ({exc}); searching card by card.", file=sys.stderr)
            _prices = {"names": {}, "faces": {}}
    return _prices


def cheapest_usd(client: httpx.Client, name: str) -> float | None:
    """Cheapest paper USD price over all printings (nonfoil). The full name
    is matched before a face name, so a card never takes the price of a
    double-faced card that shares one face's name."""
    index = price_index(client)
    norm = scryfall._normalize(name)
    for table in (index["names"], index["faces"]):
        if norm in table:
            return table[norm]
    for attempt in range(2):
        try:
            r = scryfall._send(client, "GET", "https://api.scryfall.com/cards/search",
                               params={"q": f'!"{name}" game:paper', "order": "usd", "unique": "prints"})
            break
        except scryfall.RateLimited as exc:
            if attempt:
                raise
            print(f"Scryfall asked us to slow down; waiting {exc.retry_after}s.", file=sys.stderr)
            time.sleep(exc.retry_after)
    if r.status_code != 200:
        return None
    usd = [float(p["prices"]["usd"]) for p in r.json().get("data", []) if p["prices"].get("usd")]
    return min(usd) if usd else None


def to_cents(dollars: float | None) -> int | None:
    """A Scryfall USD price (two decimals) as integer cents."""
    return None if dollars is None else parse_cents(f"{dollars:.2f}")


def scryfall_client() -> httpx.Client:
    return httpx.Client(timeout=20, headers={"User-Agent": scryfall.USER_AGENT, "Accept": "application/json"})
