"""services.collection.delete_import: what blocks an undo, and commit=False."""

import pytest

from mtgtracker import db
from mtgtracker.importers.collection_csv import import_collection
from mtgtracker.services import NotFoundError
from mtgtracker.services import collection as service
from tests.test_collection_csv import MANABOX


@pytest.fixture
def conn(db_path, cache_dir):
    conn = db.connect(db_path)
    yield conn
    conn.close()


@pytest.fixture
def imported(conn):
    summary = import_collection(conn, MANABOX.encode(), owner_id=1, imported_by=1, filename="a.csv")
    lots = [r[0] for r in conn.execute(
        "SELECT id FROM lots WHERE collection_import_id = ? ORDER BY id", (summary.import_id,))]
    return summary.import_id, lots


def _count(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


def test_delete_removes_the_lots_and_the_record(conn, imported):
    import_id, _lots = imported
    notice, preview = service.delete_import(conn, import_id)
    assert notice == "Removed the import: 4 cards."
    assert preview["lots"] == 4
    assert preview["filename"] == "a.csv"
    assert preview["owner"] == "Owner"
    assert _count(conn, "SELECT COUNT(*) FROM lots WHERE collection_import_id = ?", import_id) == 0
    assert _count(conn, "SELECT COUNT(*) FROM collection_imports WHERE id = ?", import_id) == 0
    with pytest.raises(NotFoundError):
        service.delete_import(conn, import_id)


def test_commit_false_leaves_the_transaction_to_the_caller(conn, imported):
    import_id, _ = imported
    _, first = service.delete_import(conn, import_id, commit=False)
    assert conn.in_transaction
    conn.rollback()
    assert _count(conn, "SELECT COUNT(*) FROM lots WHERE collection_import_id = ?", import_id) == 4
    _, again = service.delete_import(conn, import_id, commit=False)
    assert again == first  # deterministic, so a confirm token's digest holds
    conn.rollback()


@pytest.mark.parametrize(("blocker", "setup"), [
    ("on_deck_lists", "INSERT INTO deck_cards (deck_id, lot_id, equipped) VALUES (1, :lot, 0)"),
    ("on_deck_lists", "INSERT INTO deck_cards (deck_id, lot_id, equipped) VALUES (1, :lot, 1)"),
    ("disposed", "INSERT INTO dispositions (lot_id, disposed_at, disposition_type)"
                 " VALUES (:lot, 't', 'sold')"),
    ("in_version_snapshots", "INSERT INTO deck_version_cards (version_id, lot_id) VALUES (1, :lot)"),
    ("in_deck_changes", "INSERT INTO deck_changes (deck_id, add_card_name, add_lot_id, created_at)"
                        " VALUES (1, 'x', :lot, 't')"),
    ("in_deck_changes", "INSERT INTO deck_changes (deck_id, add_card_name, cut_lot_id, created_at)"
                        " VALUES (1, 'x', :lot, 't')"),
    ("in_deck_changes", "INSERT INTO deck_changes (deck_id, add_card_name, add_lot_id, created_at,"
                        " applied_version) VALUES (1, 'x', :lot, 't', 1)"),
])
def test_an_import_in_use_is_refused_and_nothing_is_written(conn, imported, blocker, setup):
    import_id, lots = imported
    conn.execute(setup, {"lot": lots[0]})
    conn.commit()
    with pytest.raises(service.ImportInUseError) as refused:
        service.delete_import(conn, import_id)
    assert refused.value.extra["blockers"] == {blocker: 1}
    assert _count(conn, "SELECT COUNT(*) FROM lots WHERE collection_import_id = ?", import_id) == 4
    assert _count(conn, "SELECT COUNT(*) FROM collection_imports WHERE id = ?", import_id) == 1
