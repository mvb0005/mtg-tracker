import json

import httpx
import pytest
from fastapi.testclient import TestClient

from mtgtracker import db, printings, scryfall, users
from mtgtracker.importers import collection_csv
from mtgtracker.importers.collection_csv import CollectionImportError, import_collection, parse
from mtgtracker.web.app import create_app
from mtgtracker.web.auth import DEV_COOKIE, AuthConfig

from .conftest import FIXTURES, install_transport

VALKYRIE = "9577d16a-e6ed-4d98-b90f-2abfc9b9586a"  # fdc 58, in the fixture cache
TRITON = "22dc3c27-78ca-42fd-b434-15a813c07eed"    # fdc 115

MANABOX = (
    "Name,Set code,Set name,Collector number,Foil,Rarity,Quantity,ManaBox ID,Scryfall ID,Purchase price,"
    "Misprint,Altered,Signed,Condition,Language,Proxy,Purchase price currency,Added\n"
    f"Youthful Valkyrie,FDN,Foundations,58,normal,uncommon,3,1,{VALKYRIE},0.35,"
    "false,false,false,near_mint,en,false,USD,2026-05-20T03:46:02.155Z\n"
    f"Mire Triton,FDN,Foundations,115,foil,uncommon,1,2,{TRITON},1.10,"
    "false,false,false,lightly_played,ja,false,USD,2026-05-21T00:00:00.000Z\n"
)


def _fdc(collector_number):
    cards = json.loads((FIXTURES / "scryfall_fdc.json").read_text())
    return next(c for c in cards if c["collector_number"] == collector_number)


def test_parse_manabox():
    parsed = parse(MANABOX)
    assert parsed.format == "ManaBox"
    valkyrie, triton = parsed.rows
    assert (valkyrie.qty, valkyrie.finish, valkyrie.price_cents) == (3, "nonfoil", 35)
    assert valkyrie.scryfall_id == VALKYRIE
    assert valkyrie.acquired_date == "2026-05-20"
    assert valkyrie.notes is None
    assert (triton.finish, triton.price_cents) == ("foil", 110)
    assert triton.notes == "Condition: lightly_played; Language: ja"


def test_parse_moxfield_archidekt_and_plain():
    moxfield = parse(
        "Count,Tradelist Count,Name,Edition,Condition,Language,Foil,Tags,Last Modified,Collector Number,"
        "Alter,Proxy,Purchase Price\n"
        '2,0,"Sol Ring",fdc,Near Mint,English,etched,,2026-01-02 10:00:00.000000,286,False,False,\n'
    )
    assert moxfield.format == "Moxfield"
    row = moxfield.rows[0]
    assert (row.qty, row.set_code, row.collector_number) == (2, "fdc", "286")
    assert (row.finish, row.price_cents) == ("etched", None)
    assert row.acquired_date == "2026-01-02"

    archidekt = parse(
        "Quantity,Name,Finish,Condition,Date Added,Language,Purchase Price,Edition Name,Edition Code,"
        "Scryfall ID,Collector Number\n"
        f"1,Youthful Valkyrie,Foil,NM,2026-02-03,English,$2.50,Foundations,FDN,{VALKYRIE},58\n"
    )
    assert archidekt.format == "Archidekt"
    row = archidekt.rows[0]
    assert (row.finish, row.price_cents, row.set_code, row.scryfall_id) == ("foil", 250, "fdn", VALKYRIE)

    plain = parse("card name\nSol Ring\nCounterspell\n")
    assert plain.format == "CSV"
    assert [(r.name, r.qty, r.finish) for r in plain.rows] == [
        ("Sol Ring", 1, "nonfoil"), ("Counterspell", 1, "nonfoil"),
    ]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "empty"),
        ("Quantity,Set\n1,fdc\n", "no Name column"),
        ("Name,Quantity\n", "no cards"),
        ("Name,Quantity\nSol Ring,two\n", "line 2: quantity"),
        ("Name,Quantity\nSol Ring,1\n,1\n", "line 3: no card name"),
        ("Name,Foil\nSol Ring,shiny\n", "line 2: unknown foil"),
        ("Name,Purchase price\nSol Ring,0.355\n", "line 2: price"),
    ],
)
def test_parse_rejects_with_the_line(text, message):
    with pytest.raises(CollectionImportError, match=message):
        parse(text)


