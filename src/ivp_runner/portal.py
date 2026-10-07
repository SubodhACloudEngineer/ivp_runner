"""Mist portal screenshots for the MOP's evidence column.

How it stays inside the project rules:

* **No login by the tool.** A Chromium window opens on the portal and the
  engineer logs in by hand (SSO and MFA work). The tool never sees or sends a
  password. Nothing is saved between runs: each run uses a fresh, in-memory
  browser profile.
* **Read-only after login.** Once the engineer presses Enter, every request
  the browser makes with a method other than GET, HEAD or OPTIONS is blocked,
  so no portal page can change anything. Blocked requests are counted and
  reported.
* **No invented URLs.** The first time a MOP row is captured, the engineer
  opens the right page and presses Enter. The tool then saves that page's
  address with the org, site and AP IDs replaced by placeholders
  (``sites/portal_pages.yaml``, git-ignored), and later runs go there on
  their own.
"""

from __future__ import annotations

import io
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml
from PIL import Image

from ivp_runner.results import TestResult, Verdict

READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
VIEWPORT = {"width": 1600, "height": 1000}
MAX_WIDTH = 1280  # screenshots are scaled down to this before going into the workbook
SETTLE_MS = 2500  # the portal keeps loading widgets after the network goes quiet
PAGES_FILE = Path("sites") / "portal_pages.yaml"


class PortalError(Exception):
    def __init__(self, problem: str, fix: str = ""):
        super().__init__(problem)
        self.problem, self.fix = problem, fix


def portal_url_for(api_host: str) -> str | None:
    """``https://manage.eu.mist.com`` for ``api.eu.mist.com``; None if not a Mist API host."""
    host = api_host.split(":")[0]
    if host.startswith("api.") and host.endswith(".mist.com"):
        return "https://manage." + host[len("api.") :]
    return None


# ---------------------------------------------------------------- page templates


def learn_template(url: str, ids: dict[str, str]) -> str | None:
    """Replace known IDs in ``url`` with ``{placeholders}``; None if none were found.

    ``ids`` maps placeholder -> value, e.g. {"site_id": "...", "mac": "aabbcc..."}.
    Longer values are replaced first so an ID is never split.
    """
    out, found = url, False
    for name, value in sorted(ids.items(), key=lambda kv: -len(kv[1] or "")):
        if not value:
            continue
        pattern = re.compile(re.escape(value), re.IGNORECASE)
        if pattern.search(out):
            out, found = pattern.sub("{" + name + "}", out), True
    return out if found else None


def fill_template(template: str, ids: dict[str, str]) -> str | None:
    """The URL for these IDs, or None if the template needs one we don't have."""
    try:
        return template.format(**ids)
    except (KeyError, IndexError):
        return None


def load_pages(path: Path = PAGES_FILE) -> dict[int, str]:
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {int(k): str(v) for k, v in (raw.get("rows") or {}).items()}


def save_pages(pages: dict[int, str], path: Path = PAGES_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "# note": "Learned from pages opened by hand. Placeholders are filled per run.",
        "rows": {int(k): v for k, v in sorted(pages.items())},
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


# ---------------------------------------------------------------- which AP


@dataclass(frozen=True)
class Shot:
    row: int
    device_name: str
    device_id: str
    mac: str
    reason: str  # "first failing AP" / "first passing AP" / ...


def pick_device(row: int, test_ids: Sequence[str], results: Sequence[TestResult]) -> Shot | None:
    """The AP to show for a MOP row: the first that failed, else the first that fully passed."""
    by_device: dict[str, list[TestResult]] = {}
    for r in results:
        if r.test_id in test_ids and r.device is not None:
            by_device.setdefault(r.device.id, []).append(r)
    ordered = sorted(by_device.values(), key=lambda rs: rs[0].device.name or rs[0].device.id)
    failing = [rs for rs in ordered if any(r.verdict is Verdict.FAIL for r in rs)]
    passing = [rs for rs in ordered if all(r.verdict is Verdict.PASS for r in rs)]
    for group, why in ((failing, "first failing AP"), (passing, "first passing AP")):
        if group:
            d = group[0][0].device
            return Shot(row, d.name or d.id, d.id, d.mac or "", why)
    return None


def shrink(png: bytes, max_width: int = MAX_WIDTH) -> bytes:
    img = Image.open(io.BytesIO(png))
    if img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)), Image.LANCZOS)
    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


# ---------------------------------------------------------------- the browser


