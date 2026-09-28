import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from mtgtracker import db, scryfall
from mtgtracker.importers.precon_decklist import (
    load_seed_decklists,
    resolve_decklist,
    seed_precon_deck_for_product,
)

FIXTURE_SET_DATA = Path(__file__).parent / "fixtures" / "scryfall_fdc.json"


@pytest.fixture(autouse=True)
def fixture_scryfall_cache(tmp_path, monkeypatch):
    """Point scryfall.py at a committed, trimmed fdc fixture instead of the
    gitignored local cache or the network, so these tests work on a fresh
    clone with no Scryfall access."""
    cache_dir = tmp_path / "scryfall_cache"
    cache_dir.mkdir()
    shutil.copy(FIXTURE_SET_DATA, cache_dir / "fdc.json")
    monkeypatch.setattr(scryfall, "CACHE_DIR", cache_dir)
    return cache_dir


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    return conn


def _make_transaction_and_product(conn, display_name: str, cost_basis_cents: int) -> int:
    conn.execute(
        "INSERT INTO transactions (vendor, created_at, updated_at, subtotal_cents,"
        " tax_cents, total_cents, source) VALUES ('Test', '2026-09-27T00:00:00Z',"
        " '2026-09-27T00:00:00Z', 0, 0, 0, 'manual')"
    )
    txn_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO products (transaction_id, title, display_name, qty,"
        " unit_price_cents, line_subtotal_cents, tax_allocated_cents,"
        " cost_basis_cents) VALUES (?, ?, ?, 1, ?, ?, 0, ?)",
        (txn_id, display_name, display_name, cost_basis_cents, cost_basis_cents, cost_basis_cents),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_seed_decklists_load_and_have_100_cards_each():
    seeds = load_seed_decklists()
    assert len(seeds) == 2

    for seed in seeds:
        basics_total = sum(seed.get("basics", {}).values())
        assert len(seed["cards"]) + basics_total == 100


def test_resolve_decklist_against_cached_fdc_set():
    seeds = {s["deck_name"]: s for s in load_seed_decklists()}
    report = resolve_decklist(seeds["Reign of Dragons"])

    # Sol Ring and Arcane Signet each have two fdc printings, with nothing
    # in the decklist text saying which one shipped in this box -- these
    # must come back ambiguous, not guessed.
    ambiguous_names = {a["name"] for a in report.ambiguous}
    assert {"Sol Ring", "Arcane Signet"} <= ambiguous_names

    assert report.not_found == []
    assert report.basics_unresolved == ["Mountain"]

    total_accounted = len(report.resolved) + len(report.ambiguous) + len(report.not_found)
    assert total_accounted == 68  # non-basic card count in the seed file


def test_resolved_cards_stay_within_commander_color_identity():
    # The only check that validates the bulk of the transcription: a
    # 100-card count can't tell "Dragonstorm Globe" apart from a typo, but
    # a card outside the commander's color identity would make the deck
    # Commander-illegal, which means a transcription error.
    cards = json.loads(FIXTURE_SET_DATA.read_text())
    by_id = {c["id"]: c for c in cards}

    for deck_name, commander_colors in (
        ("Reign of Dragons", {"R"}),
        ("Keen Engineering", {"U"}),
    ):
        seed = next(s for s in load_seed_decklists() if s["deck_name"] == deck_name)
        report = resolve_decklist(seed)
        for entry in report.resolved:
            card = by_id[entry["scryfall_id"]]
            identity = set(card.get("color_identity", []))
            assert identity <= commander_colors, (
                f"{entry['name']} has color identity {identity}, "
                f"outside {commander_colors} for {deck_name} -- likely a "
                "transcription error"
            )


def test_seed_precon_deck_creates_100_lots_all_equipped_and_allocated():
    conn = _make_conn()
    product_id = _make_transaction_and_product(
        conn, "Foundations Commander Deck - Reign of Dragons", cost_basis_cents=4752
    )

    result = seed_precon_deck_for_product(conn, product_id)
    assert result is not None
    deck_id, _report = result

    lots = conn.execute(
        "SELECT * FROM lots WHERE product_id = ?", (product_id,)
    ).fetchall()
    assert len(lots) == 100

    equipped = conn.execute(
        "SELECT COUNT(*) FROM deck_cards WHERE deck_id = ? AND equipped = 1", (deck_id,)
    ).fetchone()[0]
    assert equipped == 100

    total_allocated = conn.execute(
        "SELECT SUM(allocated_cost_cents) FROM lots WHERE product_id = ?", (product_id,)
    ).fetchone()[0]
    assert total_allocated == 4752

    # Deterministic even split: 4752 // 100 = 47, remainder 52 -> 52 lots
    # get 48c, 48 lots get 47c.
    shares = [row["allocated_cost_cents"] for row in lots]
    assert shares.count(48) == 52
    assert shares.count(47) == 48

    commander_lot = conn.execute(
        "SELECT * FROM lots WHERE product_id = ? AND card_name = 'Lathliss, Dragon Queen'",
        (product_id,),
    ).fetchone()
    assert commander_lot["finish"] == "foil"

    product_type = conn.execute(
        "SELECT product_type FROM products WHERE id = ?", (product_id,)
    ).fetchone()["product_type"]
    assert product_type == "precon"


def test_unknown_product_returns_none():
    conn = _make_conn()
    product_id = _make_transaction_and_product(conn, "Some Random Booster Box", 1000)

    assert seed_precon_deck_for_product(conn, product_id) is None


def test_seeding_twice_is_a_noop():
    conn = _make_conn()
    product_id = _make_transaction_and_product(
        conn, "Foundations Commander Deck - Keen Engineering", cost_basis_cents=3868
    )

    assert seed_precon_deck_for_product(conn, product_id) is not None
    assert seed_precon_deck_for_product(conn, product_id) is None

    assert conn.execute("SELECT COUNT(*) FROM decks").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0] == 100


