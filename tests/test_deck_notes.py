from typer.testing import CliRunner

from mtgtracker import db
from mtgtracker.cli import app as cli

from .test_multiuser import _client, two_users  # noqa: F401  (fixture)


def _keen(db_path):
    conn = db.connect(db_path)
    deck_id = conn.execute("SELECT id FROM decks WHERE name = 'Keen Engineering'").fetchone()[0]
    conn.close()
    return deck_id


def test_owner_writes_and_reads_notes(db_path, cache_dir):
    keen = _keen(db_path)
    me = _client(db_path, 1)
    assert "No notes yet" in me.get(f"/decks/{keen}").text
    notes = "Plan: artifacts.\r\nBudget: $5 a card <unless> on theme."
    resp = me.post(f"/decks/{keen}/notes", data={"notes": notes}, follow_redirects=False)
    assert resp.status_code == 303
    assert "notice=Notes+saved." in resp.headers["location"]
    page = me.get(f"/decks/{keen}").text
    expected = "Plan: artifacts.\nBudget: $5 a card &lt;unless&gt; on theme."  # escaped, newlines kept
    assert f'<div class="deck-notes-text">{expected}</div>' in page
    assert me.get(f"/decks/{keen}/notes.txt").text == "Plan: artifacts.\nBudget: $5 a card <unless> on theme."
    cleared = me.post(f"/decks/{keen}/notes", data={"notes": "   "}, follow_redirects=False)
    assert "Notes+cleared." in cleared.headers["location"]
    assert me.get(f"/decks/{keen}/notes.txt").text == ""
    too_long = me.post(f"/decks/{keen}/notes", data={"notes": "x" * 20_001}, follow_redirects=False)
    assert "limited+to+20%2C000" in too_long.headers["location"]


def test_notes_are_private_to_the_owner(db_path, two_users):  # noqa: F811
    keen = two_users["keen"]
    _client(db_path, 1).post(f"/decks/{keen}/notes", data={"notes": "secret plan"})
    friend = _client(db_path, two_users["friend"].id)
    assert "secret plan" not in friend.get(f"/decks/{keen}").text
    assert friend.get(f"/decks/{keen}/notes.txt").status_code == 403
    assert friend.post(f"/decks/{keen}/notes", data={"notes": "mine now"}).status_code == 403


def test_cli_reads_and_sets_notes(db_path, tmp_path):
    keen = _keen(db_path)
    note_file = tmp_path / "notes.txt"
    note_file.write_text("From the CLI\n")
    runner = CliRunner()
    result = runner.invoke(cli, ["deck-notes", str(keen), "--set", str(note_file), "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli, ["deck-notes", str(keen), "--db", str(db_path)])
    assert result.output == "From the CLI\n"
