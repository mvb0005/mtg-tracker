"""Deck assembly: the plan's categories and free-copy rules, what applying
writes (and doesn't), the shopping export, the JSON API, the web page, the CLI."""

import re

import pytest

from mtgtracker import assembly, db, deckmods, purchases
from mtgtracker.decks import record_version
from mtgtracker.services import ServiceError
from mtgtracker.services import assembly as service
from mtgtracker.web import queries
from tests.test_api import API, error, person, rows, snapshot, system
from tests.test_api import world as _world
from tests.test_multiuser import two_users  # noqa: F401
from tests.test_purchases import make_plan, shop  # noqa: F401

world = _world
CREDENTIALS = ("service_credentials",)  # a system actor's request stamps last_used_at


# --- a tiny deck builder (plain SQL; nothing here touches Scryfall) -------------------


class Table:
    def __init__(self, conn):
        self.conn = conn

    def deck(self, name, owner=1):
        deck_id = self.conn.execute(
            "INSERT INTO decks (name, format, created_at, owner_id) VALUES (?, 'Commander', '2026-10-01', ?)",
            (name, owner),
        ).lastrowid
        return deck_id

    def lot(self, name, owner=1, sid=None, tx=None, finish="nonfoil"):
        return self.conn.execute(
            "INSERT INTO lots (owner_id, card_name, scryfall_id, transaction_id, finish, acquired_date)"
            " VALUES (?, ?, ?, ?, ?, '2026-10-01')",
            (owner, name, sid, tx, finish),
        ).lastrowid

    def put(self, deck_id, lot_id, equipped):
        self.conn.execute(
            "INSERT INTO deck_cards (deck_id, lot_id, equipped) VALUES (?, ?, ?)",
            (deck_id, lot_id, int(equipped)),
        )

    def want(self, deck_id, name, sid=None):
        return self.conn.execute(
            "INSERT INTO deck_wants (deck_id, card_name, scryfall_id, created_at)"
            " VALUES (?, ?, ?, '2026-10-01')",
            (deck_id, name, sid),
        ).lastrowid

    def purchase(self, vendor="Corner Games", date="2026-10-20"):
        return self.conn.execute(
            "INSERT INTO transactions (vendor, created_at, updated_at, subtotal_cents, tax_cents,"
            " total_cents,"
            " source, owner_id) VALUES (?, ?, ?, 0, 0, 0, 'manual', 1)",
            (vendor, date, date),
        ).lastrowid


@pytest.fixture
def conn(db_path, cache_dir):
    conn = db.connect(db_path)
    yield conn
    conn.close()


@pytest.fixture
def t(conn):
    return Table(conn)


def cats(plan):
    out = {}
    for line in plan.lines:
        out.setdefault(line.category, []).append(line.card_name)
    return {k: sorted(v) for k, v in out.items()}


def state(conn, *tables):
    return {name: [tuple(r) for r in conn.execute(f"SELECT * FROM {name} ORDER BY rowid")] for name in tables}


# --- categories ------------------------------------------------------------------------


