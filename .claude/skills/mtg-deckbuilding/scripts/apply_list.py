"""Change a deck on the site to match a target list (or explicit pairs), as ONE
new version, through the app's own Modify forms. Rehearse against a local
`mtg serve --db <snapshot>` first; back up with pull-db before running live.

    apply_list.py BASE DECK_ID --db SNAPSHOT --target LIST [--plan] [--note TEXT]
    apply_list.py BASE DECK_ID --pairs PAIRS.json [--note TEXT]      # {"adds": [...], "cuts": [...]}

Adds are always queued as planned ("to acquire") changes, so nothing is moved
out of another deck. Cuts remove only this deck's copy (wanted or owned).
"""
import argparse
import collections
import html
import json
import re
import sys
import time

import httpx
from _common import key, read_list

from mtgtracker import db
from mtgtracker.web import queries

ap = argparse.ArgumentParser()
ap.add_argument("base")
ap.add_argument("deck_id", type=int)
ap.add_argument("--db", help="snapshot of the target site's DB (for the diff)")
ap.add_argument("--target", help="decklist to match")
ap.add_argument("--pairs", help="JSON with adds/cuts instead of a target")
ap.add_argument("--plan", action="store_true", help="print the swaps and stop")
ap.add_argument("--note", default="Updated with the deckbuilding skill")
args = ap.parse_args()
base, deck = args.base.rstrip("/"), args.deck_id

if args.pairs:
    with open(args.pairs) as f:
        plan = json.load(f)
else:
    if not (args.db and args.target):
        sys.exit("--target needs --db (a snapshot to diff against)")
    target, tnames, _ = read_list(args.target)
    current, cnames = collections.Counter(), {}
    detail = queries.get_deck(db.connect(args.db), deck, viewer_id=1)
    for g in detail["groups"]:
        current[key(g["card_name"])] += g["qty"]
        cnames[key(g["card_name"])] = g["card_name"]
    if sum(target.values()) != sum(current.values()):
        sys.exit(f"target has {sum(target.values())} cards, deck has {sum(current.values())}:"
                 " add/cut counts must match")
    plan = {"adds": [tnames[k] for k, q in (target - current).items() for _ in range(q)],
            "cuts": [cnames[k] for k, q in (current - target).items() for _ in range(q)]}
if len(plan["adds"]) != len(plan["cuts"]):
    sys.exit("adds and cuts must pair up one to one")
print(f"{len(plan['adds'])} swaps")
for a, c in zip(plan["adds"], plan["cuts"], strict=True):
    print(f"  + {a}   - {c}")
if args.plan or not plan["adds"]:
    sys.exit(0)

client = httpx.Client(headers={"Origin": base}, timeout=90)
page = lambda: client.get(f"{base}/decks/{deck}/modify").text  # noqa: E731


def changes(text):
    out = []
    for block in re.split(r'<li class="change', text)[1:]:
        cid = re.search(r'id="change-(\d+)"', block)
        card = re.search(r"data-card='([^']*)'", block)
        if cid and card:
            tail = block.split("Replaces", 1)[1].split('class="change-actions', 1)[0]
            name = json.loads(html.unescape(card.group(1)))["name"]
            out.append((int(cid.group(1)), name, "data-card=" in tail))
    return out


WANT_RE = re.compile(r'name="cut_want_id" value="(\d+)"[^>]*aria-label="Replace ([^"]*) \(not owned\)"')
LOT_RE = re.compile(r'name="cut_lot_id" value="(\d+)" class="pick-tile" aria-label="Replace ([^"]*)"')


def pick(text, name):
    """The cut-picker field for this deck's copy of `name`: a wanted card
    first, otherwise an owned copy on the list."""
    for field, pattern in (("cut_want_id", WANT_RE), ("cut_lot_id", LOT_RE)):
        for value, label in pattern.findall(text):
            if html.unescape(label) == name:
                return {field: value}
    return None


if changes(page()):
    sys.exit("the deck already has pending changes -- apply or remove them first")
for name in plan["adds"]:
    for attempt in range(5):
        r = client.post(f"{base}/decks/{deck}/changes", data={"planned_name": name})
        err = httpx.URL(r.headers.get("location", "")).params.get("error")
        if not err or "reach Scryfall" not in err:
            break
        time.sleep(5 * (attempt + 1))  # Scryfall rate limit: back off
    if err:
        sys.exit(f"add failed: {name}: {err} (remove the queued changes on the Modify page before retrying)")
    time.sleep(0.5)
for (cid, added, _), cut in zip(changes(page()), plan["cuts"], strict=True):
    data = pick(page(), cut)
    if data is None:
        sys.exit(f"no {cut!r} on the list to pair with {added}")
    r = client.post(f"{base}/decks/{deck}/changes/{cid}/cut", data=data)
    err = httpx.URL(r.headers.get("location", "")).params.get("error")
    if err:
        sys.exit(f"cut failed: {added} replacing {cut}: {err}")
if not all(has for *_, has in changes(page())):
    sys.exit("not every change has a replacement -- check the Modify page")
r = client.post(f"{base}/decks/{deck}/apply", data={"note": args.note})
params = httpx.URL(r.headers.get("location", "")).params
print("apply:", params.get("notice") or params.get("error"))
if args.target:
    live = collections.Counter()
    for line in client.get(f"{base}/decks/{deck}/export/decklist.txt").text.splitlines():
        m = re.match(r"^(\d+) (.+?)(?: \([A-Z0-9]+\) \S+)?(?: \*[FE]\*)?$", line)
        if m:
            live[key(m.group(2))] += int(m.group(1))
    print("deck now matches the target:", live == target)
