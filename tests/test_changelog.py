"""The write log: generated triggers, migrations, and request batch ids."""

import json
import shutil
import sqlite3

from fastapi.testclient import TestClient

from mtgtracker import audit, db
from mtgtracker.web.app import create_app
from mtgtracker.web.auth import AuthConfig


def logged_tables(conn):
    return [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
        ) if r[0] not in ("changelog", "changelog_context")
    ]


def cl_triggers(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'cl\\_%' ESCAPE '\\'")}


def extra_migrations(tmp_path, monkeypatch, sql, name="0010_extra.sql"):
    """The real migrations plus one more, as the active migrations dir."""
    path = tmp_path / "migrations"
    shutil.copytree(db.MIGRATIONS_DIR, path)
    (path / name).write_text(sql)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", path)


def log_rows(conn, **where):
    sql = "SELECT * FROM changelog"
    if where:
        sql += " WHERE " + " AND ".join(f"{k} = ?" for k in where)
    return conn.execute(sql + " ORDER BY seq", list(where.values())).fetchall()


# --- triggers ------------------------------------------------------------------

def test_every_table_has_its_three_triggers():
    conn = sqlite3.connect(":memory:")
    db.migrate(conn)
    tables = logged_tables(conn)
    assert {"lots", "audit_log", "users"} <= set(tables)
    assert cl_triggers(conn) == {f"cl_{t}_{s}" for t in tables for s in "iud"}


def test_a_table_added_outside_a_migration_is_covered_on_next_migrate():
    conn = sqlite3.connect(":memory:")
    db.migrate(conn)
    conn.execute("CREATE TABLE newcomer (id INTEGER PRIMARY KEY, v TEXT)")
    assert "cl_newcomer_i" not in cl_triggers(conn)
    db.migrate(conn)  # nothing pending, but the triggers are checked every time
    assert {"cl_newcomer_i", "cl_newcomer_u", "cl_newcomer_d"} <= cl_triggers(conn)


def test_adding_a_column_regenerates_the_triggers(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    extra_migrations(tmp_path, monkeypatch, "ALTER TABLE decks ADD COLUMN flavor TEXT;")
    assert db.migrate(conn) == 10
    conn.execute("INSERT INTO decks (name, created_at, flavor) VALUES ('D', 't', 'spicy')")
    conn.commit()
    after = json.loads(log_rows(conn, tbl="decks")[-1]["after_json"])
    assert after["flavor"] == "spicy"


def test_unchanged_triggers_are_left_alone(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    assert db.ensure_changelog_triggers(conn) is False
    conn.execute("DROP TRIGGER cl_lots_u")
    assert db.ensure_changelog_triggers(conn) is True
    assert "cl_lots_u" in cl_triggers(conn)


def test_other_triggers_survive(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    extra_migrations(tmp_path, monkeypatch, "ALTER TABLE decks ADD COLUMN flavor TEXT;")
    db.migrate(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert {"users_system_no_email_insert", "users_system_no_email_update"} <= names


# --- logging -------------------------------------------------------------------

def test_insert_update_delete_are_logged_with_before_and_after(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    last = conn.execute("SELECT MAX(seq) FROM changelog").fetchone()[0]
    conn.execute("INSERT INTO decks (name, created_at) VALUES ('Deck', 't')")
    conn.execute("UPDATE decks SET name = 'Renamed' WHERE name = 'Deck'")
    conn.execute("DELETE FROM decks")
    conn.commit()
    ins, upd, dele = [r for r in log_rows(conn, tbl="decks") if r["seq"] > last]
    assert (ins["op"], ins["before_json"], json.loads(ins["after_json"])["name"]) == ("I", None, "Deck")
    assert (upd["op"], json.loads(upd["before_json"])["name"], json.loads(upd["after_json"])["name"]) == (
        "U", "Deck", "Renamed")
    assert (dele["op"], json.loads(dele["before_json"])["name"], dele["after_json"]) == ("D", "Renamed", None)
    assert ins["row_id"] == upd["row_id"] == dele["row_id"]
    assert ins["batch_id"] is None
    assert ins["at"].endswith("Z")


def test_a_rolled_back_write_is_not_logged(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    before = conn.execute("SELECT COUNT(*) FROM changelog").fetchone()[0]
    conn.execute("INSERT INTO decks (name, created_at) VALUES ('Deck', 't')")
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM changelog").fetchone()[0] == before


def test_a_migration_logs_one_schema_row_and_no_row_noise(db_path, tmp_path, monkeypatch):
    conn = db.connect(db_path)
    before = conn.execute("SELECT COUNT(*) FROM changelog").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0] > 100
    extra_migrations(tmp_path, monkeypatch, "UPDATE lots SET notes = 'migrated'; DELETE FROM deck_cards;")
    db.migrate(conn)
    new = log_rows(conn)[before:]
    assert [(r["op"], json.loads(r["after_json"])) for r in new] == [("S", {"user_version": 10})]
    # And logging resumes afterwards.
    conn.execute("UPDATE lots SET notes = 'again' WHERE id = 1")
    conn.commit()
    assert log_rows(conn)[-1]["op"] == "U"


# --- batch ids -----------------------------------------------------------------

def _client(db_path, cache_dir):
    return TestClient(create_app(db_path, auth=AuthConfig(mode="single")))


def _deck_id(conn):
    return conn.execute("SELECT id FROM decks WHERE name = 'Keen Engineering'").fetchone()[0]


def test_a_web_write_shares_its_audit_id_as_batch_id(db_path, cache_dir):
    client = _client(db_path, cache_dir)
    conn = db.connect(db_path)
    deck = _deck_id(conn)
    assert client.post(f"/decks/{deck}/notes", data={"notes": "plan"}).status_code == 200
    (audit_row,) = conn.execute("SELECT * FROM audit_log").fetchall()
    batch = log_rows(conn, batch_id=audit_row["id"])
    assert [(r["tbl"], r["op"]) for r in batch] == [("decks", "U")]
    assert json.loads(batch[0]["after_json"])["notes"] == "plan"
    # The audit row itself and its summary update aren't part of the batch.
    own = log_rows(conn, tbl="audit_log")
    assert [r["op"] for r in own] == ["I", "U"]
    assert all(r["batch_id"] is None for r in own)
    # The shared context never leaks past the request.
    assert conn.execute("SELECT batch_id FROM changelog_context").fetchone()[0] is None
    conn.execute("UPDATE decks SET notes = 'cli' WHERE id = ?", (deck,))
    conn.commit()
    assert log_rows(conn)[-1]["batch_id"] is None


def test_each_request_gets_its_own_batch(db_path, cache_dir):
    client = _client(db_path, cache_dir)
    conn = db.connect(db_path)
    deck = _deck_id(conn)
    client.post(f"/decks/{deck}/notes", data={"notes": "one"})
    client.post(f"/decks/{deck}/notes", data={"notes": "two"})
    ids = [r[0] for r in conn.execute("SELECT id FROM audit_log ORDER BY id")]
    assert len(ids) == 2
    for audit_id in ids:
        assert len(log_rows(conn, batch_id=audit_id)) == 1


def test_a_refused_request_leaves_no_audit_row_or_log(db_path, cache_dir):
    client = _client(db_path, cache_dir)
    conn = db.connect(db_path)
    before = conn.execute("SELECT COUNT(*) FROM changelog").fetchone()[0]
    assert client.post("/decks/9999/notes", data={"notes": "x"}).status_code == 404
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM changelog").fetchone()[0] == before


def test_an_audit_failure_does_not_break_the_response_or_the_write_log(db_path, cache_dir, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("audit down")

    monkeypatch.setattr(audit, "record", boom)
    client = _client(db_path, cache_dir)
    conn = db.connect(db_path)
    deck = _deck_id(conn)
    resp = client.post(f"/decks/{deck}/notes", data={"notes": "kept"}, follow_redirects=False)
    assert resp.status_code == 303
    assert client.get(f"/decks/{deck}/notes.txt").text == "kept"
    last = log_rows(conn)[-1]
    assert (last["tbl"], last["batch_id"]) == ("decks", None)
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_set_batch_on_a_plain_connection_tags_one_transaction(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    db.set_batch(conn, 77)
    conn.execute("INSERT INTO decks (name, created_at) VALUES ('A', 't')")
    conn.commit()
    conn.execute("INSERT INTO decks (name, created_at) VALUES ('B', 't')")  # re-armed lazily
    conn.rollback()
    db.set_batch(conn, None)
    conn.execute("INSERT INTO decks (name, created_at) VALUES ('C', 't')")
    conn.commit()
    assert [r["batch_id"] for r in log_rows(conn, tbl="decks")] == [77, None]
    assert conn.execute("SELECT batch_id FROM changelog_context").fetchone()[0] is None


def test_batch_id_never_leaks_through_with_blocks_or_explicit_begin(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    other = db.connect(tmp_path / "t.db")
    db.set_batch(conn, 5)
    with conn:
        conn.execute("INSERT INTO decks (name, created_at) VALUES ('A', 't')")
    assert other.execute("SELECT batch_id FROM changelog_context").fetchone()[0] is None
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO decks (name, created_at) VALUES ('B', 't')")
    conn.commit()
    assert other.execute("SELECT batch_id FROM changelog_context").fetchone()[0] is None
    try:
        with conn:
            conn.execute("INSERT INTO decks (name, created_at) VALUES ('C', 't')")
            raise RuntimeError
    except RuntimeError:
        pass
    db.set_batch(conn, None)
    conn.execute("INSERT INTO decks (name, created_at) VALUES ('D', 't')")
    conn.commit()
    assert [r["batch_id"] for r in log_rows(other, tbl="decks")] == [5, 5, None]
