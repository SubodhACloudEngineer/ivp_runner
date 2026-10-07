"""Guided mode: ``python ivp.py`` (or ``ivp`` with no arguments).

Asks for what it needs (token if not exported, Mist cloud, site ID, MOP
workbook), shows site insights, then a numbered menu of the IVP Test Plan
sections. Running a section prints each AP's verdict as it is judged, takes
Mist portal screenshots for the MOP's evidence column, and writes the
populated workbook copy.

All prompting lives in this module; every prompt function is injectable so
the flow is tested without a keyboard.
"""

from __future__ import annotations

import getpass as _getpass
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ivp_runner.catalogue import load_catalogue
from ivp_runner.collectors import CollectError, CollectorConfigError, normalize_token
from ivp_runner.collectors.mist_api import MistApiCollector
from ivp_runner.insights import site_insights
from ivp_runner.mop_writer import MenuEntry, load_mapping, read_sections
from ivp_runner.portal import Portal, PortalError, caption, portal_url_for, take_screenshots
from ivp_runner.results import TestResult, Verdict
from ivp_runner.runner import EXIT_TOOL, RunConfig, RunOutcome, _is_uuid, run
from ivp_runner.xlsx_patch import PatchError, Picture

CLOUDS = (("Global", "api.mist.com"), ("EU", "api.eu.mist.com"))
DEFAULT_MOP = Path("reference") / "MOP_sample.xlsx"
CATALOGUE = Path("catalogue") / "ap.yaml"
MAPPING = Path("catalogue") / "mop_mapping.yaml"


class Ui:
    """Screen I/O. Colour only on a terminal, and never when NO_COLOR is set."""

    def __init__(self, ask=input, say=print, secret=_getpass.getpass, color: bool | None = None):
        self.ask_raw, self.say, self.secret = ask, say, secret
        if color is None:
            color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        self.color = color

    def ask(self, prompt: str, default: str = "") -> str:
        hint = f" [{default}]" if default else ""
        try:
            answer = self.ask_raw(f"{prompt}{hint}: ").strip()
        except EOFError:
            raise SystemExit(EXIT_TOOL) from None
        return answer or default

    def yes(self, prompt: str, default: bool) -> bool:
        answer = self.ask(f"{prompt} ({'Y/n' if default else 'y/N'})").lower()
        return default if not answer else answer.startswith("y")

    def paint(self, text: str, verdict: Verdict | str) -> str:
        if not self.color:
            return text
        code = {"PASS": "32", "FAIL": "31", "ERROR": "33", "SKIP": "90"}.get(
            str(getattr(verdict, "value", verdict)), "0"
        )
        return f"\033[{code}m{text}\033[0m"

    def heading(self, text: str) -> None:
        self.say("")
        self.say(f"\033[1m{text}\033[0m" if self.color else text)
        self.say("-" * len(text))

    def problem(self, problem: str, fix: str = "") -> None:
        self.say(f"  Problem:    {problem}")
        if fix:
            self.say(f"  What to do: {fix}")


# ---------------------------------------------------------------- steps


def ask_token(ui: Ui) -> None:
    """Use MIST_API_TOKEN if exported, else ask for it hidden; kept in this process only."""
    while True:
        try:
            normalize_token(os.environ.get("MIST_API_TOKEN"))
            return
        except CollectorConfigError as e:
            if os.environ.get("MIST_API_TOKEN"):
                ui.problem(str(e))
            raw = ui.secret("Mist API token (typing is hidden; it is not saved anywhere): ")
            os.environ["MIST_API_TOKEN"] = raw


def ask_cloud(ui: Ui) -> str:
    env = os.environ.get("IVP_API_HOST")
    if env:
        return env
    ui.say("Mist cloud:")
    for i, (name, host) in enumerate(CLOUDS, 1):
        ui.say(f"  {i}) {name:7} {host}")
    ui.say(f"  {len(CLOUDS) + 1}) other")
    while True:
        choice = ui.ask("Choose", "2")
        if choice.isdigit() and 1 <= int(choice) <= len(CLOUDS):
            return CLOUDS[int(choice) - 1][1]
        if choice == str(len(CLOUDS) + 1):
            return ui.ask("API host, e.g. api.gc1.mist.com")
        ui.say("  Please type one of the numbers.")