def test_seed_unknown_product_id_raises():
    conn = _make_conn()
    with pytest.raises(ValueError, match="no product with id 999"):
        seed_precon_deck_for_product(conn, 999)


def _card(card_id, name, finishes):
    return {"id": card_id, "name": name, "set": "tst", "collector_number": card_id[1:], "finishes": finishes}


def _write_test_set(cache_dir, cards):
    (cache_dir / "tst.json").write_text(json.dumps(cards))


def _test_seed(commander, cards, basics=None):
    return {
        "deck_name": "Test Deck",
        "product_display_name": "Test Deck Product",
        "format": "Commander",
        "commander": commander,
        "set_code": "tst",
        "cards": cards,
        "basics": basics or {},
    }


def test_resolve_reports_not_found_names(fixture_scryfall_cache):
    _write_test_set(
        fixture_scryfall_cache,
        [_card("c1", "Real Commander", ["foil"])],
    )
    seed = _test_seed("Real Commander", ["Real Commander", "Made Up Card"], basics={"Island": 3})

    report = resolve_decklist(seed)

    assert [r["name"] for r in report.resolved] == ["Real Commander"]
    assert report.resolved[0]["finish"] == "foil"
    assert report.not_found == ["Made Up Card"]
    assert report.ambiguous == []
    assert report.basics_unresolved == ["Island"]
    assert report.has_issues()


def test_commander_without_foil_printing_is_ambiguous_not_guessed(fixture_scryfall_cache):
    _write_test_set(
        fixture_scryfall_cache,
        [
            _card("c1", "Nonfoil Cmdr", ["nonfoil"]),
            _card("c2", "Other Card", ["nonfoil"]),
        ],
    )
    seed = _test_seed("Nonfoil Cmdr", ["Nonfoil Cmdr", "Other Card"])

    report = resolve_decklist(seed)

    assert [a["name"] for a in report.ambiguous] == ["Nonfoil Cmdr"]
    assert "should be foil" in report.ambiguous[0]["reason"]
    # A non-commander card with only a nonfoil finish resolves normally.
    assert [(r["name"], r["finish"]) for r in report.resolved] == [("Other Card", "nonfoil")]


def test_unresolved_cards_still_get_lots_with_null_scryfall_id(fixture_scryfall_cache, monkeypatch):
    from mtgtracker.importers import precon_decklist

    _write_test_set(
        fixture_scryfall_cache,
        [_card("c2", "Other Card", ["nonfoil"])],
    )
    seed = _test_seed("Missing Cmdr", ["Missing Cmdr", "Other Card"], basics={"Island": 98})
    monkeypatch.setattr(precon_decklist, "load_seed_decklists", lambda: [seed])

    conn = _make_conn()
    product_id = _make_transaction_and_product(conn, "Test Deck Product", cost_basis_cents=1000)
    deck_id, report = seed_precon_deck_for_product(conn, product_id)

    assert report.not_found == ["Missing Cmdr"]
    lots = {
        row["card_name"]: row
        for row in conn.execute("SELECT * FROM lots WHERE product_id = ?", (product_id,)).fetchall()
    }
    assert lots["Missing Cmdr"]["scryfall_id"] is None
    assert lots["Missing Cmdr"]["finish"] == "foil"  # commander is still expected foil
    assert lots["Other Card"]["scryfall_id"] == "c2"
    assert conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0] == 100
    equipped = conn.execute(
        "SELECT COUNT(*) FROM deck_cards WHERE deck_id = ? AND equipped = 1", (deck_id,)
    ).fetchone()[0]
    assert equipped == 100
