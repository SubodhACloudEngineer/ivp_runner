import hashlib
import inspect
import json

import pytest
import requests

from ivp_runner.collectors import CollectError, CollectorConfigError, Payload, mist_api
from ivp_runner.collectors.mist_api import MistApiCollector

from .factories import catalogue
from .fixtures_mist import site_doc, stats_items

TOKEN = "test-token-not-real-0123456789"
SITE = "site-1"


class FakeResponse:
    def __init__(self, status: int, body, headers: dict | None = None):
        self.status_code = status
        self.content = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = headers or {}


class FakeSession:
    """Serves queued responses per path, records every call. Only `get` exists."""

    def __init__(self, routes: dict[str, list]):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls: list[dict] = []

    def get(self, url, *, headers, params, timeout):
        path = url.split("://", 1)[1].split("/", 1)[1]
        self.calls.append({"path": "/" + path, "params": dict(params), "headers": headers})
        queue = self.routes["/" + path]
        nxt = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


STATS = f"/api/v1/sites/{SITE}/stats/devices"
SITE_PATH = f"/api/v1/sites/{SITE}"


def collector(session, **kw):
    sleeps: list[float] = []
    c = MistApiCollector("api.example", token=TOKEN, session=session, sleep=sleeps.append, **kw)
    return c, sleeps


def ok(body, headers=None):
    return FakeResponse(200, body, headers)


# ---------------------------------------------------------------- planning & happy path


def test_fetch_plan_is_once_per_source_per_phase(tmp_path):
    session = FakeSession({STATS: [ok(stats_items())], SITE_PATH: [ok(site_doc())]})
    c, _ = collector(session)
    cat = catalogue()

    start = c.collect(cat, "large", SITE, tmp_path, "start")
    assert [call["path"] for call in session.calls] == [SITE_PATH, STATS]  # 6 checks, 91 APs
    end = c.collect(cat, "large", SITE, tmp_path, "end")
    assert [call["path"] for call in session.calls[2:]] == [STATS]  # only AP-05 samples twice

    assert set(start) == {"site", "site_device_stats"} and set(end) == {"site_device_stats"}
    stats = start["site_device_stats"]
    assert isinstance(stats, Payload) and len(stats.items) == 91
    assert start["site"].data["timezone"] == "Europe/Madrid"


def test_get_only_with_token_header_and_params(tmp_path):
    session = FakeSession({STATS: [ok([])]})
    c, _ = collector(session)
    c.fetch("site_device_stats", SITE, tmp_path, "start")
    (call,) = session.calls
    assert call["headers"]["Authorization"] == f"Token {TOKEN}"
    assert call["params"] == {"type": "ap", "limit": "1000", "page": "1"}


def test_raw_pages_combined_file_and_manifest_persisted(tmp_path):
    items = stats_items()
    session = FakeSession({STATS: [ok(items[:50]), ok(items[50:])]})
    c, _ = collector(session, page_limit=50)
    p = c.fetch("site_device_stats", SITE, tmp_path, "start")
    c._write_manifest(tmp_path)

    raw = tmp_path / "raw"
    assert json.loads((raw / "site_device_stats.start.p1.json").read_bytes()) == items[:50]
    assert json.loads((raw / "site_device_stats.start.p2.json").read_bytes()) == items[50:]
    combined = (raw / "site_device_stats.start.json").read_bytes()
    assert json.loads(combined) == items and p.items == items
    assert p.raw_sha256 == hashlib.sha256(combined).hexdigest()
    assert p.raw_path == str(raw / "site_device_stats.start.json")

    manifest = json.loads((raw / "manifest.json").read_text())
    assert [(m["params"]["page"], m["status"]) for m in manifest] == [("1", 200), ("2", 200)]
    assert (
        manifest[0]["sha256"]
        == hashlib.sha256((raw / "site_device_stats.start.p1.json").read_bytes()).hexdigest()
    )


# ---------------------------------------------------------------- pagination


def test_pagination_stops_on_short_page(tmp_path):
    items = stats_items()
    session = FakeSession({STATS: [ok(items[:40]), ok(items[40:80]), ok(items[80:])]})
    c, _ = collector(session, page_limit=40)
    p = c.fetch("site_device_stats", SITE, tmp_path, "start")
    assert len(p.items) == 91
    assert [call["params"]["page"] for call in session.calls] == ["1", "2", "3"]


def test_server_ignoring_page_param_fails_instead_of_duplicating(tmp_path):
    items = stats_items()[:40]
    session = FakeSession({STATS: [ok(items)]})  # same page every time
    c, _ = collector(session, page_limit=40)
    with pytest.raises(CollectError, match="page 2 repeats items"):
        c.fetch("site_device_stats", SITE, tmp_path, "start")


# ---------------------------------------------------------------- HTTP handling


def test_429_honours_retry_after_then_succeeds(tmp_path):
    session = FakeSession(
        {STATS: [FakeResponse(429, b"", {"Retry-After": "3"}), FakeResponse(429, b""), ok([])]}
    )
    c, sleeps = collector(session)
    c.fetch("site_device_stats", SITE, tmp_path, "start")
    assert sleeps == [3.0, 2.0]  # Retry-After, then exponential backoff for attempt 2
    assert len(session.calls) == 3


