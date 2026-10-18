"""Recording a purchase: reconciliation and splits, honest pinning, what is
written (and what is not: no deck changes), the JSON API, the web form and the CLI."""

import json
import re
import shutil
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from mtgtracker import db, purchases, scryfall
from mtgtracker.cli import app as cli_app
from mtgtracker.importers.cart_text import CartParseError
from mtgtracker.purchases import CardsNotFoundError, PurchaseError
from mtgtracker.services import ServiceError
from mtgtracker.services import purchases as service
from tests.conftest import install_transport
from tests.test_api import API, error, person, rows, snapshot, system
from tests.test_api import world as _world
from tests.test_multiuser import two_users  # noqa: F401

world = _world  # the API tests' world: two users, a friend's deck, Claude with a credential
CARTS = Path(__file__).parent / "fixtures" / "carts"

SETS = [
    {"code": "fdc", "name": "Fixture Commander"}, {"code": "mh3", "name": "Modern Horizons 3 Commander"},
    {"code": "voc", "name": "Innistrad: Crimson Vow Commander"}, {"code": "inn", "name": "Innistrad"},
    {"code": "hml", "name": "Homelands"}, {"code": "mir", "name": "Mirage"},
    {"code": "pcy", "name": "Prophecy"}, {"code": "otj", "name": "Outlaws of Thunder Junction"},
    {"code": "du1", "name": "Doubles"}, {"code": "du2", "name": "Doubles"},
]


def card(name, set_code, number, finishes=("nonfoil", "foil")):
    return {"id": f"{set_code}-{number}", "name": name, "set": set_code, "collector_number": number,
            "finishes": list(finishes), "type_line": "Artifact", "cmc": 1, "colors": [],
            "color_identity": [], "image_uris": {"normal": f"https://img/{set_code}{number}.jpg"}}


NAMED = {c["name"].lower(): c for c in (
    card("Adarkar Wastes", "mh3", "300"), card("Truce", "hml", "9"), card("Rhystic Study", "pcy", "45"),
    card("Unlicensed Hearse", "otj", "64"), card("Delver of Secrets", "inn", "51"),
    card("Circle of Protection: Red", "mir", "12"), card("Island", "fdc", "9"),
    card("An Offer You Can't Refuse", "ffc", "7"), card("Windborn Muse", "voc", "40"),
)}
NUMBERED = {(c["set"], c["collector_number"]): c for c in NAMED.values()}


@pytest.fixture
def shop(monkeypatch, cache_dir):
    """Scryfall for the purchase tests: the sets list on disk (as fetch_sets
    caches it) and /cards/collection answering for NAMED / NUMBERED. Returns
    the list of identifiers the app asked the network for."""
    (cache_dir / "meta").mkdir()
    (cache_dir / "meta" / "sets.json").write_text(json.dumps(SETS))
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/cards/collection", f"unexpected request {request.url}"
        found = []
        for ident in json.loads(request.content)["identifiers"]:
            asked.append(ident)
            hit = (NUMBERED.get((ident["set"], ident["collector_number"])) if "set" in ident
                   else NAMED.get(ident.get("name", "").lower()))
            if hit:
                found.append(hit)
        return httpx.Response(200, json={"data": found, "not_found": []})

    install_transport(monkeypatch, httpx.MockTransport(handler))
    return asked


@pytest.fixture
def conn(db_path, shop):
    conn = db.connect(db_path)
    yield conn
    conn.close()


def make_plan(conn, text, *, tax=0, shipping=0, total=None, owner=1, vendor="Corner Games",
              date="2026-10-01"):
    cart = purchases.cart_text.parse(text)
    return purchases.plan(
        conn, cart, owner_id=owner, vendor=vendor, date=date, tax_cents=tax, shipping_cents=shipping,
        total_cents=cart.subtotal_cents + tax + shipping if total is None else total,
    )


def only(plan):
    [line] = plan.lines
    return line


# --- reconciliation and splits -------------------------------------------------------------

