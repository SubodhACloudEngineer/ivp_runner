"""Guided mode end to end: scripted answers, fake Mist API, fake portal. No network."""

import io
import zipfile
from pathlib import Path

import openpyxl
import pytest
from PIL import Image

import ivp_runner.interactive as guided
from ivp_runner.cli import main as cli_main
from ivp_runner.interactive import Ui
from ivp_runner.xlsx_patch import read_cell_texts

from .factories import ap, disconnected
from .test_cli import DESCRIPTIONS, ORG, TOKEN, FakeMist, Resp

ROOT = Path(__file__).resolve().parent.parent
SITE = "00000000-0000-4000-8000-0000000000a1"
HEADERS = {
    5: "Netsec Team IVP",
    33: "Voice service IVP  (System health and Call Test)",
    45: "Verify WAN EDGE SSR's - System validation",
    48: "Verify WAN EDGE SSR's - Routing",
    53: "Verify Switches",
    56: "Verify AP",
}


class SiteWithOrg(FakeMist):
    def get(self, url, *, headers, params, timeout):
        if url.endswith(f"/sites/{self.site_id}"):
            self.calls.append(url)
            return Resp(200, {**self.site, "org_id": ORG})
        return super().get(url, headers=headers, params=params, timeout=timeout)


class FakeBrowser:
    """Stands in for Portal: same interface, no Chromium."""

    instances: list["FakeBrowser"] = []

    def __init__(self, url, *, ask, say, headless=False):
        self.url, self.ask, self.say, self.blocked = url, ask, say, []
        self.logged_in = self.closed = False
        FakeBrowser.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def login(self):
        self.logged_in = True

    def capture(self, url, instruction):
        b = io.BytesIO()
        Image.new("RGB", (1280, 800), "navy").save(b, "PNG")
        return b.getvalue(), url or f"https://portal.example/ap/{self.url}"


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """A working folder with the MOP; catalogue paths stay absolute."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(guided, "CATALOGUE", ROOT / "catalogue" / "ap.yaml")
    monkeypatch.setattr(guided, "MAPPING", ROOT / "catalogue" / "mop_mapping.yaml")
    monkeypatch.setenv("IVP_API_HOST", "api.eu.mist.com")  # requests go to the fake session
    monkeypatch.setenv("MIST_API_TOKEN", TOKEN)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "IVP Test Plan"
    for r, text in HEADERS.items():
        ws[f"A{r}"] = text
    for r, text in DESCRIPTIONS.items():
        ws[f"B{r}"] = text
        ws[f"D{r}"] = "Not Started"
    wb.save(tmp_path / "MOP.xlsx")
    (tmp_path / "sites").mkdir()
    (tmp_path / "sites" / f"{SITE}.yaml").write_text(
        (ROOT / "tests" / "fixtures" / "mist" / "site_profile.yaml")
        .read_text()
        .replace("00000002-1111-4111-8111-000000000002", SITE),
        encoding="utf-8",
    )
    FakeBrowser.instances = []
    return tmp_path


def scripted(answers, secret=None):
    said, asked = [], []
    it = iter(answers)

    def ask(prompt):
        asked.append(prompt)
        return next(it)

    ui = Ui(ask=ask, say=said.append, secret=secret or (lambda p: "x"), color=False)
    return ui, said, asked


def text(said):
    return "\n".join(said)


def test_guided_run_of_verify_ap(workdir):
    items = [ap(1), ap(2, power_constrained=True), disconnected(9)]
    ui, said, _ = scripted(
        [
            "Branch-Office",  # not an ID: asked again
            SITE,
            "MOP.xlsx",
            "3",  # Voice: not available
            "6",  # Verify AP
            "y",  # screenshots
            "0",  # exit after the run
        ]
    )
    code = guided.main(
        ui, session=SiteWithOrg(SITE, items), sleep=lambda s: None, portal_factory=FakeBrowser
    )
    out = text(said)

    assert code == 1  # AP 2 is power-constrained
    assert "is not a Mist site ID" in out
    # insights
    assert "Site insights" in out and "3 total: 2 connected, 1 not connected (AP-TEST-9)" in out
    # menu: all six sheet sections, five marked
    first_menu = out.split("Running:")[0]
    assert "6) Verify AP" in first_menu and first_menu.count("(not available yet)") == 5
    assert "is not automated yet" in out
    # live results, per check, with the failing AP explained
    assert "AP-02  AP is not power-constrained (full PoE power)" in out
    assert "FAIL AP-TEST-2:" in out
    # screenshots: one per row, browser logged in and closed
    (browser,) = FakeBrowser.instances
    assert browser.url == "https://manage.eu.mist.com"
    assert browser.logged_in and browser.closed
    (book,) = (workdir / "out").glob("*/MOP_*.xlsx")
    with zipfile.ZipFile(book) as z:
        media = [n for n in z.namelist() if n.startswith("xl/media/ivp_runner_")]
    assert len(media) == 5
    assert read_cell_texts(book, "IVP Test Plan", ["D59"]) == {"D59": "Issue Reported"}
    assert len(list((workdir / "out").glob("*/screenshots/row*.png"))) == 5


def test_token_is_asked_hidden_when_not_exported(workdir, monkeypatch):
    monkeypatch.delenv("MIST_API_TOKEN")
    secrets = []

    def secret(prompt):
        secrets.append(prompt)
        return TOKEN

    ui, said, asked = scripted([SITE, "MOP.xlsx", "0"], secret=secret)
    code = guided.main(ui, session=SiteWithOrg(SITE, [ap(1)]), sleep=lambda s: None)
    assert code == 0 and len(secrets) == 1 and "not saved" in secrets[0]
    assert TOKEN not in text(said) + "".join(asked)


def test_bad_token_is_explained(workdir):
    ui, said, _ = scripted([SITE])
    code = guided.main(ui, session=SiteWithOrg(SITE, [], status=401), sleep=lambda s: None)
    assert code == 3 and "Mist rejected the API token" in text(said)


def test_continuing_without_screenshots_leaves_column_h_empty(workdir):
    ui, said, _ = scripted([SITE, "MOP.xlsx", "6", "n", "0"])
    guided.main(
        ui, session=SiteWithOrg(SITE, [ap(1)]), sleep=lambda s: None, portal_factory=FakeBrowser
    )
    (book,) = (workdir / "out").glob("*/MOP_*.xlsx")
    with zipfile.ZipFile(book) as z:
        assert not [n for n in z.namelist() if n.startswith("xl/media/ivp_runner_")]
    assert FakeBrowser.instances == []


def test_ivp_without_arguments_starts_guided_mode(monkeypatch):
    called = []
    monkeypatch.setattr(guided, "main", lambda **kw: called.append(kw) or 0)
    assert cli_main([]) == 0 and called


def test_prompts_live_only_in_the_guided_mode():
    for f in (ROOT / "src").rglob("*.py"):
        if f.name != "interactive.py":
            assert "input(" not in f.read_text(encoding="utf-8"), f
