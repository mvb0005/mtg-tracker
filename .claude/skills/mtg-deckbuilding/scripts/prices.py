"""Cheapest paper USD printing for each card, the total, and anything over a cap.

    .venv/bin/python .claude/skills/mtg-deckbuilding/scripts/prices.py [--cap 5] NAME...
    curl -s .../export/shopping.txt | .venv/bin/python .../prices.py --cap 5 -
"""
import argparse

from _common import BASICS, cheapest_usd, read_names, scryfall_client

ap = argparse.ArgumentParser()
ap.add_argument("--cap", type=float)
ap.add_argument("names", nargs="+")
args = ap.parse_args()
rows, total, unknown = [], 0.0, []
with scryfall_client() as client:
    for qty, name in read_names(args.names):
        price = 0.10 if name in BASICS else cheapest_usd(client, name)
        if price is None:
            unknown.append(name)
            continue
        rows.append((price, qty, name))
        total += price * qty
for price, qty, name in sorted(rows, reverse=True):
    flag = "  OVER CAP" if args.cap is not None and price > args.cap else ""
    print(f"{qty:>2} {name:40} ${price:7.2f}{flag}")
print(f"\n{sum(q for _, q, _ in rows)} cards, about ${total:.2f} at the cheapest printings")
if unknown:
    print("no price found (rate-limited or misspelled?):", ", ".join(unknown))
