"""Network-free tests for mtgtracker/scryfall.py.

CACHE_DIR is always pointed at tmp_path, and the fetch path runs against an
httpx.MockTransport injected by wrapping httpx.Client, so no test can reach
api.scryfall.com.
"""

import json

import httpx
import pytest

from mtgtracker import scryfall
from mtgtracker.scryfall import _normalize, fetch_set_cards, find_matches, index_by_name

_REAL_CLIENT = httpx.Client


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    cache_dir = tmp_path / "scryfall_cache"
    monkeypatch.setattr(scryfall, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(scryfall.time, "sleep", lambda _s: None)
    return cache_dir


@pytest.fixture
def mock_scryfall(monkeypatch):
    """Install a MockTransport. Returns (set_handler, requests) where
    set_handler(fn) sets the request handler and requests records every
    request made."""
    state = {"handler": None}
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return state["handler"](request)

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_CLIENT(*args, **kwargs)

    monkeypatch.setattr(scryfall.httpx, "Client", client_factory)

    def set_handler(fn):
        state["handler"] = fn

    return set_handler, requests


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("scryfall tried to open a network client")

    monkeypatch.setattr(scryfall.httpx, "Client", refuse)


def _card(name, number, set_code="tst", **extra):
    card = {"id": f"id-{set_code}-{number}", "name": name, "set": set_code, "collector_number": str(number)}
    return card | extra


# --- _normalize ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Sol Ring", "sol ring"),
        ("SOL RING", "sol ring"),
        ("  Sol   Ring  ", "sol ring"),
        ("Lathliss, Dragon Queen", "lathliss dragon queen"),
        ("Urza's Saga", "urzas saga"),
        ("Fire // Ice", "fire ice"),
        ("Lim-Dul's Vault", "limduls vault"),
        # Only plain spaces survive the character filter, so other whitespace
        # is dropped rather than collapsed (harmless: card names don't use it).
        ("Sol\tRing\n", "solring"),
        ("", ""),
    ],
)
def test_normalize(raw, expected):
    assert _normalize(raw) == expected


def test_normalize_drops_non_ascii_letters_instead_of_transliterating():
    # Documents current behavior: accented/ligature letters are stripped,
    # not transliterated, so "Æther Vial" and "Aether Vial" don't match.
    # Both sides of a lookup go through _normalize, so a name spelled the
    # same way as Scryfall's still matches.
    assert _normalize("Lim-Dûl's Vault") == "limdls vault"
    assert _normalize("Æther Vial") == "ther vial"
    assert _normalize("Æther Vial") != _normalize("Aether Vial")


# --- index_by_name / find_matches ---


def test_index_groups_multiple_printings_under_one_name():
    cards = [_card("Sol Ring", 1), _card("Command Tower", 2), _card("Sol Ring", 3)]
    index = index_by_name(cards)

    assert set(index) == {"sol ring", "command tower"}
    assert [c["collector_number"] for c in index["sol ring"]] == ["1", "3"]


def test_find_matches_is_case_and_punctuation_insensitive():
    index = index_by_name([_card("Lathliss, Dragon Queen", 1), _card("Sol Ring", 2)])

    assert [c["collector_number"] for c in find_matches(index, "lathliss dragon queen")] == ["1"]
    assert [c["collector_number"] for c in find_matches(index, "SOL  RING")] == ["2"]


def test_find_matches_unknown_name_returns_empty():
    index = index_by_name([_card("Sol Ring", 1)])
    assert find_matches(index, "Black Lotus") == []
    assert find_matches({}, "Sol Ring") == []


def test_index_of_empty_card_list():
    assert index_by_name([]) == {}


# --- fetch_set_cards ---


def test_cache_hit_makes_no_network_call(isolated_cache, no_network):
    isolated_cache.mkdir()
    cached = [_card("Sol Ring", 1)]
    (isolated_cache / "tst.json").write_text(json.dumps(cached))

    assert fetch_set_cards("tst") == cached