def test_every_entry_lands_in_exactly_one_category(conn, t):
    other, mine = t.deck("Other"), t.deck("Mine")
    here = t.lot("Island")
    t.put(mine, here, True)
    t.want(mine, "Sol Ring")
    free_ring = t.lot("Sol Ring", tx=t.purchase())
    away_ct = t.lot("Counterspell")
    t.put(other, away_ct, True)
    t.put(mine, away_ct, False)
    free_ct = t.lot("Counterspell")
    crypt = t.lot("Mana Crypt")
    t.put(other, crypt, True)
    t.put(mine, crypt, False)
    loose = t.lot("Opt")
    t.put(mine, loose, False)
    t.want(mine, "Delver of Secrets")
    t.want(mine, "Mox Opal")
    friend_lot = (
        t.lot("Delver of Secrets", owner=2)
        if conn.execute("SELECT 1 FROM users WHERE id=2").fetchone()
        else None
    )
    if friend_lot is None:
        conn.execute(
            "INSERT INTO users (id, email, display_name, created_at) VALUES (2, 'f@x.org', 'Fay', 't')"
        )
        t.lot("Delver of Secrets", owner=2)
    conn.commit()

    plan = assembly.plan(conn, mine)
    assert cats(plan) == {
        "here": ["Island"],
        "fill": ["Sol Ring"],
        "swap": ["Counterspell"],
        "pull": ["Mana Crypt"],
        "equip": ["Opt"],
        "borrow": ["Delver of Secrets"],
        "acquire": ["Mox Opal"],
    }
    ids = {ln.category: ln.line_id for ln in plan.lines}
    want_id = conn.execute("SELECT id FROM deck_wants WHERE card_name = 'Sol Ring'").fetchone()[0]
    assert ids["fill"] == f"fill:want:{want_id}:lot:{free_ring}"
    assert ids["swap"] == f"swap:lot:{away_ct}:lot:{free_ct}"
    assert ids["pull"] == f"pull:lot:{crypt}"
    assert ids["equip"] == f"equip:lot:{loose}"
    assert ids["here"] is None
    assert ids["borrow"] is None
    assert ids["acquire"] is None
    assert plan.counts == {"here": 1, "away": 3, "wanted": 3}
    assert [ln.default_selected for ln in plan.lines if ln.risky] == [False]
    [fill] = plan.of("fill")
    assert fill.source == "from Corner Games, 2026-10-20"
    assert plan.acquire == [("Mox Opal", 1)]
    assert plan.borrow == [("Delver of Secrets", 1)]
    assert plan.counts_after(list(plan.actionable())) == {"here": 5, "away": 0, "wanted": 2}
    [pull_place] = [p for p in plan.pull_list(list(plan.actionable())) if p["deck_id"] == other]
    assert [i["card_name"] for i in pull_place["items"]] == ["Mana Crypt"]


# --- who is a free copy -----------------------------------------------------------------


def test_a_copy_on_any_list_or_disposed_or_pending_or_someone_elses_is_not_free(conn, t):
    other, mine, third = t.deck("Other"), t.deck("Mine"), t.deck("Third")
    t.conn.execute(
        "INSERT INTO users (id, email, display_name, created_at) VALUES (2, 'f@x.org', 'Fay', 't')"
    )
    for _ in range(5):
        t.want(mine, "Sol Ring")
    listed = t.lot("Sol Ring")
    t.put(other, listed, False)  # on a list, equipped nowhere: still not free
    disposed = t.lot("Sol Ring")
    conn.execute(
        "INSERT INTO dispositions (lot_id, disposed_at, disposition_type) VALUES (?, 't', 'sold')",
        (disposed,),
    )
    pending = t.lot("Sol Ring")
    conn.execute(
        "INSERT INTO deck_changes (deck_id, add_card_name, add_lot_id, created_at)"
        " VALUES (?, 'Sol Ring', ?, 't')",
        (third, pending),
    )
    t.lot("Sol Ring", owner=2)
    free = t.lot("Sol Ring")
    plan = assembly.plan(conn, mine)
    assert [ln.lot_id for ln in plan.of("fill")] == [free]
    assert (len(plan.of("borrow")), len(plan.of("acquire"))) == (1, 3)  # Fay's copy is a friend's, not free
    conn.execute("UPDATE deck_changes SET applied_version = 2")  # no longer pending: free again
    assert sorted(ln.lot_id for ln in assembly.plan(conn, mine).of("fill")) == [pending, free]


