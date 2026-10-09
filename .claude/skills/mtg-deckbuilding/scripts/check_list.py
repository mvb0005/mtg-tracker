"""Check a Commander decklist: count, singleton, color identity, legality,
approximate slot counts (heuristic, from oracle text), curve, and pips.

    .venv/bin/python .claude/skills/mtg-deckbuilding/scripts/check_list.py LIST [--commander NAME]
"""
import argparse
import collections
import re

from _common import BASICS, read_list

from mtgtracker import scryfall

# Heuristic slot detectors over lowercased oracle text.
RAMP = (r"add \{|add one mana|add .{0,20}mana|treasure token"
        r"|search your library for .{0,60}(land|plains|island|swamp|mountain|forest)"
        r"|put a land card from your hand onto the battlefield")
DRAW = r"draws? (a|two|three|x|that many|\w+) cards?|draw cards"
WIPE = (r"destroy all|exile all (attacking )?creatures|return all .{0,30}(creatures|nonland)"
        r"|each creature .{0,20}(destroy|exile)")
INTERACT = (r"(destroy|exile|return) (another )?target "
            r"(creature|nonland permanent|permanent|artifact|enchantment|attacking)|counter target")
DEFENSE = r"hexproof|shroud|indestructible|protection from|phase out|can't attack you"
GRAVE = r"exile .{0,30}graveyard|exile all graveyards|exile each opponent's graveyard"

ap = argparse.ArgumentParser()
ap.add_argument("list")
ap.add_argument("--commander")
args = ap.parse_args()
counts, names, commander = read_list(args.list)
commander = args.commander or commander
keys = list(counts)
cards = dict(zip(keys, scryfall.fetch_collection([{"name": names[k]} for k in keys]), strict=True))
problems = [f"not found on Scryfall: {names[k]}" for k, c in cards.items() if c is None]
total = sum(counts.values())
if total != 100:
    problems.append(f"{total} cards, not 100")


def text(c):
    faces = [f.get("oracle_text") or "" for f in c.get("card_faces") or []]
    return " ".join([c.get("oracle_text") or "", *faces]).lower()


def front(c):
    return (c.get("card_faces") or [c])[0]


ckey = scryfall._normalize(commander.split(" // ")[0]) if commander else None
cmd = cards.get(ckey) if ckey else None
identity = set(cmd["color_identity"]) if cmd else None
if not cmd:
    problems.append("no commander given or found (use a Commander section or --commander)")
slots = collections.defaultdict(list)
curve, pips = collections.Counter(), collections.Counter()
for k, c in cards.items():
    if c is None:
        continue
    q, n, t, tl = counts[k], c["name"], text(c), front(c).get("type_line", "")
    if c["legalities"].get("commander") != "legal":
        problems.append(f"not Commander-legal: {n} ({c['legalities'].get('commander')})")
    if identity is not None and not set(c["color_identity"]) <= identity:
        problems.append(f"outside the commander's colors: {n} ({''.join(c['color_identity'])})")
    if q > 1 and n not in BASICS and "any number of cards named" not in t:
        problems.append(f"singleton: {q}x {n}")
    if "Land" in tl:
        slots["lands"] += [n] * q
        continue
    curve[min(int(c.get("cmc") or 0), 7)] += q
    for sym in re.findall(r"\{([WUBRG])\}", front(c).get("mana_cost") or ""):
        pips[sym] += q
    if "Creature" in tl:
        slots["creatures"].append(n)
        if "Flying" in (c.get("keywords") or []):
            slots["flying creatures"].append(n)
    if re.search(RAMP, t):
        slots["ramp"].append(n)
    if re.search(DRAW, t):
        slots["card draw"].append(n)
    if re.search(WIPE, t):
        slots["board wipes"].append(n)
    elif re.search(INTERACT, t):
        slots["targeted interaction"].append(n)
    if re.search(DEFENSE, t):
        slots["protection / defense"].append(n)
    if re.search(GRAVE, t):
        slots["graveyard hate"].append(n)
    if "no maximum hand size" in t:
        slots["no max hand size"].append(n)
for c in cards.values():
    if c and "no maximum hand size" in text(c) and "Land" in front(c).get("type_line", ""):
        slots["no max hand size"].append(c["name"])
shown_identity = "".join(sorted(identity)) if identity else "?"
print(f"commander: {cmd['name'] if cmd else '?'}  identity: {shown_identity}  cards: {total}")
targets = {"lands": "35-38 (33-34 if low curve + 10 ramp)", "ramp": "10+", "card draw": "10+",
           "targeted interaction": "8-10", "board wipes": "2-4", "protection / defense": "2-4",
           "graveyard hate": "1-3", "no max hand size": "1-2 if the deck draws a lot",
           "creatures": "", "flying creatures": ""}
for slot, goal in targets.items():
    v = slots[slot]
    print(f"\n{slot}: {len(v)}" + (f"   (typical {goal})" if goal else ""))
    if slot != "lands":
        print("  " + ", ".join(sorted(set(v))))
nonland = sum(curve.values())
avg = sum(mv * q for mv, q in curve.items()) / nonland if nonland else 0
low = sum(q for mv, q in curve.items() if mv <= 4)
print(f"\ncurve (7 = 7+): {dict(sorted(curve.items()))}  average mana value {avg:.2f}"
      f"  at 4 or less: {low}/{nonland}")
basics = {b: counts.get(scryfall._normalize(b), 0) for b in BASICS if counts.get(scryfall._normalize(b))}
print(f"colored pips: {dict(pips)}  basics: {basics}")
print("\nPROBLEMS:" if problems else "\nno rule problems", *problems, sep="\n  ")
print("\n(slot counts are heuristic from oracle text -- confirm borderline cards by hand)")