def test_fetch_paginates_and_writes_cache(isolated_cache, mock_scryfall):
    set_handler, requests = mock_scryfall
    page2_url = "https://api.scryfall.com/cards/search?page=2&q=e%3Atst&unique=prints&order=set"

    def handler(request):
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json={"has_more": False, "data": [_card("Island", 3)]})
        return httpx.Response(
            200,
            json={
                "has_more": True,
                "next_page": page2_url,
                "data": [_card("Sol Ring", 1), _card("Command Tower", 2)],
            },
        )

    set_handler(handler)

    cards = fetch_set_cards("tst")

    assert [c["name"] for c in cards] == ["Sol Ring", "Command Tower", "Island"]
    assert len(requests) == 2

    first = requests[0]
    assert first.url.host == "api.scryfall.com"
    assert first.url.path == "/cards/search"
    assert first.url.params["q"] == "e:tst"
    assert first.url.params["unique"] == "prints"
    assert first.headers["User-Agent"] == scryfall.USER_AGENT
    assert first.headers["Accept"] == "application/json"
    assert str(requests[1].url) == page2_url

    cache_file = isolated_cache / "tst.json"
    assert cache_file.exists()
    assert json.loads(cache_file.read_text()) == cards


def test_fetch_stops_when_has_more_false_even_with_next_page(mock_scryfall):
    set_handler, requests = mock_scryfall
    set_handler(
        lambda _r: httpx.Response(
            200,
            json={"has_more": False, "next_page": "https://api.scryfall.com/x", "data": [_card("A", 1)]},
        )
    )

    assert len(fetch_set_cards("tst")) == 1
    assert len(requests) == 1


def test_second_fetch_uses_cache(mock_scryfall):
    set_handler, requests = mock_scryfall
    set_handler(lambda _r: httpx.Response(200, json={"has_more": False, "data": [_card("A", 1)]}))

    first = fetch_set_cards("tst")
    second = fetch_set_cards("tst")

    assert first == second
    assert len(requests) == 1


def test_use_cache_false_refetches_and_overwrites_cache(isolated_cache, mock_scryfall):
    isolated_cache.mkdir()
    (isolated_cache / "tst.json").write_text(json.dumps([_card("Stale", 1)]))

    set_handler, requests = mock_scryfall
    set_handler(lambda _r: httpx.Response(200, json={"has_more": False, "data": [_card("Fresh", 1)]}))

    cards = fetch_set_cards("tst", use_cache=False)

    assert [c["name"] for c in cards] == ["Fresh"]
    assert len(requests) == 1
    assert json.loads((isolated_cache / "tst.json").read_text()) == cards


def test_http_error_raises_and_does_not_write_cache(isolated_cache, mock_scryfall):
    set_handler, _requests = mock_scryfall
    set_handler(lambda _r: httpx.Response(500, json={"object": "error"}))

    with pytest.raises(httpx.HTTPStatusError):
        fetch_set_cards("tst")

    assert not (isolated_cache / "tst.json").exists()


def test_error_on_later_page_does_not_write_partial_cache(isolated_cache, mock_scryfall):
    set_handler, requests = mock_scryfall

    def handler(request):
        if request.url.params.get("page") == "2":
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "has_more": True,
                "next_page": "https://api.scryfall.com/cards/search?page=2",
                "data": [_card("A", 1)],
            },
        )

    set_handler(handler)

    with pytest.raises(httpx.HTTPStatusError):
        fetch_set_cards("tst")

    assert len(requests) == 2
    assert not (isolated_cache / "tst.json").exists()


# --- fetch_collection: cache-first names and the per-card fallback ---------

def _cache(*cards):
    for card in cards:
        scryfall._cache_single(card)


@pytest.fixture
def pauses(monkeypatch):
    waited: list[float] = []
    monkeypatch.setattr(scryfall, "_pause", waited.append)
    return waited


def _collection_down(per_card, post=lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("hung", request=r))):
    """Handler: POST /cards/collection goes through `post`, GETs through `per_card`."""
    def handler(request):
        if request.method == "POST":
            return post(request)
        return per_card(request)
    return handler