def test_one_free_copy_fills_one_slot_and_pinned_printings_come_first(conn, t):
    mine = t.deck("Mine")
    t.want(mine, "Sol Ring", "sid-b")
    t.want(mine, "Sol Ring", None)
    t.want(mine, "Sol Ring", None)
    unpinned = t.lot("Sol Ring")
    other_print = t.lot("Sol Ring", sid="sid-a")
    wanted_print = t.lot("Sol Ring", sid="sid-b")
    plan = assembly.plan(conn, mine)
    fills = {ln.want_id: ln.lot_id for ln in plan.of("fill")}
    first, second, third = sorted(r[0] for r in conn.execute("SELECT id FROM deck_wants"))
    assert fills == {first: wanted_print, second: other_print, third: unpinned}
    assert len(set(fills.values())) == 3
    t.want(mine, "Sol Ring")
    assert len(assembly.plan(conn, mine).of("acquire")) == 1  # a fourth want finds no copy left


def test_a_front_face_name_matches_a_double_faced_copy(conn, t):
    mine = t.deck("Mine")
    t.want(mine, "Delver of Secrets")
    lot = t.lot("Delver of Secrets // Insectile Aberration")
    [fill] = assembly.plan(conn, mine).of("fill")
    assert fill.lot_id == lot
    t.want(mine, "Counterspell // Ice")
    t.lot("Counterspell")  # the front face is enough
    t.want(mine, "Mox Opal")
    t.lot("Mox")  # but a different card is not
    plan = assembly.plan(conn, mine)
    assert [ln.card_name for ln in plan.of("acquire")] == ["Mox Opal"]
    assert len(plan.of("fill")) == 2


def test_a_swap_prefers_the_same_printing_and_basics_count(conn, t):
    other, mine = t.deck("Other"), t.deck("Mine")
    old = t.lot("Island", sid="isl-b")
    t.put(other, old, True)
    t.put(mine, old, False)
    t.lot("Island", sid="isl-a")
    same = t.lot("Island", sid="isl-b")
    [swap] = assembly.plan(conn, mine).of("swap")
    assert (swap.old_lot_id, swap.lot_id) == (old, same)
    assert swap.from_deck_name == "Other"


# --- applying ---------------------------------------------------------------------------


def build_all(conn, t):
    other, mine, bystander = t.deck("Other"), t.deck("Mine"), t.deck("Bystander")
    t.put(mine, t.lot("Island"), True)
    t.want(mine, "Sol Ring")
    t.lot("Sol Ring", tx=t.purchase())
    away_ct = t.lot("Counterspell")
    t.put(other, away_ct, True)
    t.put(mine, away_ct, False)
    t.put(bystander, away_ct, False)
    t.lot("Counterspell")
    crypt = t.lot("Mana Crypt")
    t.put(other, crypt, True)
    t.put(mine, crypt, False)
    t.put(t.deck("Elsewhere"), t.lot("Opt"), True)
    loose = t.lot("Opt")
    t.put(mine, loose, False)
    t.want(mine, "Mox Opal")
    record_version(conn, mine, "Initial list", commit=False)
    conn.commit()
    return {
        "other": other,
        "mine": mine,
        "bystander": bystander,
        "away_ct": away_ct,
        "crypt": crypt,
        "loose": loose,
    }