class Portal:
    """A Chromium window on the Mist portal, logged in by hand, locked read-only."""

    def __init__(
        self,
        portal_url: str,
        *,
        ask: Callable[[str], str],
        say: Callable[[str], None],
        headless: bool = False,
    ):
        self.portal_url, self.ask, self.say, self.headless = portal_url, ask, say, headless
        self.blocked: list[str] = []
        self._pw = self._browser = self._page = None

    def __enter__(self) -> Portal:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise PortalError(
                "the browser component (Playwright) is not installed",
                "Run: pip install playwright && playwright install chromium",
            ) from None
        if (
            not self.headless
            and os.name != "nt"
            and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        ):
            raise PortalError(
                "no screen to open the browser window on",
                "On Windows 11, WSL provides one (WSLg); run `wsl --update` in PowerShell and "
                "reopen the terminal. Or choose to continue without screenshots.",
            )
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(
                headless=self.headless, executable_path=os.environ.get("IVP_CHROMIUM") or None
            )
        except Exception as e:  # missing browser binary, sandbox problems, ...
            self._pw.stop()
            raise PortalError(
                f"could not start Chromium: {str(e).splitlines()[0]}",
                "Run: playwright install chromium (and on Linux/WSL: sudo playwright install-deps)",
            ) from None
        context = self._browser.new_context(viewport=VIEWPORT)  # in memory: nothing saved
        self._context = context
        self._page = context.new_page()
        return self

    def __exit__(self, *exc) -> None:
        for close in (getattr(self._browser, "close", None), getattr(self._pw, "stop", None)):
            try:
                if close:
                    close()
            except Exception:
                pass

    def login(self) -> None:
        self._page.goto(self.portal_url)
        self.say(f"A browser window opened on {self.portal_url}.")
        self.say("Log in there yourself (SSO/MFA are fine). The tool never sees your password.")
        self.ask("Press Enter here once you are logged in and can see your organization... ")
        self._context.route("**/*", self._read_only)
        self.say("Browser locked to read-only: any request that could change something is blocked.")

    def _read_only(self, route) -> None:
        req = route.request
        if req.method.upper() in READ_ONLY_METHODS:
            route.continue_()
        else:
            self.blocked.append(f"{req.method} {req.url.split('?')[0]}")
            route.abort()

    def capture(self, url: str | None, instruction: str) -> tuple[bytes, str]:
        """Screenshot ``url`` (opened automatically) or, if None, the page the engineer opens."""
        if url:
            self._page.goto(url, wait_until="networkidle", timeout=60_000)
            self._page.wait_for_timeout(SETTLE_MS)
        else:
            self.say(instruction)
            self.ask("Press Enter here when the browser shows it... ")
        return shrink(self._page.screenshot(type="png")), self._page.url


def take_screenshots(
    portal: Portal,
    rows: Sequence[tuple[int, Sequence[str], str]],
    results: Sequence[TestResult],
    ids: dict[str, str],
    out_dir: Path,
    pages_file: Path = PAGES_FILE,
) -> list[tuple[Shot, bytes, str]]:
    """One screenshot per MOP row. ``rows`` is (row, test_ids, what the row asks to show).

    Returns (shot, png, url) per row captured; also writes each PNG to ``out_dir``.
    """
    pages = load_pages(pages_file)
    out_dir.mkdir(parents=True, exist_ok=True)
    taken = []
    for row, test_ids, what in rows:
        shot = pick_device(row, test_ids, results)
        if shot is None:
            portal.say(f"  row {row}: no AP was judged for {', '.join(test_ids)}; no screenshot")
            continue
        these = {**ids, "device_id": shot.device_id, "mac": shot.mac}
        url = fill_template(pages[row], these) if row in pages else None
        portal.say(f"  row {row} ({', '.join(test_ids)}): {shot.device_name}, {shot.reason}")
        instruction = (
            f"    In the browser, open AP {shot.device_name} and show: {what}"
            if url is None
            else ""
        )
        png, final_url = portal.capture(url, instruction)
        if url is None:
            learned = learn_template(final_url, these)
            if learned:
                pages[row] = learned
                save_pages(pages, pages_file)
                portal.say(f"    remembered this page for row {row}; next time it opens by itself")
        name = re.sub(r"[^A-Za-z0-9_-]+", "_", shot.device_name)
        (out_dir / f"row{row}_{name}.png").write_bytes(png)
        taken.append((shot, png, final_url))
    return taken


def caption(shot: Shot, url: str, when: datetime | None = None) -> str:
    when = when or datetime.now(UTC)
    path = re.sub(r"^https?://[^/]+", "", url)
    return (
        f"ivp-runner portal screenshot: AP {shot.device_name} ({shot.reason}), "
        f"{when:%Y-%m-%dT%H:%M:%SZ}, page {path}"
    )