def ask_site(ui: Ui, collector: MistApiCollector, scratch: Path) -> tuple[str, dict, list[dict]]:
    """Site ID -> (site_id, site document, AP stats items). Re-asks on a wrong ID."""
    while True:
        site_id = ui.ask("Site ID (from the Mist portal)")
        if not _is_uuid(site_id):
            ui.problem(
                f"{site_id!r} is not a Mist site ID",
                "Site IDs are UUIDs like 0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0, not site names.",
            )
            continue
        ui.say("  reading the site from Mist...")
        try:
            site = collector.fetch("site", site_id, scratch, "insights").data
            aps = collector.fetch("site_device_stats", site_id, scratch, "insights").items
        except CollectError as e:
            ui.problem(str(e), e.fix)
            if e.kind in ("not_found", "bad_request", "forbidden"):
                continue  # most likely a wrong ID: ask again
            raise SystemExit(EXIT_TOOL) from None
        return site_id, site, aps


def show_insights(ui: Ui, site: dict, aps: list[dict], uplink_port: str) -> None:
    ui.heading("Site insights")
    for label, value in site_insights(site, aps, uplink_port, datetime.now(UTC)):
        ui.say(f"  {label:15} {value}")


def load_profile_path(ui: Ui, site_id: str, scratch: Path) -> tuple[Path, str]:
    """(profile path, uplink port). Offers an empty profile when none exists."""
    path = Path("sites") / f"{site_id}.yaml"
    if path.is_file():
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return path, str((doc.get("expectations") or {}).get("uplink_port") or "eth0")
        except yaml.YAMLError:
            return path, "eth0"
    ui.say("")
    ui.problem(
        f"no site profile at {path}",
        "Copy sites/TEMPLATE.yaml there and fill it from the LLD. Without it, the checks "
        "that need design values (SSIDs, subnet, DNS, uplink speed) show ERROR.",
    )
    if not ui.yes("Continue without design values for now?", False):
        raise SystemExit(EXIT_TOOL)
    empty = scratch / f"{site_id}.yaml"
    empty.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "site_id": site_id,
                "site_class": "large",
                "expectations": {"uplink_port": "eth0"},
            }
        ),
        encoding="utf-8",
    )
    return empty, "eth0"


def ask_mop(ui: Ui, mapping) -> tuple[Path, list[MenuEntry]]:
    while True:
        path = Path(
            ui.ask(
                "MOP workbook to fill (a copy is written; the original is untouched)",
                str(DEFAULT_MOP),
            )
        )
        if not path.is_file():
            ui.problem(f"no file at {path}", "Type the path to the customer MOP .xlsx.")
            continue
        try:
            return path, read_sections(mapping, path)
        except PatchError as e:
            ui.problem(str(e), "Use the MOP version this tool's mapping was written for.")
        except Exception as e:  # not an xlsx, locked, ...
            ui.problem(f"cannot read {path}: {e}", "Check it is an .xlsx file and opens in Excel.")


def show_menu(ui: Ui, entries: list[MenuEntry]) -> MenuEntry | None:
    ui.heading("IVP Test Plan")
    for i, e in enumerate(entries, 1):
        tag = "" if e.runnable else "  (not available yet)"
        line = f"  {i}) {e.text}{tag}"
        ui.say(line if e.runnable or not ui.color else f"\033[90m{line}\033[0m")
    ui.say("  0) Exit")
    while True:
        choice = ui.ask("Which test do you want to run")
        if choice in ("0", "q", "exit"):
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(entries):
            entry = entries[int(choice) - 1]
            if entry.runnable:
                return entry
            ui.say(f"  '{entry.text}' is not automated yet; pick another.")
            continue
        ui.say("  Please type one of the numbers.")