def test_reconciliation_names_the_difference_to_the_cent(conn):
    assert make_plan(conn, "1 Island, $1.00", total=100).total_cents == 100
    with pytest.raises(PurchaseError, match=r"off by \$0\.01 \(total is higher\)") as exc:
        make_plan(conn, "1 Island, $1.00", total=101)
    assert "lines add up to $1.00" in str(exc.value)
    with pytest.raises(PurchaseError, match=r"off by \$0\.50 \(total is lower\)"):
        make_plan(conn, "1 Island, $1.00", tax=100, total=150)
    with pytest.raises(PurchaseError, match="can't be negative"):
        make_plan(conn, "1 Island, $1.00", tax=-5, total=95)


def test_a_bad_vendor_or_date_is_refused(conn):
    with pytest.raises(PurchaseError, match="vendor"):
        make_plan(conn, "1 Island, $1.00", vendor="  ")
    with pytest.raises(PurchaseError, match="YYYY-MM-DD"):
        make_plan(conn, "1 Island, $1.00", date="10/01/2026")
    with pytest.raises(CartParseError):
        make_plan(conn, "garbage")


def test_tax_and_shipping_spread_exactly_with_ties_by_line_order(conn):
    plan = make_plan(conn, "1 Island, $1.00\n1 Island, $1.00\n1 Island, $1.00", tax=70, shipping=30)
    # 100 over three equal lines: the first gets the odd cent
    assert [p.share_cents for p in plan.lines] == [34, 33, 33]
    assert sum(p.share_cents for p in plan.lines) == 100
    assert sum(p.cost_basis_cents for p in plan.lines) == plan.total_cents == 400
    weighted = make_plan(conn, "1 Island, $3.00\n1 Island, $1.00", tax=10)
    assert [p.share_cents for p in weighted.lines] == [8, 2]  # 7.5 and 2.5: the tie goes to the first line


def test_per_copy_costs_add_up_to_the_lines_cost_basis(conn):
    plan = make_plan(conn, "3 Island, $1.00\n1 Rhystic Study, $2.00", tax=7, shipping=1)
    island = plan.lines[0]
    assert island.cost_basis_cents == 300 + island.share_cents
    assert sum(island.copy_costs) == island.cost_basis_cents
    assert max(island.copy_costs) - min(island.copy_costs) <= 1
    assert sum(sum(p.copy_costs) for p in plan.lines) == plan.total_cents


def test_free_cards_cost_nothing_but_cant_carry_tax(conn):
    assert make_plan(conn, "2 Island, $0.00").total_cents == 0
    with pytest.raises(PurchaseError, match="cost nothing"):
        make_plan(conn, "2 Island, $0.00", tax=50)


# --- pinning ---------------------------------------------------------------------------------

def pinned(plan):
    return (only(plan).scryfall_id, only(plan).set_code, only(plan).collector_number)


def test_set_and_collector_number_pin_a_printing_despite_zero_padding(conn, shop):
    plan = make_plan(conn, "1 Unlicensed Hearse (Outlaws of Thunder Junction, 0064 - Showcase), $0.59")
    assert pinned(plan) == ("otj-64", "otj", "64")
    assert only(plan).line.notes == "Showcase"


def test_a_wrong_collector_number_does_not_pin(conn):
    plan = make_plan(conn, "1 Unlicensed Hearse (Outlaws of Thunder Junction, 0099), $0.59")
    assert pinned(plan) == (None, None, None)
    assert "isn't in OTJ" in only(plan).reason
    assert only(plan).card_name == "Unlicensed Hearse"  # the card is still named


def test_a_number_that_belongs_to_another_card_does_not_pin(conn):
    # otj #64 is the Hearse, not Rhystic Study
    plan = make_plan(conn, "1 Rhystic Study (Outlaws of Thunder Junction, 64), $1.00")
    assert pinned(plan) == (None, None, None)


def test_a_set_with_exactly_one_printing_pins(conn):
    plan = make_plan(conn, "1 Swords to Plowshares (Fixture Commander), $1.00")
    [swords] = [c for c in scryfall.fetch_set_cards("fdc") if c["name"] == "Swords to Plowshares"]
    assert pinned(plan) == (swords["id"], "fdc", "53")