def _by_fuzzy(request):
    name = request.url.params["fuzzy"]
    if name == "Missing":
        return httpx.Response(404, json={"object": "error", "type": "not_found"})
    return httpx.Response(200, json=_card(name, name.lower()))


def test_name_in_cache_makes_no_request(isolated_cache, no_network):
    _cache(_card("Sol Ring", 1), _card("Sol Ring", 2, set_code="oth"))
    got = scryfall.fetch_collection([{"name": "sol ring"}])
    assert got[0] is scryfall.cached_cards()[0]  # the first match, deterministic


def test_name_matches_a_face_in_cache(isolated_cache, no_network):
    card = _card("Delver of Secrets // Insectile Aberration", 3)
    card["card_faces"] = [{"name": "Delver of Secrets"}, {"name": "Insectile Aberration"}]
    _cache(card)
    assert scryfall.fetch_collection([{"name": "Delver of Secrets"}]) == [card]


def test_post_timeout_falls_back_to_per_card_gets(isolated_cache, mock_scryfall, pauses):
    set_handler, requests = mock_scryfall
    set_handler(_collection_down(lambda r: (
        httpx.Response(200, json=_card("By Id", 7)) if r.url.path == "/cards/id-tst-7"
        else httpx.Response(200, json=_card("By Number", 9)) if r.url.path == "/cards/xyz/9"
        else _by_fuzzy(r)
    )))
    idents = [{"name": "Alpha"}, {"id": "id-tst-7"}, {"set": "xyz", "collector_number": "9"}]
    got = scryfall.fetch_collection(idents)
    assert [c["name"] for c in got] == ["Alpha", "By Id", "By Number"]
    assert [r.method for r in requests] == ["POST", "GET", "GET", "GET"]
    assert pauses == [scryfall.FALLBACK_PAUSE] * 2
    # cached, so asking again needs no network at all
    assert len(list((isolated_cache / "cards").glob("*.json"))) == 3
    assert len(requests) == 4
    scryfall.fetch_collection([{"name": "Alpha"}])
    assert len(requests) == 4


def test_post_5xx_falls_back_too(isolated_cache, mock_scryfall, pauses):
    set_handler, _ = mock_scryfall
    set_handler(_collection_down(_by_fuzzy, post=lambda r: httpx.Response(503)))
    got = scryfall.fetch_collection([{"name": "One"}, {"name": "Two"}])
    assert [c["name"] for c in got] == ["One", "Two"]


def test_fallback_404_is_none_in_that_slot(isolated_cache, mock_scryfall, pauses):
    set_handler, _ = mock_scryfall
    set_handler(_collection_down(_by_fuzzy))
    got = scryfall.fetch_collection([{"name": "One"}, {"name": "Missing"}, {"name": "Three"}])
    assert [c and c["name"] for c in got] == ["One", None, "Three"]


def test_fallback_retries_429(isolated_cache, mock_scryfall, pauses):
    set_handler, _ = mock_scryfall
    calls = {"n": 0}

    def per_card(request):
        calls["n"] += 1
        return httpx.Response(429) if calls["n"] <= 2 else _by_fuzzy(request)

    set_handler(_collection_down(per_card))
    got = scryfall.fetch_collection([{"name": "One"}])
    assert got[0]["name"] == "One"
    assert pauses == [1, 2]


def test_fallback_gives_up_on_persistent_429(isolated_cache, mock_scryfall, pauses):
    set_handler, _ = mock_scryfall
    set_handler(_collection_down(lambda r: httpx.Response(429)))
    with pytest.raises(httpx.HTTPStatusError):
        scryfall.fetch_collection([{"name": "One"}])
    assert pauses == [1, 2, 4]


def test_unreachable_scryfall_raises_the_original_error(isolated_cache, mock_scryfall, pauses):
    set_handler, requests = mock_scryfall

    def down(request):
        raise httpx.ConnectTimeout(f"{request.method} down", request=request)

    set_handler(down)
    with pytest.raises(httpx.ConnectTimeout, match="POST down"):
        scryfall.fetch_collection([{"name": "One"}, {"name": "Two"}])
    assert len(requests) == 2  # the POST, then one failed GET, no 30s timeout per card
