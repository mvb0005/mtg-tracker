"""Who owns each card, and where those copies are -- from the site's API
(/api/v1/cards/owners), so no database snapshot is needed.

    .venv/bin/python .claude/skills/mtg-deckbuilding/scripts/who_owns.py NAME... | -
"""
import sys

import _api
from _common import key, read_names

NOT_IN_A_DECK = "not in a deck"
CHUNK = 40  # names per request, to keep the URL short


def lines(cards: list[dict]) -> list[str]:
    """One `Name: Owner: Q in Deck; ...` (or `nobody`) line per card."""
    out = []
    for card in cards:
        copies: dict[tuple[str, str], int] = {}
        for c in card["copies"]:
            where = (c["owner"], c["deck"] or NOT_IN_A_DECK)
            copies[where] = copies.get(where, 0) + c["qty"]
        text = "; ".join(f"{owner}: {q} in {deck}" for (owner, deck), q in sorted(copies.items()))
        out.append(f"{card['name']}: {text or 'nobody'}")
    return out


def main(argv: list[str]) -> None:
    wanted = {key(n): n for _, n in read_names(argv)}
    names = list(wanted.values())
    for i in range(0, len(names), CHUNK):
        for line in lines(_api.get("/cards/owners", name=names[i:i + CHUNK])["cards"]):
            print(line)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except _api.ApiError as exc:
        sys.exit(f"who_owns: {exc}")