def test_non_usd_price_is_kept_as_a_note_not_cost():
    row = parse("Name,Purchase price,Purchase price currency\nSol Ring,2.00,EUR\n").rows[0]
    assert row.price_cents is None
    assert row.notes == "Purchase price: 2.00 EUR"


def test_import_creates_one_lot_per_copy_from_the_cache(db_path, cache_dir):
    """Scryfall IDs already cached resolve with no network at all (cache_dir
    fails any request)."""
    conn = db.connect(db_path)
    friend = users.create(conn, "Alex")
    summary = import_collection(
        conn, MANABOX.encode(), owner_id=friend.id, imported_by=1, filename="bulk.csv"
    )
    assert (summary.copies, summary.distinct, summary.priced, summary.cost_cents) == (4, 2, 4, 3 * 35 + 110)
    assert summary.message() == "Imported 4 cards (2 distinct) from a ManaBox export."
    lots = conn.execute(
        "SELECT owner_id, transaction_id, scryfall_id, card_name, set_code, collector_number, finish,"
        " allocated_cost_cents, acquired_date, collection_import_id"
        " FROM lots WHERE owner_id = ? ORDER BY id",
        (friend.id,),
    ).fetchall()
    valkyrie = (VALKYRIE, "Youthful Valkyrie", "fdc", "58", "nonfoil", 35, "2026-05-20")
    triton = (TRITON, "Mire Triton", "fdc", "115", "foil", 110, "2026-05-21")
    assert [tuple(r) for r in lots] == [
        (friend.id, None, *card, summary.import_id) for card in [valkyrie] * 3 + [triton]
    ]
    record = conn.execute("SELECT * FROM collection_imports WHERE id = ?", (summary.import_id,)).fetchone()
    assert (record["owner_id"], record["imported_by"]) == (friend.id, 1)
    assert (record["format"], record["card_count"]) == ("ManaBox", 4)

    # The same file imports once per collection...
    with pytest.raises(CollectionImportError, match="already imported"):
        import_collection(conn, MANABOX.encode(), owner_id=friend.id, imported_by=1)
    # ...but someone else may import an identical file into theirs.
    import_collection(conn, MANABOX.encode(), owner_id=1, imported_by=1)
    conn.close()


def test_import_never_guesses_a_printing(db_path, cache_dir, monkeypatch):
    """Only a Scryfall ID or set + number pins a printing. A stale ID falls
    back to its set + number; a name-only match (or a stale ID with nothing
    else) names the card but stays unpinned; a card Scryfall can't find
    still becomes an unpinned lot. All of them land in the printings queue."""
    sol_ring = _fdc("286")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/cards/collection":
            idents = json.loads(request.content)["identifiers"]
            found = [sol_ring for i in idents if i.get("name") == "Sol Ring"]
            return httpx.Response(200, json={"data": found, "not_found": []})
        return httpx.Response(404, json={"object": "error", "type": "not_found"})

    install_transport(monkeypatch, httpx.MockTransport(handler))
    stale = "00000000-0000-0000-0000-000000000000"
    text = (
        "Name,Quantity,Scryfall ID,Set code,Collector number\n"
        "Sol Ring,1,,,\n"
        f"Sol Ring,1,{stale},,\n"
        f"Mire Triton,1,{stale},fdc,115\n"
        "Not A Real Card,2,,,\n"
    )
    conn = db.connect(db_path)
    summary = import_collection(conn, text.encode(), owner_id=1, imported_by=1)
    assert summary.unresolved == ["Not A Real Card"]
    assert summary.unpinned == 4
    assert summary.message() == (
        "Imported 5 cards (3 distinct) from a CSV export. 1 couldn't be found on Scryfall: Not A Real Card."
        " 4 need a printing picked (the file didn't say which)."
    )
    rows = conn.execute(
        "SELECT card_name, scryfall_id, set_code, collector_number FROM lots"
        " WHERE collection_import_id = ? ORDER BY id",
        (summary.import_id,),
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("Sol Ring", None, None, None), ("Sol Ring", None, None, None),
        ("Mire Triton", TRITON, "fdc", "115"),
        ("Not A Real Card", None, None, None), ("Not A Real Card", None, None, None),
    ]
    imported = {lot_id for (lot_id,) in conn.execute(
        "SELECT id FROM lots WHERE collection_import_id = ? AND scryfall_id IS NULL", (summary.import_id,)
    )}
    queued = {i for g in printings.unresolved_groups(conn, 1)
              for i in printings.group_for_lot(conn, g["lot_id"]).lot_ids}
    assert len(imported) == 4
    assert imported <= queued  # every unpinned copy is in the printings queue
    assert requests.count("/cards/named") == 1  # one fuzzy retry, for the one miss
    conn.close()


