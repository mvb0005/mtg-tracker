import re

from mtgtracker import db

from .test_multiuser import _client, two_users  # noqa: F401  (fixture)


def _keen(db_path):
    conn = db.connect(db_path)
    deck_id = conn.execute("SELECT id FROM decks WHERE name = 'Keen Engineering'").fetchone()[0]
    conn.close()
    return deck_id


def _bar(html):
    return re.search(r'<nav class="deck-actions".*?</nav>', html, re.S).group(0)


def test_action_bar_owner_has_assemble_modify_export(db_path, cache_dir):
    keen = _keen(db_path)
    bar = _bar(_client(db_path, 1).get(f"/decks/{keen}").text)
    assert f'href="/decks/{keen}/assemble"' in bar
    assert f'href="/decks/{keen}/modify"' in bar
    assert f'href="/decks/{keen}/export"' in bar


def test_action_bar_friend_has_only_export(db_path, two_users):  # noqa: F811
    keen = two_users["keen"]
    html = _client(db_path, two_users["friend"].id).get(f"/decks/{keen}").text
    bar = _bar(html)
    assert f'href="/decks/{keen}/export"' in bar
    assert "/assemble" not in html
    assert "/modify" not in html
    assert 'class="deck-notes"' not in html


def test_notes_summary_is_collapsed_details_with_first_line(db_path, cache_dir):
    keen = _keen(db_path)
    me = _client(db_path, 1)
    assert "Add notes" in me.get(f"/decks/{keen}").text
    me.post(f"/decks/{keen}/notes", data={"notes": "Plan: <artifacts>.\nSecond line."})
    html = me.get(f"/decks/{keen}").text
    tag = re.search(r'<details class="deck-notes"[^>]*>', html).group(0)
    assert "open" not in tag
    summary = re.search(r'<details class="deck-notes".*?</summary>', html, re.S).group(0)
    assert 'deck-notes-first">Plan: &lt;artifacts&gt;.<' in summary
    assert "Second line" not in summary
    assert "Show all" in summary
    assert "Second line" in html  # the full text is in the expanded body


def test_ledger_has_here_away_and_buy_counts(db_path, cache_dir):
    keen = _keen(db_path)
    html = _client(db_path, 1).get(f"/decks/{keen}").text
    ledger = re.search(r'<dl class="ledger">.*?</dl>', html, re.S).group(0)
    assert "<dt>On list</dt>" in ledger
    assert "<dt>Here</dt><dd>100</dd>" in ledger


def test_breakdown_is_collapsed_and_before_the_cards(db_path, cache_dir):
    keen = _keen(db_path)
    html = _client(db_path, 1).get(f"/decks/{keen}").text
    tag = re.search(r'<details class="breakdown"[^>]*>', html).group(0)
    assert "open" not in tag
    assert html.index('class="breakdown"') < html.index('class="catalog"')
