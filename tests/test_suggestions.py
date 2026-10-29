"""Deck suggestions: proposed cards to compare, never part of the deck until
accepted onto the Modify page; dismissed ones stay dismissed."""

from mtgtracker import db
from tests.test_api import API, person, rows, snapshot, system
from tests.test_api import scryfall_api as _scryfall_api
from tests.test_api import world as _world
from tests.test_multiuser import two_users  # noqa: F401

world, scryfall_api = _world, _scryfall_api

# Both are in the fixture fdc cache; neither is on Keen Engineering's list.
ANGEL, LYRA = "Angel of Vitality", "Lyra Dawnbringer"


def _suggest(client, deck_id, *items, **params):
    return client.post(f"{API}/decks/{deck_id}/suggestions", params=params, json={"suggestions": list(items)})


def _deck_state(client, deck_id):
    deck = client.get(f"{API}/decks/{deck_id}").json()
    export = client.get(f"{API}/decks/{deck_id}/export/decklist").json()["text"]
    shopping = client.get(f"{API}/decks/{deck_id}/export/shopping").json()["text"]
    assembly = client.get(f"{API}/decks/{deck_id}/assembly").json()
    return deck["counts"], deck["version"], deck["identity"], export, shopping, assembly


def test_system_user_suggests_and_the_deck_is_untouched(world, scryfall_api):
    keen = world["keen"]
    claude = system(world)
    before = _deck_state(person(world), keen)
    snap = snapshot(world, skip=("service_credentials",))  # a system call touches last_used_at
    item = {"name": ANGEL, "replaces": "counterspell", "reason": "Lifegain.", "theme": "Test",
            "price_cents": 27}
    dry = _suggest(claude, keen, item, dry_run=1)
    assert dry.status_code == 201, dry.text
    assert snapshot(world, skip=("service_credentials",)) == snap  # a dry run leaves nothing

    resp = _suggest(claude, keen, item, {"name": "Counterspell"}, {"name": LYRA})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert len(body["created"]) == 2
    assert body["skipped"] == [{"name": "Counterspell", "reason": "already on the list"}]
    by_name = {s["card_name"]: s for s in body["suggestions"]}
    assert by_name[ANGEL]["cut_card_name"] == "Counterspell"  # the list's own spelling
    assert by_name[ANGEL]["state"] == "open"
    assert by_name[ANGEL]["suggested_by"] == world["claude"].id
    assert by_name[ANGEL]["price_cents"] == 27
    # Suggestions are not on the list: counts, identity, exports, assembly unchanged.
    assert _deck_state(person(world), keen) == before
    # The same card for the same slot again is skipped.
    again = _suggest(claude, keen, item).json()
    assert again["created"] == []
    assert again["skipped"][0]["reason"] == "already suggested"


