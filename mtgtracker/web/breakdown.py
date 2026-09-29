"""Deck breakdown: mana curve, mana colors and card types, computed from a
deck's display groups (queries.get_deck rows, each with `qty`, `category`
and `art`). Counts are physical cards on the list, so 34 Islands count 34.

- Mana curve: non-land cards by mana value. Cards whose mana value isn't
  cached are left out and reported as `uncounted`.
- Mana colors: non-land cards, each counted once -- mono-colored cards under
  their color, multicolored under Multicolor, colorless under Colorless --
  so the segments add up to the cards counted.
- Card types: every card under its deck-list section (the commander under
  its own type).

Colors are fixed per entity, never by rank: each mana color has its own
hue, and each card type a fixed categorical slot, ordered so the types a
typical deck has sit in adjacent validated slots.
"""

from __future__ import annotations

from mtgtracker import cardinfo

LANDS = "Lands"

COLOR_SEGMENTS = (
    ("W", "White"),
    ("U", "Blue"),
    ("B", "Black"),
    ("R", "Red"),
    ("G", "Green"),
    ("M", "Multicolor"),
    ("C", "Colorless"),
)

# Card type -> categorical slot (1-8); "Other" uses the neutral gray.
TYPE_SLOTS = {
    "Creatures": 1,
    "Instants": 2,
    "Sorceries": 3,
    "Artifacts": 4,
    "Enchantments": 5,
    "Lands": 6,
    "Planeswalkers": 7,
    "Battles": 8,
}


def _segments(counts: dict[str, int], order: list[tuple[str, str]]) -> list[dict]:
    total = sum(counts.values())
    return [
        {"key": key, "label": label, "count": counts[key], "pct": 100 * counts[key] / total}
        for key, label in order
        if counts.get(key)
    ]


def deck_breakdown(groups: list[dict]) -> dict:
    curve_counts: dict[int, int] = {}
    color_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    uncounted = 0

    for group in groups:
        qty, art, category = group["qty"], group["art"], group["category"]
        type_counts[category] = type_counts.get(category, 0) + qty
        if category == LANDS:
            continue

        if art.cmc is None:
            uncounted += qty
        else:
            value = int(art.cmc)
            curve_counts[value] = curve_counts.get(value, 0) + qty

        if art.colors is not None:
            key = "C" if not art.colors else (art.colors[0] if len(art.colors) == 1 else "M")
            color_counts[key] = color_counts.get(key, 0) + qty

    curve = []
    if curve_counts:
        tallest = max(curve_counts.values())
        start = 0 if 0 in curve_counts else 1
        curve = [
            {"value": v, "count": curve_counts.get(v, 0), "pct": 100 * curve_counts.get(v, 0) / tallest}
            for v in range(start, max(curve_counts) + 1)
        ]

    type_order = sorted(
        ((label, label) for label in type_counts),
        key=lambda item: (TYPE_SLOTS.get(item[0], 99), cardinfo.SECTION_ORDER.index(item[0])
                          if item[0] in cardinfo.SECTION_ORDER else 99),
    )
    types = _segments(type_counts, type_order)
    for segment in types:
        segment["slot"] = TYPE_SLOTS.get(segment["key"])

    return {
        "curve": curve,
        "curve_total": sum(curve_counts.values()),
        "uncounted": uncounted,
        "colors": _segments(color_counts, list(COLOR_SEGMENTS)),
        "types": types,
        "type_total": sum(type_counts.values()),
    }