def test_the_printing_must_come_in_the_lines_finish(conn):
    # fdc's only Command Tower is foil
    nonfoil = make_plan(conn, "1 Command Tower (Fixture Commander), $1.00")
    assert pinned(nonfoil) == (None, None, None)
    assert "doesn't come in nonfoil" in only(nonfoil).reason
    foil = make_plan(conn, "1 Command Tower (Fixture Commander, Foil), $1.00")
    assert only(foil).set_code == "fdc"
    assert only(foil).collector_number == "3"


def test_several_printings_in_the_set_stay_unpinned(conn):
    plan = make_plan(conn, "1 Sol Ring (Fixture Commander), $1.00")
    assert pinned(plan) == (None, None, None)
    assert only(plan).reason == "2 printings in FDC"
    assert only(plan).card_name == "Sol Ring"


def test_unknown_ambiguous_uncached_and_missing_sets_stay_unpinned(conn):
    for text, why in [
        ("1 Swords to Plowshares (Outlaws of Thunder Junction Breaking News), $1.00", "isn't a Scryfall set"),
        ("1 Swords to Plowshares (Doubles), $1.00", "isn't a Scryfall set"),  # two sets share the name
        ("1 Truce (Homelands), $1.00", "isn't cached"),  # known set, but never fetched whole just to count
        ("1 Swords to Plowshares, $1.00", "no set"),
    ]:
        plan = make_plan(conn, text)
        assert pinned(plan) == (None, None, None), text
        assert why in only(plan).reason, text


def test_store_set_names_match_with_a_trailing_decks_dropped():
    index = purchases.SetIndex(SETS)
    assert index.code_for("Modern Horizons 3 Commander Decks") == "mh3"
    assert index.code_for("modern horizons 3 commander") == "mh3"
    assert index.code_for("Outlaws of Thunder Junction Breaking News") is None
    assert index.code_for("Doubles") is None
    assert index.code_for("FDC") == "fdc"
    assert index.code_for(None) is None


def test_colons_in_checkout_descriptions_find_the_real_card_and_set(conn):
    text = ("Description\tCond\tQty\tStock\tPrice\tTotal\n"
            "Mirage: Circle of Protection: Red\tNM\t1\t3 left\t0.35\t$0.35\n"
            "Innistrad: Crimson Vow Commander: Windborn Muse\tNM\t1\t3 left\t1.29\t$1.29\n")
    plan = make_plan(conn, text)
    assert [p.card_name for p in plan.lines] == ["Circle of Protection: Red", "Windborn Muse"]


def test_the_sets_list_is_cached_and_never_fetched_without_a_set_name(monkeypatch, cache_dir, db_path):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/sets":
            return httpx.Response(200, json={"data": [{"code": "fdc", "name": "Fixture Commander", "x": 1}]})
        return httpx.Response(200, json={"data": [NAMED["island"]], "not_found": []})

    install_transport(monkeypatch, httpx.MockTransport(handler))
    conn = db.connect(db_path)
    make_plan(conn, "1 Island, $0.10")
    assert "/sets" not in calls  # no set named: no sets request
    make_plan(conn, "1 Island (Fixture Commander), $0.10")
    make_plan(conn, "1 Island (Fixture Commander), $0.10")
    assert calls.count("/sets") == 1
    assert json.loads((cache_dir / "meta" / "sets.json").read_text()) == [
        {"code": "fdc", "name": "Fixture Commander", "set_type": None}]
    assert scryfall.cached_cards()  # the sets list is not mistaken for a set of cards


# --- not found --------------------------------------------------------------------------------

def count_all(conn):
    return [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("transactions", "products", "lots", "deck_cards", "deck_wants")]


