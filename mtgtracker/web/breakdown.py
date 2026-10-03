"""Deck breakdown: mana curve, mana colors and card types, computed from a
deck's display groups (queries.get_deck rows, each with `qty`, `category`
and `art`). Counts are physical cards on the list, so 34 Islands count 34.

- Mana curve: non-land cards by mana value. Cards whose mana value isn't
  cached are left out and reported as `uncounted`.
- Mana colors: non-land cards, each counted once -- mono-colored cards under
  their color, multicolored cards under their exact combination ("Blue-Red",
  drawn as stripes of both), colorless under Colorless -- so the segments add
  up to the cards counted.
- Card types: every card under its deck-list section (the commander under
  its own type).

Colors are fixed per entity, never by rank: each mana color has its own
hue, and each card type a fixed categorical slot, ordered so the types a
typical deck has sit in adjacent validated slots.
"""

from __future__ import annotations

from mtgtracker import cardinfo

LANDS = "Lands"

COLOR_NAMES = {"W": "White", "U": "Blue", "B": "Black", "R": "Red", "G": "Green"}

# Card type -> categorical slot (1-8); "Other" uses the neutral gray. Kept
# for a categorical view; the current charts color by mana instead.
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


def _color_key(colors: tuple[str, ...] | None) -> str:
    """A card's color bucket: its color(s) in WUBRG order ("R", "UR"),
    C(olorless), or X when its colors aren't cached."""
    if colors is None:
        return "X"
    key = "".join(c for c in cardinfo.WUBRG if c in colors)
    return key or "C"


def _label(key: str) -> str:
    if key == "X":
        return "Unknown colors"
    if key == "C":
        return "Colorless"
    return "-".join(COLOR_NAMES[c] for c in key)


def _fill(key: str) -> str:
    """CSS background for a bucket: the validated --mana-* hue for one
    color; diagonal stripes of each color for a combination (a Blue-Red card
    reads as both); gray for colorless; hatched gray for unknown."""
    if key == "X":
        return "repeating-linear-gradient(45deg, var(--mana-C) 0 3px, transparent 3px 6px)"
    if len(key) == 1:
        return f"var(--mana-{key})"
    stops = ", ".join(f"var(--mana-{c}) {5 * i}px {5 * (i + 1)}px" for i, c in enumerate(key))
    return f"repeating-linear-gradient(135deg, {stops})"


def _order(key: str) -> tuple:
    """Single colors in WUBRG order, then combinations (fewest colors
    first), then colorless, then unknown."""
    if key == "X":
        return (3,)
    if key == "C":
        return (2,)
    indices = tuple(cardinfo.WUBRG.index(c) for c in key)
    return (0, indices) if len(key) == 1 else (1, len(key), indices)


def _buckets(counts: dict[str, int]) -> list[dict]:
    total = sum(counts.values())
    return [
        {"key": key, "label": _label(key), "fill": _fill(key), "count": counts[key],
         "pct": 100 * counts[key] / total if total else 0}
        for key in sorted(counts, key=_order)
        if counts[key]
    ]


def deck_breakdown(groups: list[dict], identity: tuple[str, ...] = ()) -> dict:
    """See the module docstring. With two or more deck colors (`identity`),
    the mana-curve and card-type bars are also split by color (`stacked`),
    using the same buckets and fills as the mana-colors chart."""
    curve_colors: dict[int, dict[str, int]] = {}
    color_counts: dict[str, int] = {}
    type_colors: dict[str, dict[str, int]] = {}
    uncounted = 0

    for group in groups:
        qty, art, category = group["qty"], group["art"], group["category"]
        key = _color_key(art.colors)
        by_type = type_colors.setdefault(category, {})
        by_type[key] = by_type.get(key, 0) + qty
        if category == LANDS:
            continue
        if art.cmc is None:
            uncounted += qty
        else:
            by_value = curve_colors.setdefault(int(art.cmc), {})
            by_value[key] = by_value.get(key, 0) + qty
        if key != "X":
            color_counts[key] = color_counts.get(key, 0) + qty

    curve = []
    if curve_colors:
        totals = {v: sum(c.values()) for v, c in curve_colors.items()}
        tallest = max(totals.values())
        start = 0 if 0 in curve_colors else 1
        curve = [
            {
                "value": v,
                "count": totals.get(v, 0),
                "pct": 100 * totals.get(v, 0) / tallest,
                "segments": _buckets(curve_colors.get(v, {})),  # bottom-up
            }
            for v in range(start, max(curve_colors) + 1)
        ]
    curve_totals: dict[str, int] = {}
    for by_value in curve_colors.values():
        for key, n in by_value.items():
            curve_totals[key] = curve_totals.get(key, 0) + n

    type_totals = {category: sum(c.values()) for category, c in type_colors.items()}
    top = max(type_totals.values(), default=0)
    order = [*cardinfo.SECTION_ORDER]
    types = [
        {
            "key": category, "label": category, "count": type_totals[category],
            "pct": 100 * type_totals[category] / sum(type_totals.values()),
            "bar_pct": 100 * type_totals[category] / top,
            "slot": TYPE_SLOTS.get(category),
            "segments": _buckets(type_colors[category]),
        }
        for category in sorted(type_totals, key=lambda c: (TYPE_SLOTS.get(c, 99), order.index(c)
                                                           if c in order else 99))
    ]
    type_legend: dict[str, int] = {}
    for by_type in type_colors.values():
        for key, n in by_type.items():
            type_legend[key] = type_legend.get(key, 0) + n

    curve_legend = _buckets(curve_totals)
    return {
        "curve": curve,
        "stacked": len(identity) >= 2 and len(curve_legend) >= 2,
        "curve_legend": curve_legend,
        "curve_total": sum(curve_totals.values()),
        "uncounted": uncounted,
        "colors": _buckets(color_counts),
        "types": types,
        "type_legend": _buckets(type_legend),
        "type_total": sum(type_totals.values()),
    }
