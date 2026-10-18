"""A tiny client for the tracker's JSON API (/api/v1), for these scripts.

Config comes from the environment, then from the repo's gitignored .env
(parsed, never sourced or executed -- same rules as deploy/minikube.sh's
env_value: optional `export`, surrounding quotes and whitespace stripped,
last assignment wins):

    MTG_BASE_URL             default https://mtg.example.com
    CF_ACCESS_CLIENT_ID      Cloudflare Access service token (both, or neither)
    CF_ACCESS_CLIENT_SECRET
    MTG_ACT_FOR              the user id the system user acts for (default 1)
    MTG_DEV_SERVICE          a registered Client ID, for a local `MTG_AUTH=dev` server

Secrets never come from argv, are only ever sent as request headers, and are
never printed: errors carry the API's JSON error code and message, and
nothing else of the request or response.

X-Act-For is sent only together with credentials (the service token, or
MTG_DEV_SERVICE). The site runs in single mode until the Cloudflare cutover,
and a person-level request carrying X-Act-For is refused with 403.

A risky call (deleting an import) answers 409 with a preview and a confirm
token. That raises ConfirmRequired -- carrying .preview and .token -- and is
never confirmed automatically: show the preview to the user, then repeat the
call with confirm=<token>.
"""
import os
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[4]
DEFAULT_BASE = "https://mtg.example.com"
PREFIX = "/api/v1"


def env_value(name: str, text: str) -> str:
    """The value of NAME in .env text: the last `[export] NAME = value` line,
    with trailing whitespace and one pair of surrounding quotes removed.
    Empty if unset."""
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}\s*=\s*(.*)$")
    value = ""
    for line in text.splitlines():
        match = pattern.match(line.rstrip("\r"))
        if match:
            value = match.group(1).rstrip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


@dataclass(frozen=True)
class Config:
    base_url: str
    client_id: str
    client_secret: str
    act_for: str
    dev_service: str

    def __repr__(self) -> str:  # never show the secret
        return f"Config(base_url={self.base_url!r}, act_for={self.act_for!r})"


def load_config(environ=None, env_file=None) -> Config:
    """Environment variables win over .env (`env_file`, default the repo's)."""
    environ = os.environ if environ is None else environ
    path = Path(env_file) if env_file is not None else REPO / ".env"
    try:
        text = path.read_text()
    except OSError:
        text = ""

    def get(name: str, default: str = "") -> str:
        return (environ.get(name) or "").strip() or env_value(name, text).strip() or default

    return Config(
        base_url=get("MTG_BASE_URL", DEFAULT_BASE).rstrip("/"),
        client_id=get("CF_ACCESS_CLIENT_ID"), client_secret=get("CF_ACCESS_CLIENT_SECRET"),
        act_for=get("MTG_ACT_FOR", "1"), dev_service=get("MTG_DEV_SERVICE"),
    )


def headers_for(cfg: Config, *, act_for: bool = True) -> dict:
    """Service-token headers only when both halves are set and the site is
    HTTPS (the secret never goes over plain HTTP, e.g. to a local rehearsal
    server); X-Dev-Service only when set; X-Act-For only when one of those
    identifies a system user and `act_for` (see _server_wants_act_for)."""
    headers = {"Accept": "application/json"}
    service = False
    if cfg.client_id and cfg.client_secret and cfg.base_url.startswith("https://"):
        headers["CF-Access-Client-Id"] = cfg.client_id
        headers["CF-Access-Client-Secret"] = cfg.client_secret
        service = True
    if cfg.dev_service:
        headers["X-Dev-Service"] = cfg.dev_service
        service = True
    if service and act_for:
        headers["X-Act-For"] = cfg.act_for
    return headers


_act_for: bool | None = None  # does this server treat us as a system user? (probed once)


def _server_wants_act_for(client: httpx.Client, cfg: Config) -> bool:
    """Whether to send X-Act-For. With credentials configured, a site still
    in single mode (or a rehearsal server) treats every caller as a person
    and refuses X-Act-For with 403, so ask /me once without it: a person
    answer means don't send it; "act_for_required" means the server saw a
    system user and needs it."""
    global _act_for
    if _act_for is None:
        probe = headers_for(cfg, act_for=False)
        if "X-Act-For" not in headers_for(cfg):
            _act_for = False
        else:
            try:
                resp = client.get(PREFIX + "/me", headers=probe)
                is_json = resp.headers.get("content-type", "").startswith("application/json")
                body = resp.json() if is_json else {}
            except (httpx.HTTPError, ValueError):
                body = {}
            code = (body.get("error") or {}).get("code") if isinstance(body, dict) else None
            person = isinstance(body, dict) and (body.get("actor") or {}).get("kind") == "person"
            _act_for = not person if code is None else code == "act_for_required"
    return _act_for


class ApiError(Exception):
    """The API refused, or something other than the API answered."""

    def __init__(self, status: int, code: str, message: str, body: dict | None = None):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message, self.body = status, code, message, body or {}


class ConfirmRequired(ApiError):
    """A risky call needs the user's OK: .preview is what it would do and
    .token confirms exactly that (repeat the call with confirm=token)."""

    def __init__(self, status, code, message, body):
        super().__init__(status, code, message, body)
        self.preview, self.token = body.get("preview"), body.get("confirm")


_client: httpx.Client | None = None
_config: Config | None = None


def set_client(client: httpx.Client | None, config: Config | None = None) -> None:
    """Use this client (a TestClient in tests) and config instead of the
    environment's."""
    global _client, _config, _act_for
    _client, _config, _act_for = client, config, None


def _setup() -> tuple[httpx.Client, Config]:
    global _client, _config
    if _config is None:
        _config = load_config()
    if _client is None:
        _client = httpx.Client(base_url=_config.base_url, follow_redirects=False, timeout=90)
    return _client, _config


def request(method: str, path: str, *, params=None, json=None, files=None, confirm=None) -> dict:
    """One API call; the parsed JSON body, or ApiError / ConfirmRequired."""
    client, cfg = _setup()
    params = dict(params or {})
    if confirm:
        params["confirm"] = confirm
    try:
        headers = headers_for(cfg, act_for=_server_wants_act_for(client, cfg))
        resp = client.request(method, PREFIX + path, params=params or None, json=json, files=files,
                              headers=headers)
    except httpx.HTTPError as exc:
        raise ApiError(0, "network", f"couldn't reach {cfg.base_url} ({type(exc).__name__})") from None
    try:
        body = resp.json()
    except ValueError:
        body = None
    if resp.is_success and isinstance(body, dict):
        return body
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):  # e.g. a Cloudflare login page or redirect
        raise ApiError(resp.status_code, "not_api", "the response wasn't the API's JSON")
    cls = ConfirmRequired if resp.status_code == 409 and "confirm" in body else ApiError
    raise cls(resp.status_code, str(error.get("code")), str(error.get("message")), body)


def get(path: str, **params) -> dict:
    return request("GET", path, params=params)


def post(path: str, json=None, *, params=None, files=None) -> dict:
    return request("POST", path, params=params, json=json, files=files)


def put(path: str, json=None, *, params=None) -> dict:
    return request("PUT", path, params=params, json=json)


def delete(path: str, *, params=None, confirm=None) -> dict:
    return request("DELETE", path, params=params, confirm=confirm)