def test_unknown_cards_refuse_the_whole_purchase(conn):
    before = count_all(conn)
    with pytest.raises(CardsNotFoundError) as exc:
        make_plan(conn, "1 Island, $0.10\n1 Not A Real Card, $5.00\n1 Zzz Fake, $1.00")
    assert exc.value.names == ["Not A Real Card", "Zzz Fake"]
    assert "2 cards not found" in str(exc.value)
    with pytest.raises(service.CardsNotFound) as coded:
        service.record_purchase(conn, _owner(conn), "1 Not A Real Card, $5.00", vendor="v", date=None,
                                tax_cents=0, shipping_cents=0, total_cents=500)
    assert coded.value.extra["names"] == ["Not A Real Card"]
    assert count_all(conn) == before


def _owner(conn, user_id=1):
    from mtgtracker import users

    return users.get(conn, user_id)


# --- writing -----------------------------------------------------------------------------------

def test_recording_a_fixture_cart_writes_one_transaction_products_and_lots(conn):
    plan = make_plan(conn, (CARTS / "list.txt").read_text(), tax=215, shipping=100)
    before_decks = snapshot_decks(conn)
    summary = purchases.record(conn, plan)
    assert (summary.lines, summary.copies, summary.total_cents) == (12, 19, 3000)
    txn = conn.execute("SELECT * FROM transactions WHERE id = ?", (summary.transaction_id,)).fetchone()
    assert (txn["source"], txn["vendor"], txn["created_at"], txn["owner_id"], txn["currency"]) == (
        "manual", "Corner Games", "2026-10-01", 1, "USD")
    assert (txn["subtotal_cents"], txn["tax_cents"], txn["shipping_cents"], txn["total_cents"]) == (
        2685, 215, 100, 3000)
    products = conn.execute("SELECT * FROM products WHERE transaction_id = ? ORDER BY id",
                            (summary.transaction_id,)).fetchall()
    assert len(products) == 12
    assert sum(p["line_subtotal_cents"] for p in products) == 2685
    assert sum(p["tax_allocated_cents"] for p in products) == 315
    assert sum(p["cost_basis_cents"] for p in products) == 3000
    lots = conn.execute("SELECT * FROM lots WHERE transaction_id = ? ORDER BY id",
                        (summary.transaction_id,)).fetchall()
    assert len(lots) == 19
    assert sum(lot["allocated_cost_cents"] for lot in lots) == 3000
    for p in products:
        costs = [lot["allocated_cost_cents"] for lot in lots if lot["product_id"] == p["id"]]
        assert len(costs) == p["qty"]
        assert sum(costs) == p["cost_basis_cents"]
    assert {lot["acquired_date"] for lot in lots} == {"2026-10-01"}
    assert {lot["owner_id"] for lot in lots} == {1}
    sol = next(lot for lot in lots if lot["card_name"] == "Sol Ring")
    assert (sol["scryfall_id"], sol["notes"], sol["finish"]) == (None, "Condition: LP", "nonfoil")
    foil = next(lot for lot in lots if lot["card_name"] == "Command Tower")
    assert (foil["finish"], foil["set_code"]) == ("foil", "fdc")
    on_lists = conn.execute("SELECT COUNT(*) FROM deck_cards WHERE lot_id IN"
                            " (SELECT id FROM lots WHERE transaction_id = ?)",
                            (summary.transaction_id,)).fetchone()[0]
    assert on_lists == 0
    assert snapshot_decks(conn) == before_decks
    assert summary.unpinned == sum(1 for lot in lots if lot["scryfall_id"] is None)


def snapshot_decks(conn):
    return (
        [tuple(r) for r in conn.execute("SELECT * FROM deck_cards ORDER BY deck_id, lot_id")],
        [tuple(r) for r in conn.execute("SELECT * FROM deck_wants ORDER BY id")],
        [tuple(r) for r in conn.execute("SELECT id, version FROM decks ORDER BY id")],
    )


