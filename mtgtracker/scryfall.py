"""On-demand Scryfall lookups, scoped to a single set at a time.

This is deliberately not the daily "default_cards" bulk-data pipeline from
the original brief (descoped for v1, no live pricing). It fetches a set's
printings via the search API (paginated, per Scryfall's API guidelines: a
descriptive User-Agent, no per-card calls, a short sleep between pages) and
caches the result locally so repeated runs don't re-hit the network.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx

USER_AGENT = "mtgtracker/0.1 (personal collection tracker, local use only)"
CACHE_DIR = Path(__file__).parent.parent / ".scryfall_cache"


def _normalize(name: str) -> str:
    name = name.casefold()
    name = re.sub(r"[^a-z0-9 ]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def fetch_set_cards(set_code: str, *, use_cache: bool = True) -> list[dict]:
    """All printings in a set, via the search API (unique=prints)."""
    cache_path = CACHE_DIR / f"{set_code}.json"
    if use_cache and cache_path.exists():
        return json.loads(cache_path.read_text())

    cards: list[dict] = []
    url = f"https://api.scryfall.com/cards/search?q=e%3A{set_code}&unique=prints&order=set"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}

    with httpx.Client(timeout=15) as client:
        while url:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            cards.extend(data["data"])
            url = data.get("next_page") if data.get("has_more") else None
            if url:
                time.sleep(0.1)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cards))
    return cards


class CardLookupError(Exception):
    """A by-name lookup found no card, or the name matches several."""


def fetch_named_card(name: str) -> dict:
    """One card by (fuzzy) name via /cards/named -- an explicit user action,
    e.g. planning a card you don't own. The result is cached as
    CACHE_DIR/cards/<id>.json, a single card object, which cached_cards()
    includes (so it gets an image and type) but which is never a "set" for
    cached_set_codes(). Raises CardLookupError for no match or an ambiguous
    name; other HTTP failures propagate as httpx errors."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    with httpx.Client(timeout=15) as client:
        resp = client.get("https://api.scryfall.com/cards/named", params={"fuzzy": name}, headers=headers)
    if resp.status_code == 404:
        error = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if error.get("type") == "ambiguous":
            raise CardLookupError(f"'{name}' matches several cards; be more specific")
        raise CardLookupError(f"no card named '{name}'")
    resp.raise_for_status()
    card = resp.json()
    _cache_single(card)
    return card


def _cache_single(card: dict) -> None:
    cards_dir = CACHE_DIR / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    (cards_dir / f"{card['id']}.json").write_text(json.dumps(card))


def _satisfies(card: dict, ident: dict) -> bool:
    if "id" in ident:
        return card["id"] == ident["id"]
    if "set" in ident:
        return card["set"] == ident["set"] and card["collector_number"] == ident["collector_number"]
    wanted = _normalize(ident["name"])
    names = {_normalize(card["name"])} | {_normalize(f["name"]) for f in card.get("card_faces") or []}
    return wanted in names


def fetch_collection(identifiers: list[dict]) -> list[dict | None]:
    """Resolve many cards at once via POST /cards/collection (75 per
    request, a short pause between batches, per Scryfall's guidelines).
    Each identifier is {"id": <scryfall id>}, {"set": ..., "collector_number":
    ...} or {"name": ...}. Exact identifiers (id, set + number) already in the
    cache are answered from it; only the rest go to Scryfall. Returns one card
    (or None if not found) per identifier, in order, and caches every card
    found like fetch_named_card does. An explicit user action only (e.g.
    importing a list) -- never called on page load."""
    by_id: dict[str, dict] = {}
    by_number: dict[tuple[str, str], dict] = {}
    for card in cached_cards():
        by_id[card["id"]] = card
        by_number[(card["set"], card["collector_number"])] = card
    results: list[dict | None] = []
    missing: list[int] = []
    for i, ident in enumerate(identifiers):
        if "id" in ident:
            results.append(by_id.get(ident["id"]))
        elif "set" in ident:
            results.append(by_number.get((ident["set"], ident["collector_number"])))
        else:
            results.append(None)
        if results[-1] is None:
            missing.append(i)

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    with httpx.Client(timeout=30) as client:
        for start in range(0, len(missing), 75):
            if start:
                time.sleep(0.1)
            batch = missing[start:start + 75]
            resp = client.post("https://api.scryfall.com/cards/collection",
                               json={"identifiers": [identifiers[i] for i in batch]}, headers=headers)
            resp.raise_for_status()
            found = resp.json().get("data", [])
            for card in found:
                _cache_single(card)
            for i in batch:
                results[i] = next((c for c in found if _satisfies(c, identifiers[i])), None)
    return results


_cached_cards: dict[str, object] = {}


def cached_cards() -> list[dict]:
    """Every card already cached, never hitting the network: each set in
    CACHE_DIR/*.json plus single cards in CACHE_DIR/cards/*.json (from
    fetch_named_card). Parsed once and reused until a cache file is added or
    changes."""
    set_files = sorted(CACHE_DIR.glob("*.json"))
    card_files = sorted((CACHE_DIR / "cards").glob("*.json"))
    key = tuple((str(p), p.stat().st_mtime_ns) for p in (*set_files, *card_files))
    if _cached_cards.get("key") != key:
        cards = [card for p in set_files for card in json.loads(p.read_text())]
        cards += [json.loads(p.read_text()) for p in card_files]
        _cached_cards["cards"] = cards
        _cached_cards["key"] = key
    return _cached_cards["cards"]  # type: ignore[return-value]


def index_by_name(cards: list[dict]) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    for card in cards:
        index.setdefault(_normalize(card["name"]), []).append(card)
    return index


def find_matches(index: dict[str, list[dict]], name: str) -> list[dict]:
    return index.get(_normalize(name), [])
