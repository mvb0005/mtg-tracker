import json
import re

from mtgtracker import cardinfo, db

FRONT = "https://cards.scryfall.io/normal/front/a/b/heliod.jpg"
BACK = "https://cards.scryfall.io/normal/back/a/b/heliod.jpg"

HELIOD = {
    "id": "aaaaaaaa-0000-0000-0000-000000000017",
    "name": "Heliod, the Radiant Dawn // Heliod, the Warped Eclipse",
    "layout": "transform", "set": "mom", "collector_number": "17", "set_name": "March of the Machine",
    "type_line": "Legendary Enchantment Creature — God // Legendary Enchantment Creature — Phyrexian God",
    "cmc": 4.0, "color_identity": ["U", "W"], "rarity": "rare", "finishes": ["nonfoil", "foil"],
    "games": ["paper"], "promo": False, "legalities": {"commander": "legal"}, "prices": {},
    "card_faces": [
        {"name": "Heliod, the Radiant Dawn", "mana_cost": "{2}{W}{W}", "colors": ["W"],
         "type_line": "Legendary Enchantment Creature — God", "oracle_text": "...",
         "power": "4", "toughness": "4", "image_uris": {"normal": FRONT}},
        {"name": "Heliod, the Warped Eclipse", "mana_cost": "", "colors": ["U"],
         "type_line": "Legendary Enchantment Creature — Phyrexian God",
         "oracle_text": "You may cast spells as though they had flash.",
         "power": "4", "toughness": "6", "image_uris": {"normal": BACK}},
    ],
}


def test_back_image_only_for_cards_with_two_pictured_faces():
    assert cardinfo._info(HELIOD, exact=True).back_image == BACK
    assert cardinfo._info(HELIOD, exact=True).image == FRONT
    # An adventure (or split) card prints both halves on one front image.
    adventure = {
        "name": "Brazen Borrower // Petty Theft", "image_uris": {"normal": FRONT},
        "card_faces": [{"name": "Brazen Borrower"}, {"name": "Petty Theft"}],
    }
    assert cardinfo._info(adventure, exact=True).back_image is None
    single = {"name": "Sol Ring", "image_uris": {"normal": FRONT}}
    assert cardinfo._info(single, exact=True).back_image is None


def test_double_faced_tiles_get_a_flip_button(client, db_path, cache_dir):
    (cache_dir / "cards").mkdir(exist_ok=True)
    (cache_dir / "cards" / f"{HELIOD['id']}.json").write_text(json.dumps(HELIOD))
    conn = db.connect(db_path)
    keen = conn.execute("SELECT id FROM decks WHERE name = 'Keen Engineering'").fetchone()[0]
    lot = conn.execute(
        "INSERT INTO lots (card_name, scryfall_id, set_code, collector_number, acquired_date)"
        " VALUES (?, ?, 'mom', '17', '2026-10-01')",
        (HELIOD["name"], HELIOD["id"]),
    ).lastrowid
    conn.execute("INSERT INTO deck_cards (deck_id, lot_id, equipped) VALUES (?, ?, 1)", (keen, lot))
    conn.commit()
    conn.close()

    html = client.get(f"/decks/{keen}").text
    buttons = re.findall(r'<button type="button" class="tile-flip"[^>]*>', html)
    assert len(buttons) == 1  # only the double-faced card; the precon has none
    assert f'data-front="{FRONT}"' in buttons[0]
    assert f'data-back="{BACK}"' in buttons[0]
    assert 'aria-pressed="false"' in buttons[0]
    panel = next(
        json.loads(m.replace("&#39;", "'"))
        for m in re.findall(r"data-card='([^']*)'", html) if HELIOD["id"] in m or "Heliod" in m
    )
    assert panel["back_image"] == BACK
    assert [f["name"] for f in panel["faces"]] == ["Heliod, the Radiant Dawn", "Heliod, the Warped Eclipse"]