def test_unknown_cards_and_cuts_are_refused(world, scryfall_api):
    client, keen = person(world), world["keen"]
    resp = _suggest(client, keen, {"name": ANGEL}, {"name": "Adversary by Algorithm"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "cards_not_found"
    assert resp.json()["error"]["names"] == ["Adversary by Algorithm"]
    resp = _suggest(client, keen, {"name": ANGEL, "replaces": LYRA})
    assert (resp.status_code, resp.json()["error"]["code"]) == (422, "rejected")
    assert rows(world, "SELECT COUNT(*) FROM deck_suggestions") == [(0,)]


def test_friends_cannot_see_or_act(world, scryfall_api):
    keen = world["keen"]
    sid = _suggest(person(world), keen, {"name": ANGEL}).json()["created"][0]
    friend = person(world, world["friend"].id)
    assert friend.get(f"{API}/decks/{keen}/suggestions").status_code == 403
    assert friend.get(f"/decks/{keen}/suggestions").status_code == 403
    assert friend.post(f"{API}/decks/{keen}/suggestions/{sid}/accept").status_code == 403
    assert _suggest(friend, keen, {"name": LYRA}).status_code == 403
    assert "Suggestions" not in friend.get(f"/decks/{keen}").text


def test_accept_queues_a_change_and_removing_it_reopens(world, scryfall_api):
    client, keen = person(world), world["keen"]
    sid = _suggest(client, keen, {"name": ANGEL, "replaces": "Island"}).json()["created"][0]
    resp = client.post(f"{API}/decks/{keen}/suggestions/{sid}/accept")
    assert resp.status_code == 200, resp.text
    suggestion = resp.json()["suggestion"]
    assert suggestion["state"] == "queued"
    [change] = client.get(f"{API}/decks/{keen}").json()["pending_changes"]
    assert change["add"]["name"] == ANGEL
    assert not change["add"]["owned"]
    assert change["cut"]["name"] == "Island"
    # Accepting twice is refused.
    again = client.post(f"{API}/decks/{keen}/suggestions/{sid}/accept")
    assert again.status_code == 422
    # Removing the change on the Modify page makes the suggestion open again.
    client.post(f"/decks/{keen}/changes/{change['change_id']}/delete")
    assert client.get(f"{API}/decks/{keen}/suggestions").json()["suggestions"][0]["state"] == "open"
    # Accept again, apply on the Modify page: it's in the new version.
    client.post(f"{API}/decks/{keen}/suggestions/{sid}/accept")
    client.post(f"/decks/{keen}/apply", data={"note": "angel"})
    [s] = client.get(f"{API}/decks/{keen}/suggestions").json()["suggestions"]
    assert (s["state"], s["applied_version"]) == ("applied", 2)


def test_two_suggestions_for_one_slot_cut_different_copies(world, scryfall_api):
    client, keen = person(world), world["keen"]
    islands = rows(world, "SELECT COUNT(*) FROM deck_cards JOIN lots ON lots.id = deck_cards.lot_id"
                          " WHERE deck_id = ? AND card_name = 'Island'", keen)[0][0]
    assert islands >= 2
    ids = _suggest(client, keen, {"name": ANGEL, "replaces": "Island"},
                   {"name": LYRA, "replaces": "Island", "theme": "Low-cost creatures"}).json()["created"]
    for sid in ids:
        assert client.post(f"{API}/decks/{keen}/suggestions/{sid}/accept").status_code == 200
    cuts = [c["cut"]["lot_id"] for c in client.get(f"{API}/decks/{keen}").json()["pending_changes"]]
    assert len(set(cuts)) == 2


def test_dismiss_is_sticky_until_reopened(world, scryfall_api):
    client, keen = person(world), world["keen"]
    sid = _suggest(client, keen, {"name": ANGEL, "replaces": "Island"}).json()["created"][0]
    dismissed = client.post(f"{API}/decks/{keen}/suggestions/{sid}/dismiss").json()
    assert dismissed["suggestion"]["state"] == "dismissed"
    # Not suggested again, for any slot.
    resp = _suggest(system(world), keen, {"name": ANGEL, "replaces": "Counterspell"}).json()
    assert resp["skipped"] == [{"name": ANGEL, "reason": "dismissed before"}]
    assert client.post(f"{API}/decks/{keen}/suggestions/{sid}/accept").status_code == 422
    assert client.post(f"{API}/decks/{keen}/suggestions/{sid}/reopen").json()["suggestion"]["state"] == "open"


def test_web_page_compares_and_acts(world, scryfall_api):
    client, keen = person(world), world["keen"]
    assert "/suggestions" not in client.get(f"/decks/{keen}").text  # no button with nothing open
    ids = _suggest(client, keen, {"name": ANGEL, "replaces": "Island", "reason": "Gains life <b>", "theme":
                                  "Low-cost creatures", "price_cents": 150},
                   {"name": LYRA, "replaces": "Island", "theme": "Low-cost creatures"}).json()["created"]
    deck_page = client.get(f"/decks/{keen}").text
    assert f"/decks/{keen}/suggestions" in deck_page
    page = client.get(f"/decks/{keen}/suggestions")
    assert page.status_code == 200
    html = page.text
    for text in ("Low-cost creatures", "Replacing Island", "2 options"):
        assert text in html
    assert "Gains life &lt;b&gt;" in html  # autoescaped
    assert "$1.50" in html
    resp = client.post(f"/decks/{keen}/suggestions/{ids[0]}/accept", follow_redirects=False)
    assert resp.status_code == 303
    assert "notice=" in resp.headers["location"]
    resp = client.post(f"/decks/{keen}/suggestions/{ids[1]}/dismiss", follow_redirects=False)
    assert "notice=" in resp.headers["location"]
    html = client.get(f"/decks/{keen}/suggestions").text
    assert "queued on Modify" in html
    assert "Reopen" in html
    # Each successful write is audited.
    conn = db.connect(world["db_path"])
    paths = [r[0] for r in conn.execute("SELECT path FROM audit_log ORDER BY id DESC LIMIT 2")]
    base = f"/decks/{keen}/suggestions"
    assert paths == [f"{base}/{ids[1]}/dismiss", f"{base}/{ids[0]}/accept"]


def test_a_reused_change_id_never_links_another_suggestion(world, scryfall_api):
    """Change ids are reused after a delete: removing A's change must not
    leave A pointing at whatever change gets that id next."""
    client, keen = person(world), world["keen"]
    a, b = _suggest(client, keen, {"name": ANGEL, "replaces": "Island"}, {"name": LYRA}).json()["created"]
    change_a = client.post(f"{API}/decks/{keen}/suggestions/{a}/accept").json()["suggestion"]["change_id"]
    client.post(f"/decks/{keen}/changes/{change_a}/delete")
    change_b = client.post(f"{API}/decks/{keen}/suggestions/{b}/accept").json()["suggestion"]["change_id"]
    assert change_b == change_a  # SQLite reused the id
    listing = client.get(f"{API}/decks/{keen}/suggestions").json()["suggestions"]
    states = {s["id"]: s["state"] for s in listing}
    assert states == {a: "open", b: "queued"}
    assert client.post(f"{API}/decks/{keen}/suggestions/{a}/accept").status_code == 200


def test_owned_copy_in_another_deck_says_where(world, scryfall_api):
    client, keen = person(world), world["keen"]
    reign = rows(world, "SELECT id FROM decks WHERE name = 'Reign of Dragons'")[0][0]
    name = rows(world, "SELECT lots.card_name FROM deck_cards JOIN lots ON lots.id = deck_cards.lot_id"
                       " WHERE deck_id = ? AND equipped = 1 AND lots.card_name NOT IN"
                       " (SELECT card_name FROM lots JOIN deck_cards dc ON dc.lot_id = lots.id"
                       "  WHERE dc.deck_id = ?)"
                       " ORDER BY lots.id LIMIT 1", reign, keen)[0][0]
    assert _suggest(client, keen, {"name": name}).status_code == 201
    html = client.get(f"/decks/{keen}/suggestions").text
    assert "in Reign of Dragons; using it takes it from there" in html


def test_votes_leave_the_deck_alone_and_can_be_changed(world, scryfall_api):
    client, keen = person(world), world["keen"]
    up, down = _suggest(client, keen, {"name": ANGEL}, {"name": LYRA}).json()["created"]
    before = _deck_state(client, keen)
    snap = snapshot(world)
    dry = client.post(f"{API}/decks/{keen}/suggestions/{up}/vote", params={"dry_run": 1}, json={"vote": "up"})
    assert dry.status_code == 200, dry.text
    assert snapshot(world) == snap  # a dry run writes nothing

    liked = client.post(f"{API}/decks/{keen}/suggestions/{up}/vote", json={"vote": "up", "note": "Lifegain!"})
    assert liked.status_code == 200, liked.text
    s = liked.json()["suggestion"]
    assert (s["state"], s["vote"], s["vote_note"]) == ("open", "up", "Lifegain!")
    passed = client.post(f"{API}/decks/{keen}/suggestions/{down}/vote", json={"vote": "down"}).json()
    assert (passed["suggestion"]["state"], passed["suggestion"]["vote"]) == ("dismissed", "down")
    assert _deck_state(client, keen) == before  # votes never touch the list
    # A passed card isn't suggested again; a vote can flip either way.
    assert _suggest(system(world), keen, {"name": LYRA}).json()["skipped"][0]["reason"] == "dismissed before"
    flipped = client.post(f"{API}/decks/{keen}/suggestions/{down}/vote", json={"vote": "up"}).json()
    assert (flipped["suggestion"]["state"], flipped["suggestion"]["vote_note"]) == ("open", None)
    # Reopen (the review page's undo) clears a like too.
    reopened = client.post(f"{API}/decks/{keen}/suggestions/{up}/reopen").json()["suggestion"]
    assert (reopened["state"], reopened["vote"], reopened["vote_note"]) == ("open", None, None)
    bad = client.post(f"{API}/decks/{keen}/suggestions/{up}/vote", json={"vote": "maybe"})
    assert bad.status_code == 422
    # A queued suggestion can't be voted on.
    client.post(f"{API}/decks/{keen}/suggestions/{up}/accept")
    assert client.post(f"{API}/decks/{keen}/suggestions/{up}/vote", json={"vote": "down"}).status_code == 422


def test_friends_cannot_vote_or_review(world, scryfall_api):
    keen = world["keen"]
    sid = _suggest(person(world), keen, {"name": ANGEL}).json()["created"][0]
    friend = person(world, world["friend"].id)
    assert friend.post(f"{API}/decks/{keen}/suggestions/{sid}/vote", json={"vote": "up"}).status_code == 403
    assert friend.get(f"/decks/{keen}/suggestions/review").status_code == 403
    assert friend.post(f"/decks/{keen}/suggestions/{sid}/vote", data={"vote": "up"}).status_code == 403


def test_review_page_walks_through_and_undoes(world, scryfall_api):
    client, keen = person(world), world["keen"]
    a, b = _suggest(client, keen, {"name": ANGEL, "reason": "Gains life <b>", "theme": "Lifegain",
                                   "price_cents": 150}, {"name": LYRA}).json()["created"]
    deck_page = client.get(f"/decks/{keen}").text
    assert f"/decks/{keen}/suggestions/review" in deck_page  # unreviewed: the button goes to review
    html = client.get(f"/decks/{keen}/suggestions/review").text
    expected = (ANGEL, "Lifegain", "$1.50", "Gains life &lt;b&gt;", f"/suggestions/{a}/vote", "review.js?v=")
    for text in expected:
        assert text in html
    form = {"vote": "up", "note": "yes", "back": "review"}
    resp = client.post(f"/decks/{keen}/suggestions/{a}/vote", data=form, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith(f"/decks/{keen}/suggestions/review?notice=")
    html = client.get(f"/decks/{keen}/suggestions/review").text
    assert f"/suggestions/{b}/vote" in html  # the next card
    assert f"Last: liked {ANGEL} (yes)." in html
    client.post(f"/decks/{keen}/suggestions/{b}/vote", data={"vote": "down", "back": "review"})
    html = client.get(f"/decks/{keen}/suggestions/review").text
    assert "Every suggestion is reviewed." in html
    # Undo the last vote: Lyra is up for review again.
    client.post(f"/decks/{keen}/suggestions/{b}/reopen", data={"back": "review"})
    assert f"/suggestions/{b}/vote" in client.get(f"/decks/{keen}/suggestions/review").text
    listing = client.get(f"/decks/{keen}/suggestions").text
    assert "liked" in listing
    assert "Review" in listing


def test_a_liked_card_that_reaches_the_list_is_on_list(world, scryfall_api):
    client, keen = person(world), world["keen"]
    sid = _suggest(client, keen, {"name": ANGEL}).json()["created"][0]
    client.post(f"{API}/decks/{keen}/suggestions/{sid}/vote", json={"vote": "up"})
    # As if a whole list had been applied over the API (apply_list.py).
    conn = db.connect(world["db_path"])
    conn.execute("INSERT INTO deck_wants (deck_id, card_name, created_at) VALUES (?, ?, 'now')",
                 (keen, ANGEL))
    conn.commit()
    [s] = client.get(f"{API}/decks/{keen}/suggestions").json()["suggestions"]
    assert s["state"] == "on_list"
    assert "/suggestions" not in client.get(f"/decks/{keen}").text  # nothing open: no button
    assert client.post(f"{API}/decks/{keen}/suggestions/{sid}/vote", json={"vote": "down"}).status_code == 422
