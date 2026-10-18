"""The deckbuilding skill's scripts: the API client's config and header rules,
apply_list's diff, and apply_list / who_owns end to end against the app."""

import collections
import sys
from pathlib import Path

import httpx
import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / ".claude" / "skills" / "mtg-deckbuilding" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _api  # noqa: E402
import apply_list  # noqa: E402
import who_owns  # noqa: E402

from tests.test_api import (  # noqa: E402
    API,
    CLIENT_ID,
    person,
    rows,
    scryfall_api,  # noqa: F401
    system,
    two_users,  # noqa: F401
    world,  # noqa: F401
)

SECRET = "s3cr3t-value"


@pytest.fixture(autouse=True)
def _reset_client():
    yield
    _api.set_client(None, None)


# --- .env parsing and config --------------------------------------------------------

def test_env_value_follows_minikube_sh_rules():
    text = (
        "# comment\n"
        "MTG_BASE_URL=http://old\n"
        "  export MTG_BASE_URL =  'https://x.example/'  \r\n"
        'CF_ACCESS_CLIENT_ID="abc.access"\n'
        "CF_ACCESS_CLIENT_SECRET=plain secret  \n"
        "MTG_ACT_FOR2=9\n"
        "EMPTY=\n"
        "MIXED=\"not closed'\n"
    )
    assert _api.env_value("MTG_BASE_URL", text) == "https://x.example/"  # last wins, quotes stripped
    assert _api.env_value("CF_ACCESS_CLIENT_ID", text) == "abc.access"
    assert _api.env_value("CF_ACCESS_CLIENT_SECRET", text) == "plain secret"
    assert _api.env_value("MTG_ACT_FOR", text) == ""  # a longer name is not a match
    assert _api.env_value("EMPTY", text) == ""
    assert _api.env_value("MIXED", text) == "\"not closed'"
    assert _api.env_value("NOPE", text) == ""


def test_the_env_file_is_parsed_not_executed(tmp_path):
    marker = tmp_path / "ran"
    env = tmp_path / ".env"
    env.write_text(f"MTG_ACT_FOR=$(touch {marker})\nMTG_BASE_URL=https://h.example\n")
    cfg = _api.load_config(environ={}, env_file=env)
    assert cfg.base_url == "https://h.example"
    assert cfg.act_for.startswith("$(touch")
    assert not marker.exists()


def test_environment_beats_the_env_file_and_defaults_apply(tmp_path):
    env = tmp_path / ".env"
    env.write_text("MTG_ACT_FOR=3\nMTG_BASE_URL=https://file.example/\n")
    cfg = _api.load_config(environ={"MTG_ACT_FOR": "7"}, env_file=env)
    assert (cfg.act_for, cfg.base_url) == ("7", "https://file.example")
    bare = _api.load_config(environ={}, env_file=tmp_path / "missing")
    assert (bare.base_url, bare.act_for, bare.client_id, bare.dev_service) == (
        "", "1", "", "")  # no default site: MTG_BASE_URL must be set


def test_secrets_are_not_in_the_repr():
    cfg = _api.Config("https://h", "id", SECRET, "1", "")
    assert SECRET not in repr(cfg)
    assert SECRET not in str(cfg)


SERVICE_HEADERS = {"CF-Access-Client-Id", "CF-Access-Client-Secret", "X-Act-For"}


@pytest.mark.parametrize(("cfg", "expected"), [
    (_api.Config("b", "", "", "1", ""), set()),  # nothing: no identity headers at all
    (_api.Config("b", "id", "", "1", ""), set()),  # half a service token: none, and no X-Act-For
    (_api.Config("b", "", SECRET, "1", ""), set()),
    (_api.Config("https://h.example", "id", SECRET, "4", ""), SERVICE_HEADERS),
    (_api.Config("http://127.0.0.1:8733", "id", SECRET, "4", ""), set()),  # never over plain HTTP
    (_api.Config("b", "", "", "4", "dev.id"), {"X-Dev-Service", "X-Act-For"}),
])
def test_header_rules(cfg, expected):
    headers = _api.headers_for(cfg)
    assert set(headers) - {"Accept"} == expected
    if "X-Act-For" in headers:
        assert headers["X-Act-For"] == cfg.act_for


