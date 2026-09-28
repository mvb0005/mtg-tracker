import sqlite3

from mtgtracker import db
from mtgtracker.importers.precon_decklist import (
    load_seed_decklists,
    resolve_decklist,
    seed_precon_deck_for_product,
)


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

    # Sol Ring and Arcane Signet each have two fdc printings (an
    # original-wave foil-only version and a newer nonfoil/foil version)
    # with nothing in the decklist text saying which shipped in this box --
    # these must come back ambiguous, not guessed.
    ambiguous_names = {a["name"] for a in report.ambiguous}
    assert {"Sol Ring", "Arcane Signet"} <= ambiguous_names

    # Not every card in the box necessarily got a fresh fdc-set reprint --
    # e.g. "Dragonstorm" has no fdc printing at all, only reprints in other
    # sets, so it can't be resolved to an exact printing without the
    # physical card. It must show up as not-found, not guessed at.
    assert "Dragonstorm" in report.not_found
    assert report.basics_unresolved == ["Mountain"]

    total_accounted = len(report.resolved) + len(report.ambiguous) + len(report.not_found)
    assert total_accounted == 68  # non-basic card count in the seed file


def test_seed_precon_deck_creates_100_lots_all_equipped_and_allocated():
    conn = _make_conn()
    product_id = _make_transaction_and_product(
        conn, "Foundations Commander Deck - Reign of Dragons", cost_basis_cents=4752
    )

    result = seed_precon_deck_for_product(conn, product_id)
    assert result is not None
    deck_id, report = result

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
