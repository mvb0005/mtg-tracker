"""Deck export texts (read-only; friends can use them too)."""

from __future__ import annotations

import re

from mtgtracker.services import NotFoundError
from mtgtracker.web import queries


def export_text(detail: dict, kind: str) -> tuple[str, str]:
    """(text, download filename) for one export of a deck: "decklist",
    "shopping" or "borrow-{friend id}". `detail` is queries.get_deck(...)."""
    exports = queries.deck_exports(detail)
    texts = {"decklist": exports["decklist"], "shopping": exports["shopping"]}
    texts |= {f"borrow-{b['id']}": b["text"] for b in exports["borrow"]}
    if kind not in texts:
        raise NotFoundError("No such export")
    name = re.sub("['\\u2019]", "", detail["deck"]["name"].lower())  # "Heliod's" -> "heliods"
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-") or "deck"
    return texts[kind], f"{slug}-{kind}.txt"
