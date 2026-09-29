"""Faceted sorting and filtering for the deck and cards pages (image and
list views), shown as a shopping-site style sidebar.

Everything comes from GET query params, so it works without JS and a
filtered view can be bookmarked. Each facet takes any number of values
(?color=U&color=R). Values within a facet are OR'd -- except color with
color_mode=all, which requires every ticked color -- and facets are AND'd.
Unknown values are dropped.

- color: W/U/B/R/G match cards containing that color, M multicolored, C no
  colors (lands included). Cards whose colors aren't cached never match.
- type: every card type on the type line, so an Artifact Creature matches
  both Artifacts and Creatures (display sections still use one type each).
- mv: mana value 0-6, or 7+.
- deck: a deck id, or "none" for cards in no deck (cards page only).

Facet counts follow the usual shop convention: each option's count is how
many cards you'd see with that option ticked, given every *other* facet's
current selection.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from urllib.parse import quote, urlencode

from mtgtracker import cardinfo

SORTS = {
    "mv": "Mana value: high to low",
    "mv-asc": "Mana value: low to high",
    "name": "Name",
    "cost": "Cost basis: high to low",
}
DEFAULT_SORT = "mv"

COLORS = {
    "W": "White",
    "U": "Blue",
    "B": "Black",
    "R": "Red",
    "G": "Green",
    "M": "Multicolor",
    "C": "Colorless",
}

MANA_VALUES = {**{str(v): str(v) for v in range(7)}, "7+": "7+"}

COLOR_MODES = {"any": "Any", "all": "All"}
DEFAULT_COLOR_MODE = "any"

NO_DECK = "none"
FACETS = ("color", "type", "mv", "deck")


def _mv_bucket(cmc: float | None) -> str | None:
    if cmc is None:
        return None
    return "7+" if cmc >= 7 else str(int(cmc))


def _color_keys(colors: tuple[str, ...] | None) -> set[str]:
    """Color facet options a card satisfies."""
    if colors is None:
        return set()
    keys = set(colors)
    if len(colors) > 1:
        keys.add("M")
    if not colors:
        keys.add("C")
    return keys


def _type_keys(row: dict) -> set[str]:
    return row["art"].type_labels(row["card_name"])


def _deck_key(row: dict) -> str:
    current = row.get("current_deck_id")
    return NO_DECK if current is None else str(current)


def _keep(values: list[str] | tuple[str, ...], allowed) -> tuple[str, ...]:
    seen = []
    for value in values:
        if value in allowed and value not in seen:
            seen.append(value)
    return tuple(seen)


@dataclass(frozen=True)
class CardFilters:
    sort: str = DEFAULT_SORT
    color_mode: str = DEFAULT_COLOR_MODE
    color: tuple[str, ...] = ()
    type: tuple[str, ...] = ()
    mv: tuple[str, ...] = ()
    deck: tuple[str, ...] = ()  # cards page only

    @classmethod
    def parse(
        cls, sort: str = "", color=(), type=(), mv=(), deck=(), deck_ids: set[int] | None = None,
        color_mode: str = "",
    ) -> CardFilters:
        allowed_decks = {NO_DECK, *(str(d) for d in (deck_ids or ()))}
        return cls(
            sort=sort if sort in SORTS else DEFAULT_SORT,
            color_mode=color_mode if color_mode in COLOR_MODES else DEFAULT_COLOR_MODE,
            color=_keep(color, COLORS),
            type=_keep(type, cardinfo.SECTION_ORDER),
            mv=_keep(mv, MANA_VALUES),
            deck=_keep(deck, allowed_decks),
        )

    @property
    def active(self) -> bool:
        """Whether any filter (not just a sort) narrows the cards."""
        return any(getattr(self, facet) for facet in FACETS)

    @property
    def active_count(self) -> int:
        return sum(len(getattr(self, facet)) for facet in FACETS)

    def params(self) -> dict[str, list[str]]:
        """Non-default values, for building links that keep the current view."""
        values: dict[str, list[str]] = {}
        if self.sort != DEFAULT_SORT:
            values["sort"] = [self.sort]
        if self.color_mode != DEFAULT_COLOR_MODE:
            values["color_mode"] = [self.color_mode]
        for facet in FACETS:
            if getattr(self, facet):
                values[facet] = list(getattr(self, facet))
        return values

    def cleared(self) -> CardFilters:
        """Same sort and color mode, no filters."""
        return CardFilters(sort=self.sort, color_mode=self.color_mode)

    def href(self, path: str, **extra: str | None) -> str:
        """path plus these filters and any extra params (e.g. q, view)."""
        params = {**self.params(), **{k: [v] for k, v in extra.items() if v}}
        return f"{path}?{urlencode(params, doseq=True, quote_via=quote)}" if params else path

    def _facet_ok(self, facet: str, row: dict) -> bool:
        selected = getattr(self, facet)
        if not selected:
            return True
        if facet == "color":
            keys = _color_keys(row["art"].colors)
            if self.color_mode == "all":
                return set(selected) <= keys
            return bool(keys & set(selected))
        if facet == "type":
            return bool(_type_keys(row) & set(selected))
        if facet == "mv":
            return _mv_bucket(row["art"].cmc) in selected
        if facet == "deck":
            return _deck_key(row) in selected
        raise ValueError(facet)

    def matches(self, row: dict, *, ignore: str | None = None) -> bool:
        return all(self._facet_ok(facet, row) for facet in FACETS if facet != ignore)

    def facet_counts(self, rows: list[dict]) -> dict[str, dict[str, int]]:
        """{facet: {option: card count}} -- each facet counted with the other
        facets applied but not itself. Counts are physical cards (qty)."""
        counts: dict[str, dict[str, int]] = {facet: {} for facet in FACETS}
        for facet in FACETS:
            for row in rows:
                if not self.matches(row, ignore=facet):
                    continue
                if facet == "color":
                    keys = _color_keys(row["art"].colors)
                    # With "all", ticking an option adds a requirement: count
                    # it only where every already-ticked color also holds.
                    if self.color_mode == "all" and not set(self.color) <= keys:
                        continue
                elif facet == "type":
                    keys = _type_keys(row)
                elif facet == "mv":
                    bucket = _mv_bucket(row["art"].cmc)
                    keys = {bucket} if bucket else set()
                else:
                    keys = {_deck_key(row)}
                for key in keys:
                    counts[facet][key] = counts[facet].get(key, 0) + row["qty"]
        return counts

    def sort_key(self, row: dict) -> tuple:
        name, art = row["card_name"], row["art"]
        if self.sort == "mv-asc":
            return cardinfo.mana_value_order(name, art, descending=False)
        if self.sort == "name":
            return (name.casefold(),)
        if self.sort == "cost":
            return (-(row.get("cost_cents") or 0), name.casefold())
        return cardinfo.mana_value_order(name, art)

    def apply(self, rows: list[dict]) -> list[dict]:
        return sorted((row for row in rows if self.matches(row)), key=self.sort_key)


assert {f.name for f in fields(CardFilters)} == {"sort", "color_mode", *FACETS}
