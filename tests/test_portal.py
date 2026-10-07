"""Portal screenshots: template learning, AP choice, and the browser itself.

The browser tests drive headless Chromium against a page served on
127.0.0.1 by the test; nothing leaves the machine. They are skipped when
Playwright is not installed.
"""

import io
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image

from ivp_runner.assert_engine import evaluate
from ivp_runner.portal import (
    Portal,
    fill_template,
    learn_template,
    load_pages,
    pick_device,
    portal_url_for,
    save_pages,
    shrink,
    take_screenshots,
)

from .factories import ap, catalogue, context, disconnected, payloads, profile

SITE = "00000000-0000-4000-8000-0000000000a1"
ORG = "00000000-0000-4000-8000-0000000000f1"


def test_portal_url_follows_the_api_cloud():
    assert portal_url_for("api.eu.mist.com") == "https://manage.eu.mist.com"
    assert portal_url_for("api.mist.com") == "https://manage.mist.com"
    assert portal_url_for("127.0.0.1:8443") is None


def test_learned_template_replaces_ids_and_round_trips():
    ids = {"org_id": ORG, "site_id": SITE, "device_id": "dev-123", "mac": "aabbccddeeff"}
    url = f"https://manage.example/admin/?org_id={ORG}#!ap/detail/dev-123/{SITE.upper()}"
    template = learn_template(url, ids)
    assert (
        template == "https://manage.example/admin/?org_id={org_id}#!ap/detail/{device_id}/{site_id}"
    )
    other = {**ids, "device_id": "dev-999"}
    assert fill_template(template, other).endswith("#!ap/detail/dev-999/" + SITE)


def test_page_with_no_known_id_is_not_learned():
    assert learn_template("https://manage.example/admin/#!dashboard", {"site_id": SITE}) is None


def test_template_needing_an_unknown_id_is_not_used():
    assert fill_template("https://x/{device_id}/{vlan}", {"device_id": "d"}) is None


def test_pages_file_round_trip(tmp_path):
    f = tmp_path / "portal_pages.yaml"
    save_pages({61: "https://x/{device_id}", 57: "https://x/{site_id}"}, f)
    assert load_pages(f) == {57: "https://x/{site_id}", 61: "https://x/{device_id}"}
    assert load_pages(tmp_path / "missing.yaml") == {}


def results_for(items):
    return [e.result for e in evaluate(catalogue(), profile(), *payloads(items), context())]


def test_pick_device_prefers_first_failing_then_first_passing():
    rs = results_for([ap(1), ap(2, power_constrained=True), ap(3, power_constrained=True)])
    assert pick_device(59, ["AP-02"], rs).device_name == "AP-TEST-2"
    assert pick_device(59, ["AP-02"], rs).reason == "first failing AP"
    assert pick_device(58, ["AP-01"], rs).device_name == "AP-TEST-1"
    assert pick_device(58, ["AP-01"], rs).reason == "first passing AP"


def test_pick_device_none_when_nothing_was_judged():
    assert pick_device(58, ["AP-01"], results_for([disconnected(9)])) is None


def test_shrink_keeps_aspect_and_caps_width():
    b = io.BytesIO()
    Image.new("RGB", (2000, 1000), "white").save(b, "PNG")
    img = Image.open(io.BytesIO(shrink(b.getvalue(), 1000)))
    assert img.size == (1000, 500)


# ---------------------------------------------------------------- real browser

playwright = pytest.importorskip("playwright.sync_api")
if not os.environ.get("IVP_CHROMIUM") and Path("/opt/pw-browsers/chromium").exists():
    os.environ["IVP_CHROMIUM"] = "/opt/pw-browsers/chromium"


class FakePortal(BaseHTTPRequestHandler):
    posts: list[str] = []

    def do_GET(self):  # noqa: N802
        body = (
            "<html><body style='background:#fff'><h1>AP page</h1>"
            f"<p id=path>{self.path}</p>"
            "<script>fetch('/api/v1/sites/x/devices/y', {method: 'PUT', body: '{}'})</script>"
            "</body></html>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):  # noqa: N802
        FakePortal.posts.append(self.path)
        self.send_response(200)
        self.end_headers()

    do_POST = do_PUT  # noqa: N815

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    FakePortal.posts = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakePortal)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_browser_is_read_only_after_login_and_learns_pages(server, tmp_path):
    said, asked = [], []
    answers = iter(["", ""])  # Enter after login; Enter after opening the page by hand

    def ask(prompt):
        asked.append(prompt)
        return next(answers)

    pages = tmp_path / "portal_pages.yaml"
    rs = results_for([ap(1), ap(2, power_constrained=True)])
    dev2 = next(r.device for r in rs if r.device and r.device.name == "AP-TEST-2")

    with Portal(server, ask=ask, say=said.append, headless=True) as portal:
        portal.login()  # before the lock: the engineer's own login may POST
        FakePortal.posts.clear()
        # Engineer "navigates" to the AP page for the chosen AP; we simulate it.
        portal._page.goto(f"{server}/admin/#!ap/{dev2.id}/{SITE}")
        taken = take_screenshots(
            portal,
            [(59, ["AP-02"], '"Check the Power mode:"')],
            rs,
            {"org_id": ORG, "site_id": SITE},
            tmp_path / "shots",
            pages,
        )
        assert load_pages(pages) == {59: f"{server}/admin/#!ap/{{device_id}}/{{site_id}}"}

        # Second run: the page opens by itself, no question asked.
        n_asked = len(asked)
        again = take_screenshots(
            portal,
            [(59, ["AP-02"], "x")],
            rs,
            {"org_id": ORG, "site_id": SITE},
            tmp_path / "shots",
            pages,
        )
        assert len(asked) == n_asked
        assert again[0][2] == f"{server}/admin/#!ap/{dev2.id}/{SITE}"

    (shot, png, url) = taken[0]
    assert shot.device_name == "AP-TEST-2" and Image.open(io.BytesIO(png)).width == 1280
    assert (tmp_path / "shots" / "row59_AP-TEST-2.png").is_file()
    assert FakePortal.posts == []  # after login, no page's PUT reached the server
    assert portal.blocked and portal.blocked[0].startswith("PUT ")
    assert any("Log in there yourself" in s for s in said)
