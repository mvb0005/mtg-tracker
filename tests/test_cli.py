"""CLI tests via typer's CliRunner. Every invocation passes --db (and
--receipts) under tmp_path, the working directory is tmp_path, and Scryfall
is served from the committed fdc fixture with network clients disabled."""

import json
import shutil
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mtgtracker import db, scryfall
from mtgtracker.cli import app
from mtgtracker.decks import add_to_deck, borrow, create_deck

FIXTURES = Path(__file__).parent / "fixtures"
ORDER_FIXTURE = FIXTURES / "order_scrubbed.order"
SCRYFALL_FIXTURE = FIXTURES / "scryfall_fdc.json"

runner = CliRunner()

_REAL_CONNECT = db.connect


def _fast_connect(db_path):
    # Seeding commits once per card; on a real file each commit fsyncs,
    # which makes these tests take tens of seconds on some filesystems.
    # Durability doesn't matter for a throwaway test DB, so skip the fsyncs.
    conn = _REAL_CONNECT(db_path)
    conn.execute("PRAGMA synchronous = OFF")
    return conn


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    cache_dir = tmp_path / "scryfall_cache"
    cache_dir.mkdir()
    shutil.copy(SCRYFALL_FIXTURE, cache_dir / "fdc.json")
    monkeypatch.setattr(scryfall, "CACHE_DIR", cache_dir)

    def refuse(*_args, **_kwargs):
        raise AssertionError("CLI test tried to reach the network")

    monkeypatch.setattr(scryfall.httpx, "Client", refuse)
    monkeypatch.setattr(db, "connect", _fast_connect)
    # The CLI's defaults are relative paths (data/, receipts/); make sure
    # that even a missed --db flag can't touch the real repo dirs.
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "db" / "test.db", tmp_path / "receipts"


@pytest.fixture
def imported(paths):
    """paths, with the fixture order already imported (and its decks seeded)."""
    _import(paths)
    return paths


def _invoke(*args):
    result = runner.invoke(app, [str(a) for a in args])
    assert result.exit_code == 0, result.output
    return result.output


def _import(paths, order_path=ORDER_FIXTURE):
    db_path, receipts_dir = paths
    return _invoke("import-order", order_path, "--db", db_path, "--receipts", receipts_dir)


def _connect(db_path):
    return db.connect(db_path)


def _deck_id(conn, name):
    return conn.execute("SELECT id FROM decks WHERE name = ?", (name,)).fetchone()["id"]


def test_import_order_creates_transaction_and_seeds_both_precons(paths, tmp_path):
    db_path, receipts_dir = paths
    output = _import(paths)

    assert "Created transaction 1." in output
    assert "Seeded deck 'Foundations Commander Deck - Reign of Dragons' -> deck" in output
    assert "Seeded deck 'Foundations Commander Deck - Keen Engineering' -> deck" in output
    assert "Needs your input" in output
    assert "ambiguous: Sol Ring (multiple printings in set) -> fdc#" in output
    assert "ambiguous: Arcane Signet" in output

    assert db_path.exists()
    assert len(list(receipts_dir.iterdir())) == 1
    # Nothing written to the relative default locations.
    assert not (tmp_path / "data").exists()

    conn = _connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM decks").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0] == 200
    assert conn.execute("SELECT SUM(allocated_cost_cents) FROM lots").fetchone()[0] == 8620


def test_import_order_rerun_is_a_noop(imported):
    db_path, _ = imported
    output = _import(imported)

    assert "No-op: transaction 1 already up to date." in output
    assert "Seeded deck" not in output

    conn = _connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM decks").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0] == 200
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


def test_import_order_newer_copy_reports_update(imported, tmp_path):
    paths = imported

    with zipfile.ZipFile(ORDER_FIXTURE) as src:
        contents = {name: src.read(name) for name in src.namelist()}
    order = json.loads(contents["order.json"])
    order["updatedAt"] = "2099-01-01T00:00:00Z"
    order["status"] = "completed"
    contents["order.json"] = json.dumps(order).encode()
    newer = tmp_path / "newer.order"
    with zipfile.ZipFile(newer, "w") as dst:
        for name, data in contents.items():
            dst.writestr(name, data)

    output = _import(paths, newer)
    assert "Updated existing transaction 1." in output
    assert "Seeded deck" not in output


