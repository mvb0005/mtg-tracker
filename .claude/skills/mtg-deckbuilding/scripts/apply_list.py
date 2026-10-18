"""Change a deck on the site to match a target list (or explicit pairs), as ONE
new version, through the JSON API: a dry run first (printed), then the real
call with expect_version, then a check that the deck matches.

    apply_list.py DECK_ID --target LIST [--note TEXT] [--plan]
    apply_list.py DECK_ID --pairs PAIRS.json [--note TEXT] [--plan]   # {"adds": [...], "cuts": [...]}

Adds are queued by name as planned ("to acquire") changes, so nothing is moved
out of another deck. Each cut removes one entry of this deck's list by name (a
wanted card first, then an owned copy), never another deck's. The server
refuses if the deck changed since it was read (version_conflict) or has
unapplied Modify-page changes (pending_changes). Every real change is also
covered by the server's restore point and write log. Config: see _api.py.
"""
import argparse
import collections
import sys

import _api
from _common import key, read_list


def current_counts(deck: dict) -> tuple[collections.Counter, dict]:
    """(Counter by front-face key, display names) of the deck's list: owned
    copies plus wanted cards."""
    counts, names = collections.Counter(), {}
    for entry in (*deck["cards"], *deck["wants"]):
        counts[key(entry["name"])] += 1
        names[key(entry["name"])] = entry["name"]
    return counts, names


def diff_lists(current, cnames, target, tnames) -> dict:
    """The swaps that turn `current` into `target` (Counters by key, with
    display names): {"adds": [...], "cuts": [...]}, paired one to one. Raises
    ValueError if the card counts differ, since swaps keep the size."""
    if sum(target.values()) != sum(current.values()):
        raise ValueError(f"target has {sum(target.values())} cards, deck has {sum(current.values())}:"
                         " add/cut counts must match")
    return {"adds": [tnames[k] for k, q in (target - current).items() for _ in range(q)],
            "cuts": [cnames[k] for k, q in (current - target).items() for _ in range(q)]}


def request_body(plan: dict, note: str, version: int) -> dict:
    """The POST /decks/{id}/changes body: names (or {"lot_id"}/{"want_id"}
    dicts, as given) for adds and cuts."""
    def entry(item):
        return item if isinstance(item, dict) else {"name": item}
    return {"adds": [entry(a) for a in plan["adds"]], "cuts": [entry(c) for c in plan["cuts"]],
            "note": note, "expect_version": version}


def describe(item) -> str:
    return item if isinstance(item, str) else ", ".join(f"{k}={v}" for k, v in item.items())


def print_result(result: dict) -> None:
    print(f"  version {result['previous_version']} -> {result['version']}")
    print(f"  added:   {', '.join(result['added']) or '-'}")
    print(f"  removed: {', '.join(result['removed']) or '-'}")
    counts = result["counts"]
    print(f"  after:   {counts['here']} here, {counts['away']} away, {counts['wanted']} wanted")


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("deck_id", type=int)
    ap.add_argument("--target", help="decklist to match")
    ap.add_argument("--pairs", help="JSON with adds/cuts instead of a target")
    ap.add_argument("--plan", action="store_true", help="print the dry run and stop")
    ap.add_argument("--note", default="Updated with the deckbuilding skill")
    args = ap.parse_args(argv)
    path = f"/decks/{args.deck_id}"

    deck = _api.get(path)
    if deck["pending_changes"]:
        sys.exit("the deck already has pending changes -- apply or remove them on the Modify page first")
    target = None
    if args.pairs:
        import json
        with open(args.pairs) as f:
            plan = json.load(f)
    elif args.target:
        target, tnames, _ = read_list(args.target)
        current, cnames = current_counts(deck)
        try:
            plan = diff_lists(current, cnames, target, tnames)
        except ValueError as exc:
            sys.exit(str(exc))
    else:
        sys.exit("give --target LIST or --pairs JSON")
    if len(plan["cuts"]) > len(plan["adds"]):
        sys.exit("more cuts than adds: every cut needs an add to replace it")
    print(f"{len(plan['adds'])} adds, {len(plan['cuts'])} cuts (deck version {deck['version']})")
    for i, add in enumerate(plan["adds"]):
        cut = plan["cuts"][i] if i < len(plan["cuts"]) else None
        print(f"  + {describe(add)}" + (f"   - {describe(cut)}" if cut is not None else ""))
    if not plan["adds"]:
        return

    body = request_body(plan, args.note, deck["version"])
    print("dry run:")
    print_result(_api.post(f"{path}/changes", body, params={"dry_run": 1}))
    if args.plan:
        return
    print("applied:")
    print_result(_api.post(f"{path}/changes", body))
    if target is not None:
        live, _ = current_counts(_api.get(path))
        print("deck now matches the target:", live == target)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except _api.ApiError as exc:
        sys.exit(f"apply_list: {exc}")