def test_wanted_by_counts_matching_wants_and_changes_no_deck(conn):
    decks = [r["id"] for r in conn.execute("SELECT id FROM decks ORDER BY id")]
    for deck, name, n in [(decks[0], "Island", 2), (decks[0], "Windborn Muse", 1), (decks[1], "Island", 5),
                          (decks[1], "Delver of Secrets // Insectile Aberration", 1)]:
        for _ in range(n):
            conn.execute("INSERT INTO deck_wants (deck_id, card_name, created_at) VALUES (?, ?, 't')",
                         (deck, name))
    friend = conn.execute("INSERT INTO users (email, display_name, created_at) VALUES"
                          " ('f@example.com', 'F', 't')").lastrowid
    fdeck = conn.execute("INSERT INTO decks (name, format, created_at, owner_id)"
                         " VALUES ('Theirs', 'Commander', 't', ?)", (friend,)).lastrowid
    conn.execute("INSERT INTO deck_wants (deck_id, card_name, created_at) VALUES (?, 'Island', 't')",
                 (fdeck,))
    conn.commit()
    before = snapshot_decks(conn)
    plan = make_plan(conn, "3 Island, $0.10\n1 Delver of Secrets, $0.25\n1 Rhystic Study, $1.00")
    by_deck = {w["deck_id"]: w for w in plan.wanted_by}
    assert set(by_deck) == {decks[0], decks[1]}  # never the friend's deck
    assert by_deck[decks[0]]["copies"] == 2  # min(3 new Islands, 2 wanted); no Muse in this cart
    assert by_deck[decks[1]]["copies"] == 4  # 3 Islands + the Delver, matched by its front face
    cards = {c["name"]: c["copies"] for c in by_deck[decks[1]]["cards"]}
    assert cards == {"Island": 3, "Delver of Secrets": 1}
    purchases.record(conn, plan)
    assert snapshot_decks(conn) == before  # a purchase never touches a deck


def test_commit_false_leaves_the_transaction_to_the_caller(conn):
    before = count_all(conn)
    purchases.record(conn, make_plan(conn, "2 Island, $0.10"), commit=False)
    assert count_all(conn)[:3] == [before[0] + 1, before[1] + 1, before[2] + 2]
    conn.rollback()
    assert count_all(conn) == before


# --- undoing a purchase ----------------------------------------------------------------------------

def record(conn, text="2 Island, $0.10\n1 Rhystic Study, $1.00"):
    return purchases.record(conn, make_plan(conn, text)).transaction_id