def test_apply_writes_exactly_the_chosen_lines(conn, t):
    d = build_all(conn, t)
    plan = assembly.plan(conn, d["mine"])
    by_cat = {ln.category: ln for ln in plan.lines}
    other_before = state(conn, "deck_cards")["deck_cards"]
    money_before = [tuple(r) for r in conn.execute("SELECT id, allocated_cost_cents FROM lots ORDER BY id")]
    txns_before = state(conn, "transactions", "products")

    ids = [by_cat[c].line_id for c in ("fill", "swap", "equip")]
    result = assembly.apply(conn, d["mine"], ids)
    assert (result.filled, result.swapped, result.pulled, result.equipped) == (1, 1, 0, 1)
    assert result.version == 2
    assert result.message() == "Assembled: 1 filled, 1 swapped, 0 pulled, 1 equipped. Saved as version 2."

    mine = {
        r["lot_id"]: r["equipped"]
        for r in conn.execute("SELECT * FROM deck_cards WHERE deck_id = ?", (d["mine"],))
    }
    assert mine[by_cat["fill"].lot_id] == 1
    assert mine[by_cat["swap"].lot_id] == 1
    assert d["away_ct"] not in mine  # the away copy left this list...
    assert [
        tuple(r)
        for r in conn.execute(  # ...but stays equipped in the other deck and on a third list
            "SELECT deck_id, equipped FROM deck_cards WHERE lot_id = ? ORDER BY deck_id", (d["away_ct"],)
        )
    ] == [(d["other"], 1), (d["bystander"], 0)]
    assert mine[d["crypt"]] == 0  # the unchosen pull is untouched
    assert mine[d["loose"]] == 1
    assert conn.execute("SELECT COUNT(*) FROM deck_wants WHERE deck_id = ?", (d["mine"],)).fetchone()[0] == 1
    # other decks' rows are exactly as they were
    now = [r for r in state(conn, "deck_cards")["deck_cards"] if r[0] != d["mine"]]
    assert now == [r for r in other_before if r[0] != d["mine"]]
    assert [
        tuple(r) for r in conn.execute("SELECT id, allocated_cost_cents FROM lots ORDER BY id")
    ] == money_before
    assert state(conn, "transactions", "products") == txns_before


def test_a_pull_equips_here_and_unequips_there_and_nothing_else_moves(conn, t):
    d = build_all(conn, t)
    before = state(conn, "deck_cards")["deck_cards"]
    plan = assembly.plan(conn, d["mine"])
    pull = plan.of("pull")[0]
    result = assembly.apply(conn, d["mine"], [pull.line_id])
    assert (result.pulled, result.version) == (1, None)  # only copies moved: no new version
    after = state(conn, "deck_cards")["deck_cards"]
    changed = [(b, a) for b, a in zip(sorted(before), sorted(after), strict=True) if b != a]
    assert sorted(changed) == sorted(
        [
            ((d["other"], d["crypt"], 1), (d["other"], d["crypt"], 0)),
            ((d["mine"], d["crypt"], 0), (d["mine"], d["crypt"], 1)),
        ]
    )
    assert conn.execute("SELECT version FROM decks WHERE id = ?", (d["mine"],)).fetchone()[0] == 1
    assert result.pull_list[0]["name"] == "Other"
    assert result.pull_list[0]["items"][0]["label"] == "from Other"
    assert result.counts_after == plan.counts_after([pull.line_id])


def test_one_version_with_the_new_lots_and_remaining_wants(conn, t):
    d = build_all(conn, t)
    plan = assembly.plan(conn, d["mine"])
    fill, swap = plan.of("fill")[0], plan.of("swap")[0]
    result = assembly.apply(conn, d["mine"], [fill.line_id, swap.line_id], note="  Bought ring  ")
    assert result.version == 2
    [(note,)] = conn.execute("SELECT note FROM deck_versions WHERE deck_id = ? AND version = 2", (d["mine"],))
    assert note == "Bought ring"
    assert set(deckmods.version_lot_ids(conn, d["mine"], 2)) >= {fill.lot_id, swap.lot_id}
    assert d["away_ct"] not in deckmods.version_lot_ids(conn, d["mine"], 2)
    assert deckmods.version_wants(conn, d["mine"], 2) == [("Mox Opal", None)]
    # the name multiset is unchanged, so the history shows no card changes
    v2 = next(v for v in deckmods.versions(conn, d["mine"]) if v["version"] == 2)
    assert (v2["added"], v2["removed"]) == ([], [])
    assert len(deckmods.versions(conn, d["mine"])) == 2
    default = assembly.apply(conn, d["mine"], [assembly.plan(conn, d["mine"]).of("equip")[0].line_id])
    assert default.version is None


