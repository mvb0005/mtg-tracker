"""Post card suggestions to a deck's Suggestions page, where the user compares
each against the card it would replace and accepts or dismisses it.

    .venv/bin/python .claude/skills/mtg-deckbuilding/scripts/suggest.py DECK_ID FILE.json [--dry-run]
    .venv/bin/python .claude/skills/mtg-deckbuilding/scripts/suggest.py DECK_ID --list

FILE.json (or - for stdin) is {"theme": "...", "suggestions": [{"name", "replaces", "reason"}, ...]};
a per-item "theme" overrides the file's. Prices are looked up on Scryfall
(cheapest paper USD) unless an item has "price_cents" or --no-prices is given.
--dry-run shows what the site would create and skip, and writes nothing.
--list prints every suggestion and its state, dismissed ones included, with
the user's vote (+ liked, - passed) and note: read it before suggesting,
never re-suggest a dismissed card, and treat the notes as their feedback.
Batches over the site's limit are posted in chunks of 50.
"""
import argparse
import json
import sys

import _api
from _common import cheapest_usd, scryfall_client, to_cents


def load(path: str) -> list[dict]:
    if path == "-":
        data = json.load(sys.stdin)
    else:
        with open(path) as f:
            data = json.load(f)
    theme = data.get("theme")
    items = []
    for item in data["suggestions"]:
        item = dict(item)
        if theme and not item.get("theme"):
            item["theme"] = theme
        items.append(item)
    return items


def add_prices(items: list[dict]) -> None:
    with scryfall_client() as client:
        for item in items:
            if item.get("price_cents") is None:
                item["price_cents"] = to_cents(cheapest_usd(client, item["name"]))


CHUNK = 50  # suggestions.MAX_BATCH on the site


def show(suggestions: list[dict]) -> None:
    for s in suggestions:
        cut = f" for {s['cut_card_name']}" if s["cut_card_name"] else ""
        price = f" ${s['price_cents'] / 100:.2f}" if s["price_cents"] is not None else ""
        vote = {"up": " +", "down": " -"}.get(s.get("vote") or "", "")
        note = f"  note: {s['vote_note']}" if s.get("vote_note") else ""
        print(f"[{s['state']}{vote}] {s['card_name']}{cut}{price}  ({s['theme'] or 'no theme'}){note}")


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("deck_id", type=int)
    ap.add_argument("file", nargs="?")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-prices", action="store_true")
    args = ap.parse_args(argv)
    path = f"/decks/{args.deck_id}/suggestions"
    if args.list or not args.file:
        show(_api.get(path)["suggestions"])
        return
    items = load(args.file)
    if not args.no_prices:
        add_prices(items)
    for start in range(0, len(items), CHUNK):
        chunk = items[start:start + CHUNK]
        result = _api.post(path, {"suggestions": chunk}, params={"dry_run": 1} if args.dry_run else None)
        print(("DRY RUN: " if args.dry_run else "") + result["notice"])
        for skip in result["skipped"]:
            print(f"  skipped {skip['name']}: {skip['reason']}")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except _api.ApiError as exc:
        sys.exit(f"suggest: {exc}")
