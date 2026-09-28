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


def index_by_name(cards: list[dict]) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    for card in cards:
        index.setdefault(_normalize(card["name"]), []).append(card)
    return index


def find_matches(index: dict[str, list[dict]], name: str) -> list[dict]:
    return index.get(_normalize(name), [])
