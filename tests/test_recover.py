"""Rebuilding the database from a restore point plus the write log, and undoing one request."""

import hashlib
import os
import shutil

import pytest
from fastapi.testclient import TestClient

from mtgtracker import db, recover, restore
from mtgtracker.web.app import create_app
from mtgtracker.web.auth import AuthConfig
from tests.test_changelog import extra_migrations
from tests.test_deck_wants import LIST, scryfall_api  # noqa: F401
from tests.test_restore import dump


@pytest.fixture
def live(db_path, cache_dir):
    client = TestClient(create_app(db_path, auth=AuthConfig(mode="single")))
    conn = db.connect(db_path)
    return client, conn, db_path


def deck_id(conn, name="Keen Engineering"):
    return conn.execute("SELECT id FROM decks WHERE name = ?", (name,)).fetchone()[0]


def lot_in(conn, deck, card):
    return conn.execute(
        "SELECT lots.id FROM lots JOIN deck_cards ON deck_cards.lot_id = lots.id"
        " WHERE deck_cards.deck_id = ? AND lots.card_name = ? ORDER BY lots.id", (deck, card),
    ).fetchone()[0]


def modify_and_apply(client, conn):
    """Move Blasphemous Act into Keen Engineering in place of Counterspell."""
    keen, reign = deck_id(conn), deck_id(conn, "Reign of Dragons")
    incoming, outgoing = lot_in(conn, reign, "Blasphemous Act"), lot_in(conn, keen, "Counterspell")
    resp = client.post(f"/decks/{keen}/changes", data={"lot_id": str(incoming)}, follow_redirects=False)
    page = client.get(resp.headers["location"]).text
    change = int(page.split(f"/decks/{keen}/changes/", 1)[1].split("/", 1)[0])
    client.post(f"/decks/{keen}/changes/{change}/cut", data={"cut_lot_id": str(outgoing)})
    applied = client.post(f"/decks/{keen}/apply", data={"note": "swap"}, follow_redirects=False)
    assert applied.status_code == 303
    return keen


def audit_id(conn, action):
    return conn.execute("SELECT MAX(id) FROM audit_log WHERE action = ?", (action,)).fetchone()[0]


def test_full_replay_equals_the_live_database(live, tmp_path, scryfall_api):  # noqa: F811
    client, conn, db_path = live
    point = restore.take(db_path, "base")
    point_seq = conn.execute("SELECT MAX(seq) FROM changelog").fetchone()[0]
    keen = deck_id(conn)
    client.post(f"/decks/{keen}/notes", data={"notes": "budget: $5"})
    client.post("/decks/new", data={"name": "Saheeli's Workshop", "decklist": LIST})
    modify_and_apply(client, conn)
    client.post(f"/decks/{keen}/notes", data={"notes": ""})

    # Every logged change a request made is tagged with that request's audit row.
    seq = conn.execute("SELECT MAX(seq) FROM changelog").fetchone()[0]
    untagged = conn.execute(
        "SELECT COUNT(*) FROM changelog WHERE seq > ? AND tbl != 'audit_log' AND"
        " (batch_id IS NULL OR batch_id NOT IN (SELECT id FROM audit_log))", (point_seq,)
    ).fetchone()[0]
    assert untagged == 0
    assert conn.execute("SELECT COUNT(*) FROM changelog WHERE seq > ?", (point_seq,)).fetchone()[0] > 50
    assert conn.execute("SELECT batch_id FROM changelog_context").fetchone()[0] is None

    out = tmp_path / "recovered.db"
    summary = recover.recover(point, db_path, out)
    assert seq == summary.final_seq

    copy = db.connect(out)
    assert dump(copy) == dump(conn)
    assert summary.fk_problems == []
    assert summary.final_seq == conn.execute("SELECT MAX(seq) FROM changelog").fetchone()[0]
    assert summary.applied > 50
    assert all(mine == live for mine, live in summary.counts.values())
    assert copy.execute("PRAGMA user_version").fetchone()[0] == 9
    # A faithful live DB: triggers are back and keep logging.
    assert db.ensure_changelog_triggers(copy) is False
    copy.execute("UPDATE decks SET notes = 'after' WHERE id = ?", (keen,))
    copy.commit()
    assert copy.execute("SELECT MAX(seq) FROM changelog").fetchone()[0] == summary.final_seq + 1


