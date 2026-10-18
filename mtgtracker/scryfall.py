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


def name_keys(card_name: str) -> set[str]:
    """A card name's normalized form and its front face's ("A // B" -> "a b",
    "a"), so a double-faced card matches however a list or a lot spells it."""
    return {_normalize(card_name), _normalize(card_name.split("//")[0])}


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
    invalidate_cache()
    return cards


SETS_MAX_AGE = 24 * 3600  # a refresh is only offered for a cache older than this


def fetch_sets(*, refresh: bool = False) -> list[dict]:
    """Every Scryfall set (/sets: code, name, ...), cached as
    CACHE_DIR/meta/sets.json. It lives in meta/ because cached_cards() and the
    printings queue treat every CACHE_DIR/*.json as a list of cards. A cached
    list is reused; refresh=True re-fetches it, but only when the cache is
    older than SETS_MAX_AGE (so an unknown set name can't hammer Scryfall)."""
    path = CACHE_DIR / "meta" / "sets.json"
    if path.exists() and not (refresh and time.time() - path.stat().st_mtime > SETS_MAX_AGE):
        return json.loads(path.read_text())
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    with httpx.Client(timeout=15) as client:
        resp = client.get("https://api.scryfall.com/sets", headers=headers)
    resp.raise_for_status()
    sets = [{"code": s["code"], "name": s["name"], "set_type": s.get("set_type")}
            for s in resp.json()["data"]]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sets))
    return sets


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
    invalidate_cache()


def _satisfies(card: dict, ident: dict) -> bool:
    if "id" in ident:
        return card["id"] == ident["id"]
    if "set" in ident:
        return card["set"] == ident["set"] and card["collector_number"] == ident["collector_number"]
    wanted = _normalize(ident["name"])
    return wanted in _names(card)


# Pause between per-card fallback requests, and the 429 back-off schedule.
# Both go through _pause so tests can patch it out.
FALLBACK_PAUSE = 0.3
RETRY_DELAYS = (1, 2, 4)


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def _names(card: dict) -> set[str]:
    return {_normalize(card["name"])} | {_normalize(f["name"]) for f in card.get("card_faces") or []}


def _fetch_one(client: httpx.Client, ident: dict, headers: dict) -> dict | None:
    """One card by identifier via the per-card endpoints. None for a 404; a
    429 is retried after RETRY_DELAYS, then raised like any other HTTP error."""
    if "id" in ident:
        url, params = f"https://api.scryfall.com/cards/{ident['id']}", None
    elif "set" in ident:
        url, params = f"https://api.scryfall.com/cards/{ident['set']}/{ident['collector_number']}", None
    else:
        url, params = "https://api.scryfall.com/cards/named", {"fuzzy": ident["name"]}
    for delay in (*RETRY_DELAYS, None):
        resp = client.get(url, params=params, headers=headers)
        if resp.status_code != 429 or delay is None:
            break
        _pause(delay)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _fetch_each(
    client: httpx.Client, idents: list[dict], headers: dict, original: Exception,
) -> list[dict | None]:
    """Fallback for a batch the collection endpoint failed: one GET per
    identifier, in order. If Scryfall can't be reached at all, raises
    `original` (the batch's own failure) rather than a per-card error."""
    out: list[dict | None] = []
    reached = False
    for n, ident in enumerate(idents):
        if n:
            _pause(FALLBACK_PAUSE)
        try:
            card = _fetch_one(client, ident, headers)
        except httpx.TransportError:
            if not reached:
                raise original from None
            raise
        reached = True
        if card is not None:
            _cache_single(card)
        out.append(card)
    return out