def test_an_unused_purchase_can_be_deleted(conn):
    tid = record(conn)
    notice, preview = service.delete_purchase(conn, tid)
    assert notice == "Removed the purchase from Corner Games: 3 cards."
    assert (preview["lots"], preview["total_cents"], preview["vendor"]) == (3, 120, "Corner Games")
    assert conn.execute("SELECT COUNT(*) FROM lots WHERE transaction_id = ?", (tid,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM products WHERE transaction_id = ?", (tid,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE id = ?", (tid,)).fetchone()[0] == 0


@pytest.mark.parametrize(("setup", "blocker"), [
    ("INSERT INTO deck_cards (deck_id, lot_id) VALUES (1, {lot})", "on_deck_lists"),
    ("INSERT INTO dispositions (lot_id, disposed_at, disposition_type)"
     " VALUES ({lot}, 't', 'sold')", "disposed"),
    ("INSERT INTO deck_costs (transaction_id, description, amount_cents, acquired_date)"
     " VALUES ({tid}, 'x', 1, 't')", "has_deck_costs"),
    ("INSERT INTO receipts (transaction_id, sha256, original_filename, mime_type, file_ext,"
     " imported_at, kind)"
     " VALUES ({tid}, 'abc', 'r.pdf', 'application/pdf', 'pdf', 't', 'pdf')", "has_receipts"),
])
def test_a_purchase_in_use_is_refused_and_nothing_changes(conn, setup, blocker):
    tid = record(conn)
    lot = conn.execute("SELECT MIN(id) FROM lots WHERE transaction_id = ?", (tid,)).fetchone()[0]
    conn.execute(setup.format(lot=lot, tid=tid))
    conn.commit()
    before = count_all(conn)
    with pytest.raises(service.PurchaseInUseError) as exc:
        service.delete_purchase(conn, tid)
    assert blocker in exc.value.extra["blockers"]
    assert count_all(conn) == before


def test_a_wallet_order_cannot_be_undone_here(conn):
    wallet = conn.execute("SELECT MIN(id) FROM transactions WHERE source = 'wallet_order'").fetchone()[0]
    with pytest.raises(ServiceError, match="recorded by hand"):
        service.delete_purchase(conn, wallet)


# --- JSON API ------------------------------------------------------------------------------------

CART = "2 Island, $0.10\n1 Rhystic Study (Prophecy), $1.00"


def body(**over):
    return {"vendor": "Corner Games", "date": "2026-10-01", "tax_cents": 10, "shipping_cents": 0,
            "total_cents": 130, "cart": CART, **over}


def test_api_dry_run_writes_nothing_and_matches_the_real_run(world, shop):
    client = person(world)
    before = snapshot(world)
    dry = client.post(f"{API}/purchases?dry_run=1", json=body())
    assert dry.status_code == 201, dry.text
    assert snapshot(world) == before  # no rows, no audit, no changelog, no restore point
    real = client.post(f"{API}/purchases", json=body())
    assert real.status_code == 201
    dry_body, real_body = dry.json(), real.json()
    assert dry_body.pop("dry_run") is True
    for key in ("transaction_id", "line_detail"):  # the ids a dry run used are rolled back
        dry_body.pop(key), real_body.pop(key)
    dry_notice, real_notice = dry_body.pop("notice"), real_body.pop("notice")
    assert dry_notice.startswith("Would record")
    assert real_notice.startswith("Recorded")
    assert dry_body == real_body


def test_api_create_list_and_get(world, shop):
    client = person(world)
    created = client.post(f"{API}/purchases", json=body())
    assert created.status_code == 201, created.text
    summary = created.json()
    counts = (summary["lines"], summary["copies"], summary["total_cents"], summary["unpinned"])
    assert counts == (2, 3, 130, 3)
    # 10 cents of tax over $0.20 and $1.00 is 1.67 and 8.33: 2 and 8
    assert [d["cost_basis_cents"] for d in summary["line_detail"]] == [22, 108]
    assert summary["line_detail"][1]["needs_printing_because"].startswith("set PCY isn't cached")
    pid = summary["transaction_id"]
    listing = client.get(f"{API}/purchases?source=manual").json()["purchases"]
    assert [(p["id"], p["copies"], p["total_cents"]) for p in listing] == [(pid, 3, 130)]
    detail = client.get(f"{API}/purchases/{pid}").json()
    assert [(line["card"], line["qty"], line["cost_basis_cents"]) for line in detail["lines"]] == [
        ("Island", 2, 22), ("Rhystic Study", 1, 108)]
    assert [lot["cost_cents"] for lot in detail["lines"][0]["lots"]] == [11, 11]
    lots = [lot for line in detail["lines"] for lot in line["lots"]]
    assert all(lot["needs_printing"] and not lot["on_a_deck_list"] for lot in lots)
    audit = rows(world, "SELECT action FROM audit_log ORDER BY id DESC LIMIT 1")
    assert audit == [("add_purchase",)]


def test_api_purchases_are_private(world, shop):
    pid = person(world).post(f"{API}/purchases", json=body()).json()["transaction_id"]
    friend = person(world, world["friend"].id)
    assert error(friend.get(f"{API}/purchases/{pid}"))["code"] == "not_found"
    assert friend.get(f"{API}/purchases").json() == {"purchases": []}
    assert friend.delete(f"{API}/purchases/{pid}").status_code == 404
    mine = friend.post(f"{API}/purchases", json=body()).json()["transaction_id"]
    assert rows(world, "SELECT owner_id FROM transactions WHERE id = ?", mine) == [(world["friend"].id,)]
    assert rows(world, "SELECT DISTINCT owner_id FROM lots WHERE transaction_id = ?", mine) == [
        (world["friend"].id,)]


def test_api_refusals_write_nothing(world, shop):
    client = person(world)
    before = snapshot(world, restore_dir=False)
    off = client.post(f"{API}/purchases", json=body(total_cents=131))
    assert off.status_code == 422
    assert error(off)["code"] == "rejected"
    assert "off by $0.01" in error(off)["message"]
    fake = client.post(f"{API}/purchases", json=body(cart="1 Zzz Not Real, $1.00", total_cents=110))
    assert (fake.status_code, error(fake)["code"]) == (422, "cards_not_found")
    assert error(fake)["names"] == ["Zzz Not Real"]
    junk = client.post(f"{API}/purchases", json=body(cart="what"))
    assert junk.status_code == 422
    assert "line 1" in error(junk)["message"]
    missing = client.post(f"{API}/purchases", json={"vendor": "x", "cart": CART})
    assert error(missing)["code"] == "validation_error"
    assert snapshot(world, restore_dir=False) == before


def test_api_a_person_deletes_an_unused_purchase(world, shop):
    client = person(world)
    pid = client.post(f"{API}/purchases", json=body()).json()["transaction_id"]
    dry = client.delete(f"{API}/purchases/{pid}?dry_run=1")
    assert dry.status_code == 200
    assert dry.json()["preview"]["lots"] == 3
    assert rows(world, "SELECT COUNT(*) FROM lots WHERE transaction_id = ?", pid) == [(3,)]
    done = client.delete(f"{API}/purchases/{pid}")
    assert done.status_code == 200
    assert done.json()["notice"].startswith("Removed the purchase")
    assert rows(world, "SELECT COUNT(*) FROM transactions WHERE id = ?", pid) == [(0,)]
    assert client.delete(f"{API}/purchases/{pid}").status_code == 404


def test_api_a_system_actor_must_confirm_deleting(world, shop):
    claude = system(world)
    pid = claude.post(f"{API}/purchases", json=body()).json()["transaction_id"]
    first = claude.delete(f"{API}/purchases/{pid}")
    assert first.status_code == 409
    assert error(first)["code"] == "confirm_required"
    assert first.json()["preview"]["lots"] == 3
    assert rows(world, "SELECT COUNT(*) FROM lots WHERE transaction_id = ?", pid) == [(3,)]
    done = claude.delete(f"{API}/purchases/{pid}", params={"confirm": first.json()["confirm"]})
    assert done.status_code == 200, done.text
    assert rows(world, "SELECT COUNT(*) FROM lots WHERE transaction_id = ?", pid) == [(0,)]


def test_api_delete_is_refused_while_a_lot_is_on_a_deck_list(world, shop):
    client = person(world)
    pid = client.post(f"{API}/purchases", json=body()).json()["transaction_id"]
    [(lot,)] = rows(world, "SELECT MIN(id) FROM lots WHERE transaction_id = ?", pid)
    conn = db.connect(world["db_path"])
    conn.execute("INSERT INTO deck_cards (deck_id, lot_id, equipped) VALUES (?, ?, 0)", (world["keen"], lot))
    conn.commit()
    conn.close()
    blocked = client.delete(f"{API}/purchases/{pid}")
    assert blocked.status_code == 409
    assert error(blocked)["code"] == "purchase_in_use"
    assert error(blocked)["blockers"] == {"on_deck_lists": 1}
    assert rows(world, "SELECT COUNT(*) FROM lots WHERE transaction_id = ?", pid) == [(3,)]


# --- web ---------------------------------------------------------------------------------------

FORM = {"cart": CART, "vendor": "Corner Games", "date": "2026-10-01", "tax": "0.10", "shipping": "",
        "total": "$1.30"}


def test_the_purchases_page_links_to_the_form(client, shop):
    assert 'href="/purchases/new"' in client.get("/transactions").text
    assert "Record a purchase" in client.get("/purchases/new").text


def test_web_preview_then_record(client, shop, db_path):
    def counts():
        return (rows_of(db_path, "SELECT COUNT(*) FROM transactions"),
                rows_of(db_path, "SELECT COUNT(*) FROM audit_log"))

    before = counts()
    preview = client.post("/purchases/preview", data=FORM)
    assert preview.status_code == 200
    text = preview.text
    assert "needs printing" in text
    assert "$1.30" in text
    assert "Record this purchase" in text
    assert re.search(r"The lines add up to \$1\.20", text)
    assert counts() == before  # a preview leaves no audit row either
    done = client.post("/purchases", data=FORM, follow_redirects=False)
    assert done.status_code == 303
    location = done.headers["location"]
    assert location.startswith("/transactions/")
    assert "notice=" in location
    page = client.get(location)
    assert "Recorded 3 cards" in page.text
    assert "Corner Games" in page.text
    assert "Island" in page.text
    assert "$1.30" in page.text
    assert "Recorded by hand" in page.text
    assert rows_of(db_path, "SELECT COUNT(*) FROM transactions") == before[0] + 1


def rows_of(db_path, sql):
    conn = db.connect(db_path)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def test_web_mistakes_redisplay_the_form_with_the_message(client, shop, db_path):
    off = client.post("/purchases/preview", data={**FORM, "total": "1.31"})
    assert off.status_code == 422
    assert "off by $0.01" in off.text
    assert "Island" in off.text
    bad = client.post("/purchases", data={**FORM, "total": "twelve"}, follow_redirects=False)
    assert bad.status_code == 422
    assert "isn&#39;t an amount" in bad.text
    fake = client.post("/purchases", data={**FORM, "cart": "1 Zzz Not Real, $1.20"}, follow_redirects=False)
    assert fake.status_code == 422
    assert "Zzz Not Real" in fake.text
    assert rows_of(db_path, "SELECT COUNT(*) FROM transactions WHERE source = 'manual'") == 0


def test_web_preview_shows_which_decks_want_the_cards(client, shop, db_path):
    conn = db.connect(db_path)
    conn.execute("INSERT INTO deck_wants (deck_id, card_name, created_at) VALUES (1, 'Island', 't')")
    conn.commit()
    conn.close()
    text = client.post("/purchases/preview", data=FORM).text
    assert "Decks that want these" in text
    assert "Nothing is added to a deck" in text


def test_a_wallet_order_page_is_unchanged_by_the_new_section(client, shop):
    page = client.get("/transactions/1").text
    assert "Recorded by hand" not in page
    assert "<h2>Cards</h2>" not in page


# --- CLI ------------------------------------------------------------------------------------------

def test_cli_dry_run_then_record(db_path, shop, tmp_path, monkeypatch):
    cart = tmp_path / "cart.txt"
    cart.write_text(CART)
    args = ["record-purchase", str(cart), "--vendor", "Corner Games", "--total", "1.30", "--tax", "0.10",
            "--date", "2026-10-01", "--db", str(db_path)]
    runner = CliRunner()
    dry = runner.invoke(cli_app, [*args, "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "Would record 3 cards on 2 lines, $1.30 (3 need a printing)" in dry.output
    assert rows_of(db_path, "SELECT COUNT(*) FROM transactions WHERE source = 'manual'") == 0
    done = runner.invoke(cli_app, args)
    assert done.exit_code == 0, done.output
    assert "Recorded purchase" in done.output
    assert rows_of(db_path, "SELECT COUNT(*) FROM lots WHERE transaction_id IN"
                   " (SELECT id FROM transactions WHERE source = 'manual')") == 3
    off = runner.invoke(cli_app, [*args[:5], "9.99", *args[6:]])
    assert off.exit_code != 0
    assert "off by" in off.output


def test_cache_dir_fixture_is_untouched_by_other_tests(cache_dir):
    assert shutil
    assert (cache_dir / "fdc.json").exists()


def test_dry_run_notice_says_would_record():
    summary = purchases.PurchaseSummary(
        transaction_id=None, lines=1, copies=2, total_cents=34, unpinned=0,
        wanted_by=[{"deck_id": 5, "deck": "D", "copies": 1, "cards": []}], detail=[],
    )
    assert summary.message().startswith("Recorded 2 cards")
    dry = summary.message(dry_run=True)
    assert dry.startswith("Would record 2 cards")
    assert "nothing would be added" in dry
