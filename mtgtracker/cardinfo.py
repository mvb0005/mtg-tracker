"""Display info (image, type) for cards, from the local Scryfall cache only.

Lots pinned to a printing (scryfall_id set) get that exact printing. Unpinned
lots (ambiguous precon cards, basics) get a representative printing of the
same name, flagged `exact=False` so the UI can say the printing isn't picked:
it prefers a printing that comes in the lot's finish and isn't a promo, so
an unpinned nonfoil Sol Ring shows the regular printing, not the foil-only
starter promo. Cards with no cached printing at all get no image.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mtgtracker import scryfall

BASIC_LAND_NAMES = ("Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes")

# Display sections, in deck-list order. A card lands in the first section
# whose type appears in its (front face) type line, so "Artifact Creature"
# is a Creature and "Artifact Land" is a Land.
CATEGORIES = (
    ("Creature", "Creatures"),
    ("Planeswalker", "Planeswalkers"),
    ("Battle", "Battles"),
    ("Instant", "Instants"),
    ("Sorcery", "Sorceries"),
    ("Land", "Lands"),
    ("Artifact", "Artifacts"),
    ("Enchantment", "Enchantments"),
)
OTHER = "Other"
SECTION_ORDER = [label for _, label in CATEGORIES] + [OTHER]


@dataclass(frozen=True)
class CardInfo:
    image: str | None
    type_line: str | None
    exact: bool  # True if this is the lot's own pinned printing
    cmc: float | None = None  # mana value; the same for every printing of a card
    # For the hover panel: one entry per face (name, mana_cost, type_line,
    # oracle_text, stats), plus printing details.
    faces: tuple[dict, ...] = ()
    rarity: str | None = None
    set_name: str | None = None
    artist: str | None = None
    # Card colors (Scryfall "colors", front face for double-faced cards):
    # e.g. ("R",), () for colorless; None when not cached.
    colors: tuple[str, ...] | None = None
    # Color identity (Scryfall "color_identity"): mana symbols anywhere on
    # the card, used for a deck's commander colors. None when not cached.
    color_identity: tuple[str, ...] | None = None
    # The back face's image, for double-faced cards (transform, modal,
    # reversible) -- whose faces each have their own image. None for
    # single-faced cards, and for split/adventure/flip cards, which print
    # both halves on the front.
    back_image: str | None = None

    def category(self, card_name: str) -> str:
        if self.type_line:
            front = self.type_line.split("//")[0]
            for type_word, label in CATEGORIES:
                if type_word in front:
                    return label
        if card_name in BASIC_LAND_NAMES:
            return "Lands"
        return OTHER

    def type_labels(self, card_name: str) -> set[str]:
        """Every card type on the (front face) type line, e.g. an Artifact
        Creature is {"Creatures", "Artifacts"}. `category` picks just one of
        these for display sections; filters use them all."""
        if self.type_line:
            front = self.type_line.split("//")[0].split("—")[0]
            labels = {label for type_word, label in CATEGORIES if type_word in front}
            if labels:
                return labels
        return {self.category(card_name)}


def image_url(card: dict) -> str | None:
    uris = card.get("image_uris") or ((card.get("card_faces") or [{}])[0].get("image_uris")) or {}
    return uris.get("normal") or uris.get("small")


def back_image_url(card: dict) -> str | None:
    faces = card.get("card_faces") or []
    if len(faces) < 2 or card.get("image_uris"):
        return None
    uris = faces[1].get("image_uris") or {}
    return uris.get("normal") or uris.get("small")


def _stats(face: dict) -> str | None:
    if face.get("power") is not None:
        return f"{face['power']}/{face['toughness']}"
    if face.get("loyalty") is not None:
        return f"Loyalty {face['loyalty']}"
    if face.get("defense") is not None:
        return f"Defense {face['defense']}"
    return None


def _info(card: dict, *, exact: bool) -> CardInfo:
    faces = card.get("card_faces") or [card]
    return CardInfo(
        image=image_url(card),
        back_image=back_image_url(card),
        type_line=card.get("type_line"),
        exact=exact,
        cmc=card.get("cmc"),
        faces=tuple(
            {
                "name": face.get("name"),
                "mana_cost": face.get("mana_cost") or "",
                "type_line": face.get("type_line"),
                "oracle_text": face.get("oracle_text") or "",
                "stats": _stats(face),
            }
            for face in faces
        ),
        rarity=card.get("rarity"),
        set_name=card.get("set_name"),
        artist=card.get("artist") or (faces[0].get("artist") if faces else None),
        colors=_colors(card),
        color_identity=tuple(card["color_identity"]) if card.get("color_identity") is not None else None,
    )


def _colors(card: dict) -> tuple[str, ...] | None:
    colors = card.get("colors")
    if colors is None and card.get("card_faces"):
        colors = card["card_faces"][0].get("colors")
    return tuple(colors) if colors is not None else None


def names(card: dict) -> set[str]:
    """Normalized names a card answers to: the full name and each face."""
    result = {scryfall._normalize(card["name"])}
    for face in card.get("card_faces") or []:
        result.add(scryfall._normalize(face["name"]))
    return result


class _Index:
    def __init__(self, cards: list[dict]):
        self.cards = cards
        self.by_id: dict[str, dict] = {}
        self.by_name: dict[str, list[dict]] = {}
        for card in cards:
            if card.get("digital"):
                continue
            self.by_id[card["id"]] = card
            for name in names(card):
                self.by_name.setdefault(name, []).append(card)


_index: _Index | None = None


def index() -> _Index:
    """Index over scryfall.cached_cards(), rebuilt when the cache changes."""
    global _index
    cards = scryfall.cached_cards()
    if _index is None or _index.cards is not cards:
        _index = _Index(cards)
    return _index


def printings_named(card_name: str) -> list[dict]:
    """Every cached paper printing of card_name (raw Scryfall dicts)."""
    return index().by_name.get(scryfall._normalize(card_name), [])


def lookup(scryfall_id: str | None, card_name: str, finish: str = "nonfoil") -> CardInfo:
    idx = index()
    if scryfall_id and (card := idx.by_id.get(scryfall_id)):
        return _info(card, exact=True)

    candidates = idx.by_name.get(scryfall._normalize(card_name), [])
    if not candidates:
        if card_name in BASIC_LAND_NAMES:
            # A basic land's facts are known even with no printing cached.
            return CardInfo(None, f"Basic Land — {card_name}", exact=False, cmc=0.0,
                            colors=(), color_identity=())
        return CardInfo(None, None, exact=False)
    best = min(
        candidates,
        key=lambda c: (
            finish not in c.get("finishes", []),
            bool(c.get("promo_types")),
            image_url(c) is None,
            c["set"],
            c["collector_number"],
        ),
    )
    return _info(best, exact=False)


_SYMBOL_RE = re.compile(r"\{([^}]+)\}")
_COLORS = "WUBRG"


def color_weight(mana_cost: str) -> tuple[int, int]:
    """(distinct colors, colored symbols) in a mana cost. Generic, X and {C}
    count as colorless; a hybrid symbol like {W/U} counts toward both colors.

    >>> color_weight("{1}{U}{R}"), color_weight("{1}{R}{R}"), color_weight("{3}")
    ((2, 2), (1, 2), (0, 0))
    """
    symbols = _SYMBOL_RE.findall(mana_cost or "")
    colored = [sym for sym in symbols if any(c in sym for c in _COLORS)]
    distinct = {c for sym in colored for c in sym if c in _COLORS}
    return len(distinct), len(colored)


WUBRG = "WUBRG"


def deck_identity(rows: list[dict], commander: str | None = None) -> tuple[str, ...]:
    """A deck's colors in WUBRG order, from display rows (each with
    `card_name`, `category`, `art`): the commander's color identity when it
    is known, else every color on the deck's non-land cards. () = colorless
    (or nothing cached)."""
    if commander:
        for row in rows:
            if row["card_name"] == commander and row["art"].color_identity is not None:
                return tuple(c for c in WUBRG if c in row["art"].color_identity)
    found: set[str] = set()
    for row in rows:
        if row["category"] != "Lands":
            found |= set(row["art"].color_identity or row["art"].colors or ())
    return tuple(c for c in WUBRG if c in found)


def mana_value_order(card_name: str, info: CardInfo, *, descending: bool = True) -> tuple:
    """Sort key: highest mana value first (lowest with descending=False);
    among equal mana values, colored mana outranks colorless either way --
    more distinct colors first, then more colored symbols
    ({1}{U}{R} > {1}{R}{R} > {3}); then by name. Uses the front face's cost.
    Cards whose mana value isn't cached sort after everything else."""
    colors, colored = color_weight(info.faces[0]["mana_cost"] if info.faces else "")
    value = info.cmc or 0
    return (info.cmc is None, -value if descending else value, -colors, -colored, card_name.casefold())
