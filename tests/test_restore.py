"""Restore points: taking, the web rules for when, and retention."""

import gzip
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from mtgtracker import db, restore, users
from mtgtracker.web import deps
from mtgtracker.web.app import create_app
from mtgtracker.web.auth import AuthConfig

ADD = {"display_name": "Sam", "email": "sam@example.com"}


def dump(conn):
    out = {}
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
        " ORDER BY name"
    ).fetchall()
    for (table,) in tables:
        out[table] = [tuple(r) for r in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
    return out


def points(db_path):
    return restore.list_points(restore.restore_dir(db_path))


def test_take_makes_a_valid_gzipped_copy_with_a_sidecar(db_path, tmp_path):
    point = restore.take(db_path, "People Add!")
    assert point.parent == db_path.parent / "restore"
    assert point.name.endswith("-people-add.db.gz")
    assert not [p for p in point.parent.iterdir() if p.name.startswith(".")]  # no temp files left
    out = tmp_path / "unzipped.db"
    out.write_bytes(gzip.decompress(point.read_bytes()))
    live, copy = sqlite3.connect(db_path), sqlite3.connect(out)
    assert dump(copy) == dump(live)
    (info,) = points(db_path)
    seq = live.execute("SELECT MAX(seq) FROM changelog").fetchone()[0]
    assert (info["changelog_seq"], info["user_version"], info["action"]) == (seq, 9, "people-add")
    assert info["size"] == point.stat().st_size


def test_ordinary_writes_take_a_point_at_most_every_ten_minutes(db_path, cache_dir):
    client = TestClient(create_app(db_path, auth=AuthConfig(mode="single")))
    conn = db.connect(db_path)
    deck = conn.execute("SELECT id FROM decks").fetchone()[0]
    client.get("/decks")
    assert points(db_path) == []  # reads never do
    client.post(f"/decks/{deck}/notes", data={"notes": "a"})
    client.post(f"/decks/{deck}/notes", data={"notes": "b"})
    (first,) = points(db_path)
    assert first["action"] == "deck-save-notes"
    rows = conn.execute("SELECT restore_point FROM audit_log ORDER BY id").fetchall()
    assert [r[0] for r in rows] == [first["name"], first["name"]]


def test_an_old_enough_point_is_replaced(db_path, cache_dir, monkeypatch):
    client = TestClient(create_app(db_path, auth=AuthConfig(mode="single")))
    deck = db.connect(db_path).execute("SELECT id FROM decks").fetchone()[0]
    old = datetime.now(UTC) - timedelta(minutes=11)
    restore.take(db_path, "earlier", now=old)
    client.post(f"/decks/{deck}/notes", data={"notes": "a"})
    assert [p["action"] for p in points(db_path)] == ["earlier", "deck-save-notes"]


def test_a_request_that_fails_or_has_no_route_takes_no_point(db_path, cache_dir):
    client = TestClient(create_app(db_path, auth=AuthConfig(mode="single")))
    assert client.post("/nowhere", data={}).status_code == 404
    evil = {"Origin": "https://evil.example"}
    assert client.post("/decks/1/notes", data={"notes": "x"}, headers=evil).status_code == 403
    assert points(db_path) == []


def test_a_snapshot_failure_never_breaks_the_request(db_path, cache_dir, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(restore, "take", boom)
    client = TestClient(create_app(db_path, auth=AuthConfig(mode="single")))
    deck = db.connect(db_path).execute("SELECT id FROM decks").fetchone()[0]
    assert client.post(f"/decks/{deck}/notes", data={"notes": "kept"}).status_code == 200
    assert client.get(f"/decks/{deck}/notes.txt").text == "kept"


def test_risky_routes_always_take_a_point(db_path, cache_dir):
    client = TestClient(create_app(db_path, auth=AuthConfig(mode="dev")))
    assert client.post("/people", data=ADD).status_code == 200
    assert client.post("/people", data={**ADD, "email": "sam2@example.com"}).status_code == 200
    assert [p["action"] for p in points(db_path)] == ["people-add", "people-add"]
    audits = db.connect(db_path).execute("SELECT restore_point FROM audit_log ORDER BY id").fetchall()
    assert [a[0] for a in audits] == [p["name"] for p in points(db_path)]


def test_a_dry_run_takes_no_point(db_path, cache_dir):
    conn = db.connect(db_path)
    claude = users.create_system(conn, "Claude")
    users.add_credential(conn, claude.id, "client-id-1", "t")
    conn.close()
    client = TestClient(create_app(db_path, auth=AuthConfig(mode="dev")))
    headers = {"X-Dev-Service": "client-id-1", "X-Act-For": "1"}
    resp = client.post("/people", data=ADD, headers=headers)
    assert resp.status_code == 409
    assert points(db_path) == []
    done = client.post("/people", data={**ADD, "confirm": resp.json()["confirm"]}, headers=headers)
    assert done.status_code == 200
    assert len(points(db_path)) == 1


def test_prune_keeps_fifty_plus_the_newest_of_each_day(tmp_path):
    directory = tmp_path / "restore"
    directory.mkdir()
    now = datetime(2026, 10, 18, 20, 0, tzinfo=UTC)
    stamps = []
    for day in range(120):
        for hour in (3, 15):  # two a day
            stamps.append((now - timedelta(days=day)).replace(hour=hour))
    for when in stamps:
        stem = f"{when.strftime(restore.STAMP)}-x"
        (directory / f"{stem}.db.gz").write_bytes(b"")
        (directory / f"{stem}.json").write_text("{}")
    lone = (now - timedelta(days=200)).strftime(restore.STAMP)
    (directory / f"{lone}-nosidecar.db.gz").write_bytes(b"")  # the only one that day, no sidecar
    (directory / "notes.txt").write_text("not a point")

    deleted = restore.prune(directory)
    kept = restore.list_points(directory)
    newest = sorted(stamps)[-50:]
    per_day = {w.date(): max(x for x in stamps if x.date() == w.date()) for w in stamps}
    expect = set(newest) | set(per_day.values())
    assert {p["created_at"] for p in kept} >= expect
    assert len(kept) == len(expect) + 1  # + the lone old point
    assert len(deleted) + len(kept) == 241
    assert len({p["created_at"].date() for p in kept}) == 121  # every day is still recoverable
    assert (directory / "notes.txt").exists()
    # Sidecars go with their points.
    assert len(list(directory.glob("*.json"))) == len([p for p in kept if "nosidecar" not in p["name"]])
    restore.prune(directory)  # idempotent


@pytest.mark.parametrize("name", ["x.db.gz", "20260101T000000000000Z", "notaday-x.db.gz"])
def test_list_ignores_strangers(tmp_path, name):
    (tmp_path / name).write_bytes(b"")
    assert restore.list_points(tmp_path) == []
    assert restore.prune(tmp_path) == []
    assert deps  # imported for the registry of web helpers