def test_until_seq_and_until_audit_stop_in_the_right_place(live, tmp_path):
    client, conn, db_path = live
    point = restore.take(db_path, "base")
    keen = deck_id(conn)
    client.post(f"/decks/{keen}/notes", data={"notes": "first"})
    after_first, seq_first = dump(conn), conn.execute("SELECT MAX(seq) FROM changelog").fetchone()[0]
    client.post(f"/decks/{keen}/notes", data={"notes": "second"})
    second = audit_id(conn, "deck_save_notes")
    client.post(f"/decks/{keen}/notes", data={"notes": "third"})

    by_seq = tmp_path / "a.db"
    recover.recover(point, db_path, by_seq, until_seq=seq_first)
    assert dump(db.connect(by_seq)) == after_first

    by_audit = tmp_path / "b.db"
    recover.recover(point, db_path, by_audit, until_audit=second)
    assert dump(db.connect(by_audit)) == after_first  # just before the second request, audit row and all

    by_time = tmp_path / "c.db"
    at = conn.execute("SELECT at FROM changelog WHERE seq = ?", (seq_first,)).fetchone()[0]
    recover.recover(point, db_path, by_time, until_time=at)
    by_time_notes = db.connect(by_time).execute("SELECT notes FROM decks WHERE id = ?", (keen,))
    assert by_time_notes.fetchone()[0] == "first"

    with pytest.raises(recover.RecoverError, match="nothing for audit id 999"):
        recover.recover(point, db_path, tmp_path / "d.db", until_audit=999)
    assert not (tmp_path / "d.db").exists()


def test_replay_across_a_migration(live, tmp_path, monkeypatch):
    _client, conn, db_path = live
    point = restore.take(db_path, "base")
    extra_migrations(
        tmp_path, monkeypatch,
        "ALTER TABLE decks ADD COLUMN flavor TEXT; UPDATE decks SET flavor = 'migrated';"
        "CREATE TABLE tags (id INTEGER PRIMARY KEY, label TEXT NOT NULL);",
    )
    keen = deck_id(conn)
    conn.execute("UPDATE decks SET notes = 'before' WHERE id = ?", (keen,))
    conn.commit()
    db.migrate(conn)
    conn.execute("INSERT INTO tags (label) VALUES ('new table')")
    conn.execute("UPDATE decks SET flavor = 'spicy', notes = 'after' WHERE id = ?", (keen,))
    conn.commit()

    out = tmp_path / "recovered.db"
    summary = recover.recover(point, db_path, out)
    copy = db.connect(out)
    assert dump(copy) == dump(conn)
    assert copy.execute("PRAGMA user_version").fetchone()[0] == 10
    assert summary.fk_problems == []
    assert db.ensure_changelog_triggers(copy) is False  # new table's triggers exist, in sync

    # Stopping before the migration gives the old schema.
    s_seq = conn.execute("SELECT seq FROM changelog WHERE op = 'S' AND after_json LIKE '%10%'").fetchone()[0]
    older = tmp_path / "older.db"
    recover.recover(point, db_path, older, until_seq=s_seq - 1)
    assert db.connect(older).execute("PRAGMA user_version").fetchone()[0] == 9


def test_the_live_database_is_never_written(live, tmp_path):
    client, conn, db_path = live
    point = restore.take(db_path, "base")
    client.post(f"/decks/{deck_id(conn)}/notes", data={"notes": "x"})
    conn.close()

    def fingerprint():
        return hashlib.sha256(db_path.read_bytes()).hexdigest(), os.stat(db_path).st_mtime_ns

    before = fingerprint()
    recover.recover(point, db_path, tmp_path / "out.db")
    assert fingerprint() == before
    assert not list(db_path.parent.glob("*-journal"))
    assert not list(db_path.parent.glob("*-wal"))


def test_refuses_to_overwrite_or_target_the_live_database(live, tmp_path):
    _client, _conn, db_path = live
    point = restore.take(db_path, "base")
    existing = tmp_path / "exists.db"
    existing.write_bytes(b"keep me")
    with pytest.raises(recover.RecoverError, match="already exists"):
        recover.recover(point, db_path, existing)
    assert existing.read_bytes() == b"keep me"
    with pytest.raises(recover.RecoverError):
        recover.recover(point, db_path, db_path)
    with pytest.raises(recover.RecoverError, match="at most one"):
        recover.recover(point, db_path, tmp_path / "x.db", until_seq=1, until_audit=1)


def test_a_point_can_be_replayed_from_a_later_copy_of_the_log(live, tmp_path):
    client, conn, db_path = live
    point = restore.take(db_path, "base")
    client.post(f"/decks/{deck_id(conn)}/notes", data={"notes": "x"})
    later = tmp_path / "later-copy.db"
    shutil.copy(db_path, later)
    out = tmp_path / "out.db"
    recover.recover(point, later, out)
    assert dump(db.connect(out)) == dump(conn)