def test_default_note_names_the_counts(conn, t):
    d = build_all(conn, t)
    fill = assembly.plan(conn, d["mine"]).of("fill")[0]
    assembly.apply(conn, d["mine"], [fill.line_id])
    [(note,)] = conn.execute("SELECT note FROM deck_versions WHERE deck_id = ? AND version = 2", (d["mine"],))
    assert note == "Assembled: 1 filled, 0 swapped, 0 pulled, 0 equipped"


def test_a_stale_or_unknown_line_refuses_everything(conn, t):
    d = build_all(conn, t)
    plan = assembly.plan(conn, d["mine"])
    good = plan.of("fill")[0].line_id
    before = state(conn, "deck_cards", "deck_wants", "deck_versions", "deck_version_cards")
    for bad in ("fill:want:999:lot:999", "pull:lot:999", "bogus"):
        with pytest.raises(assembly.AssemblyError, match=re.escape(bad)) as exc:
            assembly.apply(conn, d["mine"], [good, bad])
        assert exc.value.stale == [bad]
        assert state(conn, "deck_cards", "deck_wants", "deck_versions", "deck_version_cards") == before
    with pytest.raises(ServiceError) as exc:  # the service says so too (409 over the API)
        service.assemble(conn, d["mine"], [good, "equip:lot:999"])
    assert type(exc.value).__name__ == "StalePlan"
    with pytest.raises(assembly.AssemblyError, match="Nothing selected"):
        assembly.apply(conn, d["mine"], [])
    assembly.apply(conn, d["mine"], [good])
    with pytest.raises(assembly.AssemblyError, match="no longer apply"):  # the same line twice in a row
        assembly.apply(conn, d["mine"], [good])


def test_commit_false_leaves_the_decision_to_the_caller(conn, t):
    d = build_all(conn, t)
    conn.commit()
    before = state(conn, "deck_cards", "deck_wants", "decks")
    assembly.apply(conn, d["mine"], [assembly.plan(conn, d["mine"]).of("fill")[0].line_id], commit=False)
    conn.rollback()
    assert state(conn, "deck_cards", "deck_wants", "decks") == before


def test_a_purchase_then_assembly_turns_the_bought_copies_into_here(conn, shop, t):  # noqa: F811
    mine = t.deck("Animar")
    for name in ("Rhystic Study", "Truce", "Truce"):
        t.want(mine, name)
    conn.commit()
    purchases.record(
        conn,
        make_plan(conn, "1 Rhystic Study, $2.00\n2 Truce, $0.50", vendor="Corner Games", date="2026-10-20"),
    )
    before = conn.execute("SELECT version FROM decks WHERE id = ?", (mine,)).fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM deck_cards WHERE deck_id = ?", (mine,)).fetchone()[0] == 0
    plan = assembly.plan(conn, mine)
    assert len(plan.of("fill")) == 3
    assert not plan.of("acquire")
    assert {ln.source for ln in plan.of("fill")} == {"from Corner Games, 2026-10-20"}
    result = assembly.apply(conn, mine, [ln.line_id for ln in plan.of("fill")])
    assert result.counts_after == {"here": 3, "away": 0, "wanted": 0}
    assert result.version == before + 1
    assert [p["name"] for p in result.pull_list] == ["your collection"]
    assert {i["label"] for i in result.pull_list[0]["items"]} == {"from Corner Games, 2026-10-20"}
    again = assembly.plan(conn, mine)
    assert len(again.of("here")) == 3
    assert not again.actionable()


def test_shopping_export_shrinks_to_the_plans_acquire_list(conn, t):
    mine = t.deck("Mine")
    t.want(mine, "Sol Ring")
    t.want(mine, "Mox Opal")
    t.want(mine, "Mox Opal")
    conn.commit()
    detail = queries.get_deck(conn, mine, viewer_id=1)
    assert queries.deck_exports(detail)["shopping"] == "2 Mox Opal\n1 Sol Ring\n"
    t.lot("Mox Opal")
    conn.commit()
    detail = queries.get_deck(conn, mine, viewer_id=1)
    plan = assembly.plan(conn, mine)
    assert detail["acquire"] == plan.acquire == [("Mox Opal", 1), ("Sol Ring", 1)]
    assert queries.deck_exports(detail)["shopping"] == "1 Mox Opal\n1 Sol Ring\n"
    assert detail["assembly_counts"]["fill"] == 1


