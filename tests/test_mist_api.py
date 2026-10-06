import hashlib
import json

import pytest
import requests

from ivp_runner.methods import CollectError
from ivp_runner.methods.mist_api import SOURCES, MistApi

TOKEN = "test-token-not-real-0123456789"


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self.content = body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class FakeTransport:
    """Records calls. Exposes only `get`: any other verb would raise AttributeError."""

    def __init__(self, body: bytes, status: int = 200):
        self.body, self.status, self.calls = body, status, []

    def get(self, url, *, headers, params, timeout):
        self.calls.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        return FakeResponse(self.body, self.status)


def test_get_only_with_token_header_and_raw_payload_saved(tmp_path):
    body = json.dumps([{"id": "d1", "type": "ap"}]).encode()
    t = FakeTransport(body)
    p = MistApi("api.example", transport=t, token=TOKEN).collect(
        "site_device_stats", "site-1", tmp_path, "start"
    )

    (call,) = t.calls
    assert call["url"] == "https://api.example/api/v1/sites/site-1/stats/devices"
    assert call["headers"]["Authorization"] == f"Token {TOKEN}"
    assert call["params"] == {"type": "ap", "limit": "1000"}
    assert p.items == [{"id": "d1", "type": "ap"}]
    saved = (tmp_path / "raw" / "site_device_stats.start.json").read_bytes()
    assert saved == body  # verbatim
    assert p.raw_sha256 == hashlib.sha256(saved).hexdigest()
    assert p.identity == SOURCES["site_device_stats"].identity


def test_token_is_scrubbed_from_saved_payload_and_never_printed(tmp_path, capsys):
    body = json.dumps([{"id": "d1", "echo": TOKEN}]).encode()
    api = MistApi("api.example", transport=FakeTransport(body), token=TOKEN)
    p = api.collect("site_device_stats", "site-1", tmp_path)
    saved = (tmp_path / "raw" / "site_device_stats.once.json").read_text()
    assert TOKEN not in saved and "<REDACTED_TOKEN>" in saved
    assert TOKEN not in repr(api) and TOKEN not in repr(p)
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err


def test_token_comes_from_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("MIST_API_TOKEN", raising=False)
    with pytest.raises(CollectError, match="MIST_API_TOKEN is not set"):
        MistApi("api.example", transport=FakeTransport(b"[]"))
    monkeypatch.setenv("MIST_API_TOKEN", TOKEN)
    t = FakeTransport(b"[]")
    MistApi("api.example", transport=t).collect("site_device_stats", "s", tmp_path)
    assert t.calls[0]["headers"]["Authorization"] == f"Token {TOKEN}"


@pytest.mark.parametrize(
    "body, status, message",
    [
        (b"[]", 403, "failed: HTTPError"),
        (b"<html>", 200, "not JSON"),
        (b"{}", 200, "expected a list"),
    ],
)
def test_collection_failures_raise_collect_error(tmp_path, body, status, message):
    api = MistApi("api.example", transport=FakeTransport(body, status), token=TOKEN)
    with pytest.raises(CollectError, match=message) as e:
        api.collect("site_device_stats", "s", tmp_path)
    assert TOKEN not in str(e.value)


def test_unknown_source_rejected(tmp_path):
    api = MistApi("api.example", transport=FakeTransport(b"[]"), token=TOKEN)
    with pytest.raises(CollectError, match="unknown mist_api source"):
        api.collect("orgs_delete_everything", "s", tmp_path)


def test_backend_source_code_contains_no_write_verbs():
    import inspect

    from ivp_runner.methods import mist_api

    src = inspect.getsource(mist_api)
    for verb in (".post(", ".put(", ".delete(", ".patch(", ".request("):
        assert verb not in src