def test_one_import_is_capped(db_path, cache_dir, monkeypatch):
    monkeypatch.setattr(collection_csv, "MAX_COPIES", 5)
    conn = db.connect(db_path)
    with pytest.raises(CollectionImportError, match="6 cards is more than 5"):
        import_collection(conn, b"Name,Quantity\nSol Ring,3\nCounterspell,3\n", owner_id=1, imported_by=1)
    conn.close()


def test_scryfall_outage_writes_nothing(db_path, cache_dir, monkeypatch):
    install_transport(monkeypatch, httpx.MockTransport(lambda r: httpx.Response(503)))
    conn = db.connect(db_path)
    before = conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0]
    with pytest.raises(httpx.HTTPStatusError):
        import_collection(conn, b"Name\nSol Ring\n", owner_id=1, imported_by=1)
    assert conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0] == before
    assert conn.execute("SELECT COUNT(*) FROM collection_imports").fetchone()[0] == 0
    conn.close()


def test_fetch_collection_by_id_uses_the_cache_first(cache_dir, monkeypatch):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["identifiers"])
        return httpx.Response(200, json={"data": [], "not_found": []})

    install_transport(monkeypatch, httpx.MockTransport(handler))
    cards = scryfall.fetch_collection(
        [{"id": VALKYRIE}, {"set": "fdc", "collector_number": "115"}, {"id": "missing"}]
    )
    assert [c and c["id"] for c in cards] == [VALKYRIE, TRITON, None]
    assert calls == [[{"id": "missing"}]]


# --- web --------------------------------------------------------------------------


def _client(db_path, user_id):
    client = TestClient(create_app(db_path, auth=AuthConfig(mode="dev")))
    client.cookies.set(DEV_COOKIE, str(user_id))
    return client


def _upload(client, person_id, text=MANABOX, name="bulk.csv"):
    return client.post(
        f"/people/{person_id}/import", files={"file": (name, text.encode(), "text/csv")},
        follow_redirects=False,
    )


def test_admin_imports_for_a_friend_who_has_not_signed_in(db_path, cache_dir):
    conn = db.connect(db_path)
    friend = users.create(conn, "Alex", "alex@example.com")
    conn.close()
    me = _client(db_path, 1)
    assert "Import a collection" in me.get(f"/people/{friend.id}").text
    resp = _upload(me, friend.id)
    assert resp.status_code == 303
    assert "notice=Imported+4+cards" in resp.headers["location"]
    page = me.get(f"/people/{friend.id}").text
    assert "4 cards from bulk.csv (ManaBox)" in page

    # Their cards are theirs: in their collection with their cost, not in yours.
    alex = _client(db_path, friend.id)
    cards = alex.get("/cards", params={"view": "list"}).text
    assert "Youthful Valkyrie" in cards
    assert "$1.10" in cards
    assert "Youthful Valkyrie" not in me.get("/cards", params={"view": "list"}).text
    friend_view = me.get(f"/people/{friend.id}/cards", params={"view": "list"}).text
    assert "Youthful Valkyrie" in friend_view
    assert "$1.10" not in friend_view
    assert '"cost": "$' not in friend_view  # hidden, not shown as $0.00

    again = _upload(me, friend.id)
    assert "already+imported" in again.headers["location"]