def test_the_pull_list_survives_a_round_trip_through_a_url(conn, t):
    d = build_all(conn, t)
    plan = assembly.plan(conn, d["mine"])
    ids = [ln.line_id for ln in plan.lines if ln.line_id]
    result = assembly.apply(conn, d["mine"], ids)
    text = assembly.encode_pull_list(result.pull_list)
    back = assembly.decode_pull_list(conn, d["mine"], text)
    assert [(p["name"], [i["lot_id"] for i in p["items"]]) for p in back] == [
        (p["name"], [i["lot_id"] for i in p["items"]]) for p in result.pull_list
    ]
    # junk and foreign lots drop out
    assert assembly.decode_pull_list(conn, d["mine"], "x,1-2,99999-1") == []


# --- the JSON API --------------------------------------------------------------------------


def api_setup(world):
    conn = db.connect(world["db_path"])
    t = Table(conn)
    d = build_all(conn, t)
    conn.commit()
    conn.close()
    return d


def test_get_plan_is_json_and_writes_nothing(world):
    d = api_setup(world)
    before = snapshot(world)
    resp = person(world).get(f"{API}/decks/{d['mine']}/assembly")
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {
        "here": 1,
        "fill": 1,
        "swap": 1,
        "equip": 1,
        "pull": 1,
        "borrow": 0,
        "acquire": 1,
    }
    pull = next(ln for ln in body["lines"] if ln["category"] == "pull")
    assert pull["risky"]
    assert not pull["default_selected"]
    assert pull["from"]["name"] == "Other"
    assert body["acquire"] == [{"name": "Mox Opal", "qty": 1}]
    assert {p["name"] for p in body["pull_list"]} == {"Other", "your collection"}
    assert snapshot(world) == before


def test_dry_run_changes_nothing_and_leaves_no_trace(world):
    d = api_setup(world)
    plan = person(world).get(f"{API}/decks/{d['mine']}/assembly").json()
    ids = [ln["line_id"] for ln in plan["lines"] if ln["default_selected"] and ln["line_id"]]
    pulls = [ln["line_id"] for ln in plan["lines"] if ln["risky"]]
    before = snapshot(world, skip=CREDENTIALS)
    for client in (person(world), system(world)):
        ok = client.post(f"{API}/decks/{d['mine']}/assembly?dry_run=1", json={"line_ids": ids})
        assert ok.status_code == 200
        assert ok.json()["dry_run"] is True
        assert ok.json()["filled"] == 1
        assert ok.json()["counts_after"] == {"here": 4, "away": 1, "wanted": 1}
        hard = client.post(f"{API}/decks/{d['mine']}/assembly/pulls?dry_run=1", json={"line_ids": pulls})
        assert hard.status_code == 200
        assert hard.json()["dry_run"] is True
        assert hard.json()["preview"]["pulled"] == 1
        assert snapshot(world, skip=CREDENTIALS) == before


