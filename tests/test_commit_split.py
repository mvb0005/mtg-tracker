"""Write helpers take `commit` (default True). With commit=False they do all
their validation and writes but neither commit nor roll back, so a caller
(a dry run, a larger transaction) decides. The wrapper behavior, commit on
success, is what the rest of the suite already exercises; each test here
also checks it on a second, fresh connection."""

import csv
import io

import pytest

from mtgtracker import commanders, db, deckmods, users
from mtgtracker.importers import collection_csv
from mtgtracker.services import decks as deck_service
from tests.test_commanders import typed_cache  # noqa: F401  (fixture)
from tests.test_deck_wants import LIST, scryfall_api  # noqa: F401  (fixture)

TABLES = ("decks", "deck_cards", "deck_wants", "deck_changes", "deck_versions", "lots",
          "collection_imports", "users")


def _state(conn):
    counts = tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES)
    extras = conn.execute(
        "SELECT COALESCE(SUM(version), 0), COALESCE(GROUP_CONCAT(commander), ''), COUNT(notes) FROM decks"
    ).fetchone()
    applied = conn.execute("SELECT COUNT(applied_version), COUNT(cut_lot_id) FROM deck_changes").fetchone()
    return counts + tuple(extras) + tuple(applied)


@pytest.fixture
def conn(db_path, cache_dir):
    conn = db.connect(db_path)
    yield conn
    conn.close()


def _assert_split(conn, db_path, write):
    """write(commit=...) must leave nothing behind after a rollback when
    commit=False, and persist for other connections when committing."""
    before = _state(conn)
    write(commit=False)
    assert _state(conn) != before  # it did write, on this connection's transaction
    other = db.connect(db_path)
    assert _state(other) == before  # ...but nothing was committed
    conn.rollback()
    assert _state(conn) == before

    write(commit=True)
    assert _state(other) != before
    other.close()


def _keen_reign(conn):
    ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM decks")}
    return ids["Keen Engineering"], ids["Reign of Dragons"]


def _lot(conn, deck_id, name):
    return conn.execute(
        "SELECT lots.id FROM lots JOIN deck_cards ON deck_cards.lot_id = lots.id"
        " WHERE deck_cards.deck_id = ? AND lots.card_name = ? ORDER BY lots.id", (deck_id, name),
    ).fetchone()[0]


def test_apply_changes(conn, db_path):
    keen, reign = _keen_reign(conn)
    change = deckmods.add_owned(conn, keen, _lot(conn, reign, "Blasphemous Act"))
    deckmods.set_cut(conn, keen, change, _lot(conn, keen, "Counterspell"))
    _assert_split(conn, db_path, lambda commit: deckmods.apply_changes(conn, keen, "x", commit=commit))


def test_import_list(conn, db_path, scryfall_api):  # noqa: F811
    keen, _ = _keen_reign(conn)
    _assert_split(conn, db_path, lambda commit: deckmods.import_list(conn, keen, LIST, commit=commit))


def test_create_deck_from_list(conn, db_path, scryfall_api):  # noqa: F811
    _assert_split(
        conn, db_path,
        lambda commit: deckmods.create_deck_from_list(conn, "Workshop", LIST, commit=commit),
    )


def test_create_deck_from_list_failure_rolls_back_only_when_committing(conn, db_path, scryfall_api):  # noqa: F811
    before = _state(conn)
    with pytest.raises(deckmods.ChangeError):
        deckmods.create_deck_from_list(conn, "Empty", "Sideboard\n1 Island\n")
    assert _state(conn) == before


def test_import_collection(conn, db_path, scryfall_api):  # noqa: F811
    out = io.StringIO()
    csv.writer(out).writerows([["Name", "Quantity"], ["Counterspell", "2"], ["Totally Made Up", "1"]])
    data = out.getvalue().encode()
    _assert_split(
        conn, db_path,
        lambda commit: collection_csv.import_collection(
            conn, data, owner_id=1, imported_by=1, filename="a.csv", commit=commit
        ),
    )


def test_change_list_edits(conn, db_path):
    keen, reign = _keen_reign(conn)
    incoming, outgoing = _lot(conn, reign, "Blasphemous Act"), _lot(conn, keen, "Counterspell")
    _assert_split(conn, db_path, lambda commit: deckmods.add_owned(conn, keen, incoming, commit=commit))
    [change] = deckmods.pending_changes(conn, keen)
    _assert_split(
        conn, db_path, lambda commit: deckmods.set_cut(conn, keen, change["id"], outgoing, commit=commit)
    )
    _assert_split(
        conn, db_path, lambda commit: deckmods.remove_change(conn, keen, change["id"], commit=commit)
    )


def test_set_commander(conn, db_path, typed_cache):  # noqa: F811
    keen, _ = _keen_reign(conn)
    current = conn.execute("SELECT commander FROM decks WHERE id = ?", (keen,)).fetchone()[0]
    other = next(c["card_name"] for c in commanders.eligible(conn, keen) if c["card_name"] != current)
    _assert_split(
        conn, db_path, lambda commit: commanders.set_commander(conn, keen, other, commit=commit)
    )


def test_save_notes(conn, db_path):
    keen, _ = _keen_reign(conn)
    _assert_split(conn, db_path, lambda commit: deck_service.save_notes(conn, keen, "plan", commit=commit))


def test_create_user(conn, db_path):
    _assert_split(conn, db_path, lambda commit: users.create(conn, "Eve", "eve@example.com", commit=commit))