def test_import_order_recovers_seeding_after_failed_scryfall_fetch(paths, tmp_path, monkeypatch):
    # First run: the set isn't cached and the network is unavailable, so
    # seeding fails after the transaction has been committed.
    empty_cache = tmp_path / "empty_cache"
    empty_cache.mkdir()
    monkeypatch.setattr(scryfall, "CACHE_DIR", empty_cache)
    db_path, receipts_dir = paths
    result = runner.invoke(
        app, ["import-order", str(ORDER_FIXTURE), "--db", str(db_path), "--receipts", str(receipts_dir)]
    )
    assert result.exit_code != 0

    conn = _connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0] == 0
    conn.close()

    # Re-run with Scryfall data available: transaction is a no-op, but the
    # decks get seeded.
    shutil.copy(SCRYFALL_FIXTURE, empty_cache / "fdc.json")
    output = _import(paths)
    assert "No-op" in output
    assert output.count("Seeded deck") == 2


def test_list_decks_empty_db(paths):
    db_path, _ = paths
    assert _invoke("list-decks", "--db", db_path) == ""


def test_list_decks_after_import(imported):
    db_path, _ = imported
    output = _invoke("list-decks", "--db", db_path)

    assert output.splitlines() == [
        "1: Reign of Dragons (Commander) - 100 cards equipped",
        "2: Keen Engineering (Commander) - 100 cards equipped",
    ]


def test_list_decks_counts_only_equipped_cards(imported):
    db_path, _ = imported
    conn = _connect(db_path)
    reign = _deck_id(conn, "Reign of Dragons")
    frank = create_deck(conn, "Frankendeck", "2026-10-01")
    lot_ids = [
        r["lot_id"]
        for r in conn.execute("SELECT lot_id FROM deck_cards WHERE deck_id = ? LIMIT 3", (reign,))
    ]
    for lot_id in lot_ids:
        borrow(conn, lot_id, frank)
    conn.close()

    lines = _invoke("list-decks", "--db", db_path).splitlines()
    assert "1: Reign of Dragons (Commander) - 97 cards equipped" in lines
    assert "3: Frankendeck (Commander) - 3 cards equipped" in lines


def test_reassemble_deck_fully_assembled(imported):
    db_path, _ = imported
    out = _invoke("reassemble-deck", "1", "--db", db_path)
    assert out.splitlines() == [
        "Reign of Dragons: 100 in the deck, 0 elsewhere, 0 not owned yet", "Deck is fully assembled.",
    ]


def test_reassemble_deck_plans_pulls_equips_and_the_pull_list(imported):
    db_path, _ = imported
    conn = _connect(db_path)
    reign = _deck_id(conn, "Reign of Dragons")
    keen = _deck_id(conn, "Keen Engineering")

    def lot_for(name):
        return conn.execute(
            "SELECT id FROM lots WHERE card_name = ? AND product_id = 1 LIMIT 1", (name,)
        ).fetchone()["id"]

    borrowed = lot_for("Lathliss, Dragon Queen")
    borrow(conn, borrowed, keen)

    # A card taken out of the deck and not put anywhere: still on the list, equipped nowhere.
    loose = lot_for("Sol Ring")
    conn.execute("UPDATE deck_cards SET equipped = 0 WHERE lot_id = ?", (loose,))
    conn.commit()
    conn.close()

    output = _invoke("reassemble-deck", reign, "--db", db_path)
    assert "Put in the deck (in no deck now) (1):" in output
    assert f"  Sol Ring  [equip:lot:{loose}]" in output
    assert "Pull from another deck (risky) (1):" in output
    assert f"  Lathliss, Dragon Queen (from Keen Engineering)  [pull:lot:{borrowed}]" in output
    assert "  Keen Engineering (1):\n    Lathliss, Dragon Queen - from Keen Engineering" in output
    assert "  From your collection (1):\n    Sol Ring - on this list, in no deck" in output


def test_reassemble_deck_unknown_id_is_an_error(paths):
    db_path, _ = paths
    result = runner.invoke(app, ["reassemble-deck", "999", "--db", str(db_path)])
    assert result.exit_code == 1


def test_add_to_deck_unequipped_shows_up_as_a_pull(imported):
    # A card listed (unequipped) in a new deck, still equipped in its precon,
    # is a pull when assembling the new deck.
    db_path, _ = imported
    conn = _connect(db_path)
    frank = create_deck(conn, "Frankendeck", "2026-10-01")
    lot_id = conn.execute(
        "SELECT id FROM lots WHERE card_name = 'Lathliss, Dragon Queen'"
    ).fetchone()["id"]
    add_to_deck(conn, frank, lot_id, equipped=False)
    conn.close()

    output = _invoke("reassemble-deck", frank, "--db", db_path)
    assert f"  Lathliss, Dragon Queen (from Reign of Dragons)  [pull:lot:{lot_id}]" in output