# --- errors never echo credentials ---------------------------------------------------------

def _client_answering(handler):
    return httpx.Client(base_url="https://h.example", transport=httpx.MockTransport(handler))


def test_errors_carry_the_api_code_and_never_credentials():
    cfg = _api.Config("https://h.example", "id.access", SECRET, "1", "")

    def handler(request):
        assert request.headers["CF-Access-Client-Secret"] == SECRET  # sent as a header...
        return httpx.Response(422, json={"error": {"code": "rejected", "message": "no such card"}},
                              headers={"X-Echo": SECRET})
    _api.set_client(_client_answering(handler), cfg)
    with pytest.raises(_api.ApiError) as exc:
        _api.post("/decks", {"name": "x"})
    assert (exc.value.status, exc.value.code, exc.value.message) == (422, "rejected", "no such card")
    assert SECRET not in str(exc.value)
    assert "id.access" not in str(exc.value)

    redirect = httpx.Response(302, headers={"Location": "https://login"})
    _api.set_client(_client_answering(lambda r: redirect), cfg)
    with pytest.raises(_api.ApiError) as login:
        _api.get("/me")
    assert login.value.code == "not_api"
    assert "login" not in str(login.value)

    def boom(request):
        raise httpx.ConnectError(f"refused with {SECRET}")
    _api.set_client(_client_answering(boom), cfg)
    with pytest.raises(_api.ApiError) as down:
        _api.get("/me")
    assert down.value.code == "network"
    assert SECRET not in str(down.value)


def test_a_confirm_request_is_returned_never_auto_confirmed():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(409, json={"error": {"code": "confirm_required", "message": "confirm"},
                                         "preview": {"lots": 4}, "confirm": "tok"})
    _api.set_client(_client_answering(handler), _api.Config("https://h.example", "", "", "1", ""))
    with pytest.raises(_api.ConfirmRequired) as asked:
        _api.delete("/imports/1")
    assert (asked.value.preview, asked.value.token) == ({"lots": 4}, "tok")
    assert len(calls) == 1
    assert "confirm" not in calls[0].url.params


# --- apply_list's diff -------------------------------------------------------------------------

def _counts(*names):
    counter = collections.Counter(_key(n) for n in names)
    return counter, {_key(n): n for n in names}


def _key(name):
    from _common import key
    return key(name)


def test_diff_lists_pairs_adds_and_cuts_by_front_face_name():
    current, cnames = _counts("Sol Ring", "Island", "Island", "Delver of Secrets // Insectile Aberration")
    target, tnames = _counts("Sol Ring", "Island", "Delver of Secrets", "Rhystic Study")
    plan = apply_list.diff_lists(current, cnames, target, tnames)
    assert plan == {"adds": ["Rhystic Study"], "cuts": ["Island"]}


def test_diff_lists_with_nothing_to_do_and_a_size_mismatch():
    same, names = _counts("Sol Ring", "Island")
    assert apply_list.diff_lists(same, names, same, names) == {"adds": [], "cuts": []}
    bigger, bnames = _counts("Sol Ring", "Island", "Command Tower")
    with pytest.raises(ValueError, match="target has 3 cards, deck has 2"):
        apply_list.diff_lists(same, names, bigger, bnames)