# ---------------------------------------------------------------- live results


def why(r: TestResult) -> str:
    """One line: what was expected and what was seen, for each assertion that did not pass."""
    parts = []
    for a in r.assertions:
        if a.verdict is Verdict.PASS:
            continue
        if a.verdict is Verdict.ERROR:
            parts.append(f"{a.field}: {a.message or a.reason.value}")
        else:
            parts.append(f"{a.field} is {_short(a.actual)}, expected {_short(a.expected)}")
    if not parts:
        return r.message
    return "; ".join(parts[:3]) + (f"; +{len(parts) - 3} more" if len(parts) > 3 else "")


def _short(v) -> str:
    if v is None:
        return "not reported"
    if isinstance(v, dict) and "delta" in v:
        return f"{v.get('start')} -> {v.get('end')} (+{v.get('delta')})"
    text = str(v).lower() if isinstance(v, bool) else str(v)
    return text if len(text) <= 40 else text[:37] + "..."


def live_printer(ui: Ui, catalogue) -> Callable[[list[TestResult]], None]:
    titles = {c.test_id: c.title for c in catalogue.checks}

    def show(results: list[TestResult]) -> None:
        by_test: dict[str, list[TestResult]] = {}
        for r in results:
            by_test.setdefault(r.test_id, []).append(r)
        for test_id in sorted(by_test):
            rs = sorted(by_test[test_id], key=lambda r: (r.device.name or "") if r.device else "")
            ui.heading(f"{test_id}  {titles.get(test_id, '')}")
            cells = []
            for r in rs:
                name = (r.device.name or r.device.id) if r.device else "(site)"
                cells.append(f"{name[:12]:12} " + ui.paint(f"{r.verdict.value:5}", r.verdict))
            for i in range(0, len(cells), 4):
                ui.say("  " + "   ".join(cells[i : i + 4]))
            for r in rs:
                if r.verdict in (Verdict.FAIL, Verdict.ERROR):
                    name = (r.device.name or r.device.id) if r.device else "(site)"
                    ui.say("  " + ui.paint(r.verdict.value, r.verdict) + f" {name}: {why(r)}")
            c = Counter(r.verdict for r in rs)
            ui.say("  " + ", ".join(ui.paint(f"{c[v]} {v.value}", v) for v in Verdict if c[v]))

    return show


# ---------------------------------------------------------------- one section