def test_a_system_actor_applies_fills_directly(world):
    d = api_setup(world)
    plan = person(world).get(f"{API}/decks/{d['mine']}/assembly").json()
    ids = [ln["line_id"] for ln in plan["lines"] if ln["default_selected"] and ln["line_id"]]
    resp = system(world).post(f"{API}/decks/{d['mine']}/assembly", json={"line_ids": ids, "note": "bought"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["version"], body["filled"], body["swapped"], body["equipped"]) == (2, 1, 1, 1)
    assert body["notice"].startswith("Assembled: 1 filled")
    assert rows(world, "SELECT COUNT(*) FROM deck_wants WHERE deck_id = ?", d["mine"]) == [(1,)]
    [(summary,)] = rows(world, "SELECT summary_json FROM audit_log WHERE path LIKE '%/assembly'")
    assert "Assembled" in summary


def test_pulls_have_their_own_risky_endpoint(world):
    d = api_setup(world)
    pull = f"pull:lot:{d['crypt']}"
    url = f"{API}/decks/{d['mine']}/assembly"
    # the plain endpoint refuses a pull; the pulls endpoint refuses a selection without one
    assert error(person(world).post(url, json={"line_ids": [pull]}))["code"] == "use_pulls_endpoint"
    assert (
        error(person(world).post(f"{url}/pulls", json={"line_ids": [f"equip:lot:{d['loose']}"]}))["code"]
        == "no_pulls"
    )

    claude = system(world)
    first = claude.post(f"{url}/pulls", json={"line_ids": [pull]})
    assert first.status_code == 409
    assert error(first)["code"] == "confirm_required"
    token, preview = first.json()["confirm"], first.json()["preview"]
    assert preview["pulled"] == 1
    assert preview["pull_list"][0]["name"] == "Other"
    assert rows(
        world, "SELECT equipped FROM deck_cards WHERE lot_id = ? AND deck_id = ?", d["crypt"], d["other"]
    ) == [(1,)]
    # a token only works for the selection it was shown
    wrong = claude.post(f"{url}/pulls?confirm={token}", json={"line_ids": [pull, f"equip:lot:{d['loose']}"]})
    assert wrong.status_code == 409
    done = claude.post(f"{url}/pulls", params={"confirm": token}, json={"line_ids": [pull]})
    assert done.status_code == 200, done.text
    assert rows(world, "SELECT equipped FROM deck_cards WHERE lot_id = ? ORDER BY deck_id", d["crypt"]) == [
        (0,),
        (1,),
    ]
    # a person needs no token
    people = person(world)
    d2 = api_setup(world)
    ok = people.post(
        f"{API}/decks/{d2['mine']}/assembly/pulls", json={"line_ids": [f"pull:lot:{d2['crypt']}"]}
    )
    assert ok.status_code == 200
    assert ok.json()["pulled"] == 1


def test_a_stale_line_is_a_409_and_a_friend_gets_403(world):
    d = api_setup(world)
    url = f"{API}/decks/{d['mine']}/assembly"
    before = snapshot(world, restore_dir=False, skip=CREDENTIALS)
    stale = person(world).post(url, json={"line_ids": ["equip:lot:424242"]})
    assert stale.status_code == 409
    assert error(stale)["code"] == "stale_plan"
    assert error(stale)["stale"] == ["equip:lot:424242"]
    assert snapshot(world, restore_dir=False, skip=CREDENTIALS) == before
    friend = person(world, world["friend"].id)
    assert friend.get(url).status_code == 403
    assert friend.post(url, json={"line_ids": [f"equip:lot:{d['loose']}"]}).status_code == 403
    assert friend.post(f"{url}/pulls", json={"line_ids": [f"pull:lot:{d['crypt']}"]}).status_code == 403
    assert system(world, act_for=world["friend"].id).get(url).status_code == 403
    assert snapshot(world, restore_dir=False, skip=CREDENTIALS) == before


# --- the web page ----------------------------------------------------------------------------


def test_the_assemble_page_preview_and_apply(world):
    d = api_setup(world)
    client = person(world)
    page = client.get(f"/decks/{d['mine']}/assemble")
    assert page.status_code == 200
    html = page.text
    for heading in (
        "Fill from your collection",
        "Swap in a free copy",
        "Put in the deck",
        "Pull from another deck",
    ):
        assert heading in html
    assert "Still to acquire" in html
    boxes = re.findall(r'<input type="checkbox" name="line" value="([^"]+)"( checked)?>', html)
    assert {v: bool(c) for v, c in boxes} == {
        next(v for v, _ in boxes if v.startswith("fill:")): True,
        next(v for v, _ in boxes if v.startswith("swap:")): True,
        next(v for v, _ in boxes if v.startswith("equip:")): True,
        f"pull:lot:{d['crypt']}": False,  # a pull is never pre-checked
    }
    assert "takes the card out" in html or "takes the card out of the deck" in html

    chosen = [v for v, _ in boxes]
    audit_before = rows(world, "SELECT COUNT(*) FROM audit_log")
    preview = client.post(f"/decks/{d['mine']}/assemble/preview", data={"line": chosen})
    assert preview.status_code == 200
    assert "Preview" in preview.text
    assert "1 card will leave the decks they are in" in preview.text
    assert re.search(r"In the deck</dt><dd>1 → 5<", preview.text)
    assert rows(world, "SELECT COUNT(*) FROM audit_log") == audit_before  # a preview leaves no audit row
    assert client.post(f"/decks/{d['mine']}/assemble/preview", data={}).status_code == 422

    done = client.post(f"/decks/{d['mine']}/assemble", data={"line": chosen}, follow_redirects=False)
    assert done.status_code == 303
    location = done.headers["location"]
    assert location.startswith(f"/decks/{d['mine']}?notice=Assembled")
    assert "pl=" in location
    deck = client.get(location).text
    assert "Assembled: 1 filled, 1 swapped, 1 pulled, 1 equipped" in deck
    assert "<h2>Pull list</h2>" in deck
    assert re.search(r'<a href="/decks/\d+">Other</a> <span class="subtle">· 1</span>', deck)
    assert "from Corner Games, 2026-10-20" in deck
    assert "Mana Crypt" in deck
    plain = client.get(f"/decks/{d['mine']}").text  # without the param there is no pull list
    assert "<h2>Pull list</h2>" not in plain
    again = client.post(f"/decks/{d['mine']}/assemble", data={"line": chosen}, follow_redirects=False)
    assert again.status_code == 303
    assert "error=" in again.headers["location"]


def test_the_page_is_owner_only_and_the_deck_page_links_it(world):
    d = api_setup(world)
    friend = person(world, world["friend"].id)
    assert friend.get(f"/decks/{d['mine']}/assemble").status_code == 403
    assert friend.post(f"/decks/{d['mine']}/assemble", data={"line": ["equip:lot:1"]}).status_code == 403
    assert (
        friend.post(f"/decks/{d['mine']}/assemble/preview", data={"line": ["equip:lot:1"]}).status_code == 403
    )
    assert f'href="/decks/{d["mine"]}/assemble"' not in friend.get(f"/decks/{d['mine']}").text
    assert f'href="/decks/{d["mine"]}/assemble"' in person(world).get(f"/decks/{d['mine']}").text


def test_what_a_queued_change_will_replace_is_left_alone(conn, t):
    other, mine = t.deck("Other"), t.deck("Mine")
    want = t.want(mine, "Sol Ring")
    t.lot("Sol Ring")  # free, but the want is about to be cut
    away, pulled, loose = t.lot("Counterspell"), t.lot("Mana Crypt"), t.lot("Opt")
    for lot in (away, pulled):
        t.put(other, lot, True)
        t.put(mine, lot, False)
    t.put(mine, loose, False)
    t.lot("Counterspell")  # a free copy that would otherwise make a swap
    for column, value in (("cut_want_id", want), ("cut_lot_id", away), ("cut_lot_id", pulled),
                          ("cut_lot_id", loose)):
        conn.execute(
            f"INSERT INTO deck_changes (deck_id, add_card_name, {column}, created_at)"
            " VALUES (?, 'Rhystic Study', ?, 't')", (mine, value))
    plan = assembly.plan(conn, mine)
    assert not plan.actionable()
    assert plan.acquire == []
    names = sorted(ln.card_name for ln in plan.unavailable)
    assert names == ["Counterspell", "Mana Crypt", "Opt", "Sol Ring"]
    assert {ln.reason for ln in plan.unavailable} == {"queued to be replaced on the Modify page"}