def test_import_permissions(db_path, cache_dir):
    conn = db.connect(db_path)
    friend = users.create(conn, "Alex", "alex@example.com")
    users.touch(conn, friend.id)  # they've signed in: they manage their own collection now
    conn.close()
    me = _client(db_path, 1)
    assert "Import a collection" not in me.get(f"/people/{friend.id}").text
    assert _upload(me, friend.id).status_code == 403
    alex = _client(db_path, friend.id)
    assert _upload(alex, 1).status_code == 403
    assert _upload(alex, friend.id).status_code == 303  # their own


def test_single_mode_imports_only_into_your_own(db_path, cache_dir):
    """In single mode every visitor is the admin, so nobody may act for a
    friend from the web there."""
    conn = db.connect(db_path)
    friend = users.create(conn, "Alex", "alex@example.com")
    conn.close()
    client = TestClient(create_app(db_path))
    assert "Import a collection" not in client.get(f"/people/{friend.id}").text
    assert _upload(client, friend.id).status_code == 403
    assert "Import a collection" in client.get("/people/1").text
    assert _upload(client, 1).status_code == 303


def test_upload_errors_come_back_as_messages(db_path, cache_dir, monkeypatch):
    me = _client(db_path, 1)
    assert "Choose+a+CSV" in me.post("/people/1/import", data={}, follow_redirects=False).headers["location"]
    bad = _upload(me, 1, text="Quantity\n1\n")
    assert "no+Name+column" in bad.headers["location"]
    monkeypatch.setattr("mtgtracker.web.app.MAX_UPLOAD_BYTES", 10)
    assert "too+big" in _upload(me, 1).headers["location"]
    assert collection_csv.MAX_ROWS > 0


def test_cached_cards_rechecks_cheaply_but_sees_new_and_rewritten_files(cache_dir, monkeypatch):
    """A page does hundreds of lookups: between full checks only the two
    cache directories are statted. New files show up at once; a file
    rewritten in place shows up after FULL_CHECK_SECONDS (or at once when
    this process wrote it)."""
    import os
    from pathlib import Path

    first = scryfall.cached_cards()
    globs = []
    real_glob = Path.glob
    monkeypatch.setattr(Path, "glob", lambda self, pattern: globs.append(pattern) or real_glob(self, pattern))
    for _ in range(50):
        assert scryfall.cached_cards() is first
    assert globs == []  # no per-file scan between full checks

    card = dict(_fdc("58"), id="11111111-1111-1111-1111-111111111111")
    (cache_dir / "cards").mkdir(exist_ok=True)
    (cache_dir / "cards" / f"{card['id']}.json").write_text(json.dumps(card))
    assert card["id"] in {c["id"] for c in scryfall.cached_cards()}  # new file: seen at once

    # Rewritten in place by another process: seen at the next full check.
    path = cache_dir / "cards" / f"{card['id']}.json"
    path.write_text(json.dumps(dict(card, name="Renamed Valkyrie")))
    stamp = path.stat().st_mtime_ns + 1_000_000_000  # writes inside one fs tick share an mtime
    os.utime(path, ns=(stamp, stamp))
    monkeypatch.setattr(scryfall, "FULL_CHECK_SECONDS", 0.0)
    assert "Renamed Valkyrie" in {c["name"] for c in scryfall.cached_cards()}