def fetch_collection(identifiers: list[dict]) -> list[dict | None]:
    """Resolve many cards at once via POST /cards/collection (75 per
    request, a short pause between batches, per Scryfall's guidelines).
    Each identifier is {"id": <scryfall id>}, {"set": ..., "collector_number":
    ...} or {"name": ...}. Anything already in the cache is answered from it
    (a name matches the first cached printing, any one will do); only the rest
    go to Scryfall. If the collection POST times out, fails to connect or
    returns a 5xx, that batch is resolved one card at a time instead (GET
    /cards/{id}, /cards/{set}/{number}, /cards/named?fuzzy=), pausing between
    requests and backing off on 429; if Scryfall can't be reached at all, the
    POST's own exception is raised. Returns one card (or None if not found)
    per identifier, in order, and caches every card found like
    fetch_named_card does. An explicit user action only (e.g. importing a
    list) -- never called on page load."""
    by_id: dict[str, dict] = {}
    by_number: dict[tuple[str, str], dict] = {}
    by_name: dict[str, dict] = {}
    for card in cached_cards():
        by_id[card["id"]] = card
        by_number[(card["set"], card["collector_number"])] = card
        for name in _names(card):
            by_name.setdefault(name, card)
    results: list[dict | None] = []
    missing: list[int] = []
    for i, ident in enumerate(identifiers):
        if "id" in ident:
            results.append(by_id.get(ident["id"]))
        elif "set" in ident:
            results.append(by_number.get((ident["set"], ident["collector_number"])))
        else:
            results.append(by_name.get(_normalize(ident["name"])))
        if results[-1] is None:
            missing.append(i)

    if not missing:
        return results
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    with httpx.Client(timeout=30) as client:
        for start in range(0, len(missing), 75):
            if start:
                time.sleep(0.1)
            batch = missing[start:start + 75]
            idents = [identifiers[i] for i in batch]
            try:
                resp = client.post("https://api.scryfall.com/cards/collection",
                                   json={"identifiers": idents}, headers=headers)
                if resp.status_code >= 500:
                    resp.raise_for_status()
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                for i, card in zip(batch, _fetch_each(client, idents, headers, e), strict=True):
                    results[i] = card
                continue
            resp.raise_for_status()
            found = resp.json().get("data", [])
            for card in found:
                _cache_single(card)
            for i in batch:
                results[i] = next((c for c in found if _satisfies(c, identifiers[i])), None)
    return results


_cached_cards: dict[str, object] = {}

# How often cached_cards() re-checks every cache file's mtime. In between it
# only stats the two cache directories, whose mtimes change whenever a file
# is added or removed. A page does hundreds of card lookups, and statting
# every one of hundreds of cached cards per lookup made pages take seconds.
FULL_CHECK_SECONDS = 5.0


def _dir_mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def invalidate_cache() -> None:
    """Make the next cached_cards() call re-check every file (called after
    this process writes to the cache)."""
    _cached_cards.pop("checked_at", None)


def cached_cards() -> list[dict]:
    """Every card already cached, never hitting the network: each set in
    CACHE_DIR/*.json plus single cards in CACHE_DIR/cards/*.json (from
    fetch_named_card). Parsed once and reused until a cache file is added or
    changes: added/removed files are seen at once (directory mtimes), a file
    rewritten in place by another process within FULL_CHECK_SECONDS."""
    cheap = (str(CACHE_DIR), _dir_mtime(CACHE_DIR), _dir_mtime(CACHE_DIR / "cards"))
    now = time.monotonic()
    if (
        "cards" in _cached_cards and _cached_cards.get("cheap") == cheap
        and now - _cached_cards.get("checked_at", -FULL_CHECK_SECONDS) < FULL_CHECK_SECONDS  # type: ignore[operator]
    ):
        return _cached_cards["cards"]  # type: ignore[return-value]
    set_files = sorted(CACHE_DIR.glob("*.json"))
    card_files = sorted((CACHE_DIR / "cards").glob("*.json"))
    key = tuple((str(p), p.stat().st_mtime_ns) for p in (*set_files, *card_files))
    if _cached_cards.get("key") != key:
        cards = [card for p in set_files for card in json.loads(p.read_text())]
        cards += [json.loads(p.read_text()) for p in card_files]
        _cached_cards["cards"] = cards
        _cached_cards["key"] = key
    _cached_cards["cheap"] = cheap
    _cached_cards["checked_at"] = now
    return _cached_cards["cards"]  # type: ignore[return-value]


def index_by_name(cards: list[dict]) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    for card in cards:
        index.setdefault(_normalize(card["name"]), []).append(card)
    return index


def find_matches(index: dict[str, list[dict]], name: str) -> list[dict]:
    return index.get(_normalize(name), [])