def run_section(
    ui: Ui,
    entry: MenuEntry,
    *,
    site_id: str,
    org_id: str,
    api_host: str,
    mop: Path,
    profile: Path,
    session: Any = None,
    sleep: Any = None,
    portal_factory: Callable[..., Portal] = Portal,
) -> RunOutcome:
    catalogue = load_catalogue(CATALOGUE)
    ui.heading(f"Running: {entry.text}")
    portal: Portal | None = None
    if ui.yes("Take Mist portal screenshots for the MOP (you log in to the portal by hand)?", True):
        url = portal_url_for(api_host) or ui.ask(
            "Mist portal address, e.g. https://manage.mist.com"
        )
        try:
            portal = portal_factory(url, ask=ui.ask_raw, say=ui.say).__enter__()
            portal.login()
        except PortalError as e:
            ui.problem(e.problem, e.fix)
            portal = None
            if not ui.yes("Continue without screenshots?", True):
                raise SystemExit(EXIT_TOOL) from None

    def screenshots(outcome: RunOutcome, mapping) -> list[Picture]:
        if portal is None:
            return []
        ui.heading("Portal screenshots")
        rows = [(rm.row, rm.test_ids, _what(catalogue, rm.test_ids)) for rm in entry.rows]
        ids = {"org_id": org_id, "site_id": site_id}
        taken = take_screenshots(
            portal, rows, outcome.results, ids, outcome.run_dir / "screenshots"
        )
        return [
            Picture(
                cell=f"{mapping.evidence_column}{shot.row}",
                png=png,
                name=f"portal:r{shot.row}:{shot.device_name}",
                description=caption(shot, url),
            )
            for shot, png, url in taken
        ]

    cfg = RunConfig(
        site_id=site_id,
        org_id=org_id,
        api_host=api_host,
        catalogue=CATALOGUE,
        mop=mop,
        out=Path("out"),
        profile=profile,
        mapping=MAPPING,
    )
    try:
        outcome = run(
            cfg,
            session=session,
            sleep=sleep,
            stderr=_Progress(ui),
            on_results=live_printer(ui, catalogue),
            screenshots=screenshots,
        )
    finally:
        if portal is not None:
            if portal.blocked:
                ui.say(f"  read-only lock blocked {len(portal.blocked)} portal request(s)")
            portal.__exit__(None, None, None)

    ui.heading("Result")
    if outcome.tool_error:
        ui.say("RUN FAILED - the checks were not completed.")
        ui.problem(outcome.tool_error, outcome.tool_fix or "")
    c = Counter(r.verdict for r in outcome.results)
    if outcome.results:
        ui.say("  " + ", ".join(ui.paint(f"{c[v]} {v.value}", v) for v in Verdict if c[v]))
    ui.say(f"  run folder: {outcome.run_dir}")
    ui.say(f"  workbook:   {outcome.workbook or '(not written)'}")
    if outcome.workbook and shutil.which("explorer.exe") and shutil.which("wslpath"):
        if ui.yes("Open the run folder in Windows Explorer?", False):
            win = subprocess.run(
                ["wslpath", "-w", str(outcome.run_dir)], capture_output=True, text=True
            ).stdout.strip()
            subprocess.run(["explorer.exe", win], check=False)
    return outcome


def _what(catalogue, test_ids) -> str:
    for c in catalogue.checks:
        if c.test_id in test_ids:
            return f'"{c.mop.description}"'
    return "the page this MOP row asks for"


class _Progress:
    """File-like sink for the runner's progress lines, shown dimmed."""

    def __init__(self, ui: Ui):
        self.ui, self.buf = ui, ""

    def write(self, text: str) -> int:
        self.buf += text
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.strip():
                self.ui.say(f"\033[90m{line}\033[0m" if self.ui.color else line)
        return len(text)

    def flush(self) -> None:
        pass


# ---------------------------------------------------------------- entry point


def main(
    ui: Ui | None = None,
    *,
    session: Any = None,
    sleep: Any = None,
    portal_factory: Callable[..., Portal] = Portal,
) -> int:
    ui = ui or Ui()
    ui.heading("ivp-runner: Mist post-deployment verification")
    try:
        ask_token(ui)
        api_host = ask_cloud(ui)
        kwargs = {"session": session}
        if sleep is not None:
            kwargs["sleep"] = sleep
        try:
            collector = MistApiCollector(api_host, **kwargs)
        except CollectorConfigError as e:
            ui.problem(str(e))
            return EXIT_TOOL
        with tempfile.TemporaryDirectory(prefix="ivp-insights-") as tmp:
            scratch = Path(tmp)
            site_id, site, aps = ask_site(ui, collector, scratch)
            org_id = str(site.get("org_id") or "")
            profile, uplink = load_profile_path(ui, site_id, Path(tempfile.mkdtemp(prefix="ivp-")))
            show_insights(ui, site, aps, uplink)
        mapping = load_mapping(MAPPING)
        mop, entries = ask_mop(ui, mapping)
        last = 0
        while True:
            entry = show_menu(ui, entries)
            if entry is None:
                return last
            outcome = run_section(
                ui,
                entry,
                site_id=site_id,
                org_id=org_id,
                api_host=api_host,
                mop=mop,
                profile=profile,
                session=session,
                sleep=sleep,
                portal_factory=portal_factory,
            )
            last = outcome.exit_code
    except SystemExit as e:
        return int(e.code or 0)
    except KeyboardInterrupt:
        ui.say("\ninterrupted")
        return EXIT_TOOL