def test_a_log_from_another_timeline_is_refused(live, tmp_path):
    client, conn, db_path = live
    point = restore.take(db_path, "base")
    client.post(f"/decks/{deck_id(conn)}/notes", data={"notes": "x"})
    other = tmp_path / "other.db"
    shutil.copy(db_path, other)
    other_conn = db.connect(other)
    point_seq = restore.list_points(point.parent)[0]["changelog_seq"]
    other_conn.execute("UPDATE changelog SET tbl = 'elsewhere' WHERE seq = ?", (point_seq,))
    other_conn.commit()
    with pytest.raises(recover.RecoverError, match="does not continue"):
        recover.recover(point, other, tmp_path / "out.db")
    assert not (tmp_path / "out.db").exists()


# --- undo_batch ----------------------------------------------------------------

def test_undo_a_notes_edit(live):
    client, conn, _ = live
    keen = deck_id(conn)
    client.post(f"/decks/{keen}/notes", data={"notes": "keep"})
    client.post(f"/decks/{keen}/notes", data={"notes": "oops"})
    batch = audit_id(conn, "deck_save_notes")
    preview = recover.undo_batch(conn, batch)
    assert [(p["table"], p["undo"]) for p in preview] == [("decks", "restore")]
    assert conn.execute("SELECT notes FROM decks WHERE id = ?", (keen,)).fetchone()[0] == "keep"
    with pytest.raises(recover.RecoverError, match="changed again later"):
        recover.undo_batch(conn, batch)  # the undo itself is a later change to that row


def test_undo_a_deck_apply(live):
    client, conn, _ = live
    keen = modify_and_apply(client, conn)
    swapped = dump(conn)
    batch = audit_id(conn, "deck_apply")
    before_cards = {r[0] for r in conn.execute("SELECT lot_id FROM deck_cards WHERE deck_id = ?", (keen,))}
    preview = recover.undo_batch(conn, batch, commit=False)
    assert {p["undo"] for p in preview} >= {"delete", "restore"}
    conn.rollback()
    assert dump(conn) == swapped  # a preview with commit=False and rollback changes nothing

    recover.undo_batch(conn, batch)
    after_cards = {r[0] for r in conn.execute("SELECT lot_id FROM deck_cards WHERE deck_id = ?", (keen,))}
    assert after_cards != before_cards
    assert conn.execute("SELECT version FROM decks WHERE id = ?", (keen,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM deck_versions WHERE deck_id = ? AND version = 2",
                        (keen,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE id = ?", (batch,)).fetchone()[0] == 1


def test_undo_is_refused_when_a_later_batch_touched_the_same_row(live):
    client, conn, _ = live
    keen = deck_id(conn)
    client.post(f"/decks/{keen}/notes", data={"notes": "one"})
    first = audit_id(conn, "deck_save_notes")
    client.post(f"/decks/{keen}/notes", data={"notes": "two"})
    with pytest.raises(recover.RecoverError, match="changed again later"):
        recover.undo_batch(conn, first)
    assert conn.execute("SELECT notes FROM decks WHERE id = ?", (keen,)).fetchone()[0] == "two"
    with pytest.raises(recover.RecoverError, match="no logged changes"):
        recover.undo_batch(conn, 12345)


# --- CLI -----------------------------------------------------------------------

def test_cli_restore_points_recover_and_undo(live, tmp_path):
    from typer.testing import CliRunner

    from mtgtracker.cli import app

    client, conn, db_path = live
    runner = CliRunner()
    assert "No restore points" in runner.invoke(app, ["restore-points", "--db", str(db_path)]).output
    point = restore.take(db_path, "base")
    keen = deck_id(conn)
    client.post(f"/decks/{keen}/notes", data={"notes": "cli"})
    listing = runner.invoke(app, ["restore-points", "--db", str(db_path)]).output
    assert point.name in listing

    out = tmp_path / "out.db"
    result = runner.invoke(app, ["recover", str(point), "--out", str(out), "--log-db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert dump(db.connect(out)) == dump(conn)
    again = runner.invoke(app, ["recover", str(point), "--out", str(out), "--log-db", str(db_path)])
    assert again.exit_code != 0

    batch = str(audit_id(conn, "deck_save_notes"))
    dry = runner.invoke(app, ["undo-batch", batch, "--db", str(db_path)])
    assert "Would undo 1 changes" in dry.output
    assert conn.execute("SELECT notes FROM decks WHERE id = ?", (keen,)).fetchone()[0] == "cli"
    done = runner.invoke(app, ["undo-batch", batch, "--yes", "--db", str(db_path)])
    assert "Undid 1 changes" in done.output
    assert conn.execute("SELECT notes FROM decks WHERE id = ?", (keen,)).fetchone()[0] in (None, "")