def test_current_counts_include_wants_and_request_body_accepts_names_or_ids():
    deck = {"cards": [{"name": "Island"}, {"name": "Island"}], "wants": [{"name": "Rhystic Study"}]}
    counts, names = apply_list.current_counts(deck)
    assert counts[_key("Island")] == 2
    assert names[_key("Rhystic Study")] == "Rhystic Study"
    body = apply_list.request_body({"adds": ["A", {"lot_id": 5}], "cuts": [{"want_id": 2}]}, "n", 3)
    assert body == {"adds": [{"name": "A"}, {"lot_id": 5}], "cuts": [{"want_id": 2}], "note": "n",
                    "expect_version": 3}


# --- end to end against the app ---------------------------------------------------------------------

def _use(client, world_):
    _api.set_client(client, _api.Config("http://testserver", "", "", "1", CLIENT_ID))


def test_apply_list_dry_runs_then_applies_and_verifies(world, scryfall_api, tmp_path, capsys):  # noqa: F811
    keen = world["keen"]
    client = system(world)
    _use(client, world)
    text = client.get(f"{API}/decks/{keen}/export/decklist").json()["text"]
    lines = text.splitlines()
    index = next(i for i, line in enumerate(lines) if line.split(" (")[0] == "1 Counterspell")
    lines[index] = "1 Rhystic Study"
    target = tmp_path / "target.txt"
    target.write_text("\n".join(lines))

    apply_list.main([str(keen), "--target", str(target), "--plan"])
    out = capsys.readouterr().out
    assert "+ Rhystic Study   - Counterspell" in out
    assert "dry run:" in out
    assert "applied:" not in out
    assert rows(world, "SELECT version FROM decks WHERE id = ?", keen) == [(1,)]

    apply_list.main([str(keen), "--target", str(target), "--note", "swap"])
    out = capsys.readouterr().out
    assert "version 1 -> 2" in out
    assert "deck now matches the target: True" in out
    assert rows(world, "SELECT version, note FROM deck_versions WHERE deck_id = ? AND version = 2", keen) == [
        (2, "swap")]

    target.write_text(text + "\n1 Command Tower\n")
    with pytest.raises(SystemExit, match="target has"):
        apply_list.main([str(keen), "--target", str(target)])


def test_apply_list_refuses_a_deck_with_pending_changes(world, scryfall_api, tmp_path):  # noqa: F811
    person(world).post(f"/decks/{world['keen']}/changes", data={"planned_name": "Rhystic Study"})
    _use(system(world), world)
    target = tmp_path / "t.txt"
    target.write_text("1 Island")
    with pytest.raises(SystemExit, match="pending changes"):
        apply_list.main([str(world["keen"]), "--target", str(target)])


def test_who_owns_prints_one_line_per_card(world, capsys):  # noqa: F811
    _use(system(world), world)
    who_owns.main(["Counterspell", "Rhystic Study", "No Such Card"])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("Counterspell: Alex: 1 in not in a deck; Owner: 1 in ")
    assert lines[1] == "Rhystic Study: Alex: 1 in Alex Blue"
    assert lines[2] == "No Such Card: nobody"


@pytest.mark.parametrize(("me_answer", "sends_act_for"), [
    (httpx.Response(200, json={"principal": {"id": 1, "kind": "person"},
                               "actor": {"id": 1, "kind": "person"}}), False),  # single mode
    (httpx.Response(400, json={"error": {"code": "act_for_required", "message": "say who"}}), True),
])
def test_x_act_for_only_when_the_server_sees_a_system_user(me_answer, sends_act_for):
    """With credentials configured, a site still in single mode refuses
    X-Act-For (403), so the client asks /me once and sends it only when the
    server identified a system user."""
    cfg = _api.Config("https://h.example", "id.access", SECRET, "1", "")
    seen = []

    def handler(request):
        if request.url.path.endswith("/me") and "X-Act-For" not in request.headers:
            return me_answer
        seen.append("X-Act-For" in request.headers)
        return httpx.Response(200, json={"decks": []})
    _api.set_client(_client_answering(handler), cfg)
    _api.get("/decks")
    _api.get("/decks")
    assert seen == [sends_act_for, sends_act_for]