def test_429_gives_up_after_max_retries(tmp_path):
    session = FakeSession({STATS: [FakeResponse(429, b"")]})
    c, sleeps = collector(session, max_retries=3)
    with pytest.raises(CollectError, match=r"rate limited \(429\) after 4 attempts"):
        c.fetch("site_device_stats", SITE, tmp_path, "start")
    assert sleeps == [1.0, 2.0, 4.0]


@pytest.mark.parametrize(
    "first",
    [FakeResponse(503, b"down"), requests.ConnectionError("reset"), requests.Timeout("slow")],
)
def test_transient_failures_are_retried(tmp_path, first):
    session = FakeSession({STATS: [first, ok([])]})
    c, sleeps = collector(session)
    c.fetch("site_device_stats", SITE, tmp_path, "start")
    assert sleeps == [1.0] and len(session.calls) == 2


@pytest.mark.parametrize(
    "status, message",
    [
        (401, "401 authentication failed"),
        (403, "403 authentication failed or the token lacks access"),
        (404, "404 not found; check the site id and --api-host"),
        (400, "HTTP 400"),
    ],
)
def test_client_errors_are_not_retried(tmp_path, status, message):
    session = FakeSession({STATS: [FakeResponse(status, b"{}")]})
    c, sleeps = collector(session)
    with pytest.raises(CollectError, match=message):
        c.fetch("site_device_stats", SITE, tmp_path, "start")
    assert sleeps == [] and len(session.calls) == 1


@pytest.mark.parametrize("body, message", [(b"<html>", "not JSON"), (b"{}", "expected a list")])
def test_bad_bodies_are_collect_errors(tmp_path, body, message):
    c, _ = collector(FakeSession({STATS: [ok(body)]}))
    with pytest.raises(CollectError, match=message):
        c.fetch("site_device_stats", SITE, tmp_path, "start")


def test_failed_source_is_returned_not_raised(tmp_path):
    session = FakeSession({STATS: [FakeResponse(403, b"")], SITE_PATH: [ok(site_doc())]})
    c, _ = collector(session)
    start = c.collect(catalogue(), "large", SITE, tmp_path, "start")
    assert isinstance(start["site"], Payload)
    assert isinstance(start["site_device_stats"], CollectError)


# ---------------------------------------------------------------- token safety


def test_missing_token_fails_with_clear_message(monkeypatch):
    monkeypatch.delenv("MIST_API_TOKEN", raising=False)
    with pytest.raises(CollectorConfigError, match="MIST_API_TOKEN is not set"):
        MistApiCollector("api.example", session=FakeSession({}))


def test_token_read_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("MIST_API_TOKEN", TOKEN)
    session = FakeSession({STATS: [ok([])]})
    MistApiCollector("api.example", session=session).fetch("site_device_stats", SITE, tmp_path, "s")
    assert session.calls[0]["headers"]["Authorization"] == f"Token {TOKEN}"


def test_token_never_persisted_or_exposed(tmp_path, capsys):
    echo = [{"id": "x", "echo": TOKEN}]
    session = FakeSession(
        {STATS: [FakeResponse(429, b"", {"X-Debug": TOKEN}), ok(echo)], SITE_PATH: [ok(site_doc())]}
    )
    c, _ = collector(session)
    c.collect(catalogue(), "large", SITE, tmp_path, "start")
    for f in (tmp_path / "raw").iterdir():
        assert TOKEN not in f.read_text(), f.name
    assert TOKEN not in repr(c)
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err


def test_errors_never_include_token(tmp_path):
    c, _ = collector(FakeSession({STATS: [FakeResponse(401, TOKEN.encode())]}))
    with pytest.raises(CollectError) as e:
        c.fetch("site_device_stats", SITE, tmp_path, "start")
    assert TOKEN not in str(e.value)


def test_collector_source_has_no_write_verbs():
    src = inspect.getsource(mist_api)
    for verb in (".post(", ".put(", ".delete(", ".patch(", ".request("):
        assert verb not in src


# ---------------------------------------------------------------- token normalisation


@pytest.mark.parametrize(
    "raw",
    [f"  {TOKEN}\n", f"Token {TOKEN}", f"token  {TOKEN} "],
)
def test_token_whitespace_and_scheme_prefix_are_tolerated(tmp_path, raw):
    session = FakeSession({STATS: [ok([])]})
    c = MistApiCollector("api.example", token=raw, session=session, sleep=lambda s: None)
    c.fetch("site_device_stats", SITE, tmp_path, "start")
    assert session.calls[0]["headers"]["Authorization"] == f"Token {TOKEN}"


@pytest.mark.parametrize(
    "raw, message",
    [
        (TOKEN[:6] + " " + TOKEN[6:], "contains a space at character 7 of"),
        (TOKEN[:6] + "\n" + TOKEN[6:], "contains a line break at character 7"),
        ("   ", "MIST_API_TOKEN is not set"),
    ],
)
def test_malformed_token_fails_before_any_request_without_echoing(raw, message):
    session = FakeSession({})
    with pytest.raises(CollectorConfigError, match=message) as e:
        MistApiCollector("api.example", token=raw, session=session)
    assert TOKEN[6:] not in str(e.value) and TOKEN[:6] not in str(e.value)
    assert session.calls == []
