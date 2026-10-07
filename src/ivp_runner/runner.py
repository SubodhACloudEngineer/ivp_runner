"""End-to-end run: load, collect, evaluate, render evidence, populate the MOP copy.

Everything for one run lands in ``<out>/<run-id>/``:

    results.json   run header + one record per (test, device)
    raw/           every API response, plus manifest.json
    evidence/      <test>/<device>.png card + .json field evidence
    MOP_<site>_<run-id>.xlsx   populated copy of the reference workbook
    run.log        timestamped steps (never the token)

Non-interactive by design: no prompts anywhere, and the token comes only
from MIST_API_TOKEN.

Exit codes: 0 all PASS (SKIP alone doesn't count), 1 any FAIL, 2 any ERROR
(this wins over FAIL), 3 the tool could not complete.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ivp_runner import __version__
from ivp_runner.assert_engine import RunContext, evaluate, write_evidence
from ivp_runner.catalogue import CatalogueError, load_catalogue
from ivp_runner.collectors import CollectorConfigError, Payload
from ivp_runner.collectors.mist_api import MistApiCollector
from ivp_runner.evidence import write_cards
from ivp_runner.mop_writer import load_mapping
from ivp_runner.mop_writer import write_results as write_mop
from ivp_runner.results import SiteRef, TestResult, Verdict, timestamps
from ivp_runner.site_profile import load_site_profile
from ivp_runner.xlsx_patch import PatchError

EXIT_PASS, EXIT_FAIL, EXIT_ERROR, EXIT_TOOL = 0, 1, 2, 3
RUN_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RunConfig:
    site_id: str
    org_id: str
    api_host: str
    catalogue: Path
    mop: Path
    out: Path
    profile: Path
    mapping: Path


@dataclass
class RunOutcome:
    exit_code: int
    run_id: str
    run_dir: Path
    results: list[TestResult] = field(default_factory=list)
    workbook: Path | None = None
    tool_error: str | None = None

    @property
    def results_path(self) -> Path:
        return self.run_dir / "results.json"


class ToolFailure(Exception):
    """The tool itself cannot complete the run (exit 3)."""


def exit_code_for(results: list[TestResult]) -> int:
    verdicts = {r.verdict for r in results}
    if Verdict.ERROR in verdicts:
        return EXIT_ERROR
    if Verdict.FAIL in verdicts:
        return EXIT_FAIL
    return EXIT_PASS


def new_run_dir(out: Path, site_id: str, now: datetime) -> tuple[str, Path]:
    base = f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{site_id[:8]}"
    run_id, n = base, 1
    while (out / run_id).exists():
        n += 1
        run_id = f"{base}-{n}"
    run_dir = out / run_id
    run_dir.mkdir(parents=True)
    return run_id, run_dir


def run(
    cfg: RunConfig,
    *,
    session: Any = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    stderr: Any = None,
) -> RunOutcome:
    started = clock()
    cfg.out.mkdir(parents=True, exist_ok=True)
    run_id, run_dir = new_run_dir(cfg.out, cfg.site_id, started)
    log = _logger(run_id, run_dir / "run.log", stderr)
    outcome = RunOutcome(EXIT_TOOL, run_id, run_dir)
    try:
        _run(cfg, outcome, log, session, sleep, clock, started)
    except ToolFailure as e:
        outcome.exit_code = EXIT_TOOL
        outcome.tool_error = str(e)
        log.error("run could not complete: %s", e)
        _write_results_json(cfg, outcome, started, clock(), None)
    finally:
        log.info("exit code %d", outcome.exit_code)
        for h in list(log.handlers):
            h.close()
            log.removeHandler(h)
    return outcome


def _run(cfg, outcome: RunOutcome, log, session, sleep, clock, started) -> None:
    log.info("ivp-runner %s run %s", __version__, outcome.run_id)
    log.info("site %s, org %s, api host %s", cfg.site_id, cfg.org_id, cfg.api_host)

    # 1. Inputs: validate everything before any API call.
    for label, path in (
        ("catalogue", cfg.catalogue),
        ("MOP workbook", cfg.mop),
        ("MOP mapping", cfg.mapping),
    ):
        if not path.is_file():
            raise ToolFailure(f"{label} not found: {path}")
    if not cfg.profile.is_file():
        raise ToolFailure(
            f"site profile not found: {cfg.profile}. Copy sites/TEMPLATE.yaml to that path "
            "and fill it from the LLD (uplink_port, ssids, mgmt_subnet, dns_servers, "
            "min_uplink_speed_mbps); blanks make the checks that need them report ERROR."
        )
    try:
        catalogue = load_catalogue(cfg.catalogue)
        profile = load_site_profile(cfg.profile)
        mapping = load_mapping(cfg.mapping)
    except (CatalogueError, ValueError) as e:
        raise ToolFailure(f"invalid input: {e}") from None
    if profile.site_id != cfg.site_id:
        raise ToolFailure(
            f"profile {cfg.profile} is for site {profile.site_id}, not --site {cfg.site_id}"
        )
    log.info(
        "catalogue %s: %d checks; profile class %s",
        cfg.catalogue,
        len(catalogue.checks),
        profile.site_class,
    )

    # 2. Collector (token problems stop here, before any request).
    kwargs = {"session": session}
    if sleep is not None:
        kwargs["sleep"] = sleep
    try:
        collector = MistApiCollector(cfg.api_host, **kwargs)
    except CollectorConfigError as e:
        raise ToolFailure(str(e)) from None

    # 3-4. Collect start and end samples (no added wait between them).
    start = collector.collect(catalogue, profile.site_class, cfg.site_id, outcome.run_dir, "start")
    _log_collection(log, "start", start)
    end = collector.collect(catalogue, profile.site_class, cfg.site_id, outcome.run_dir, "end")
    _log_collection(log, "end", end)

    site_doc = start.get("site")
    if isinstance(site_doc, Payload):
        site_name = str(site_doc.data.get("name") or cfg.site_id)
        tz = str(site_doc.data.get("timezone") or "UTC")
    else:
        site_name, tz = cfg.site_id, "UTC"
        log.warning("site details unavailable (%s); using UTC for local times", site_doc)

    # 5. Evaluate.
    ctx = RunContext(
        run_id=outcome.run_id,
        site=SiteRef(id=cfg.site_id, name=site_name, **{"class": profile.site_class}),
        timezone=tz,
        out_dir=str(outcome.run_dir),
        started_at=started,
    )
    evaluations = evaluate(catalogue, profile, start, end, ctx)
    outcome.results = [e.result for e in evaluations]
    outcome.exit_code = exit_code_for(outcome.results)
    log.info("evaluated %d results", len(outcome.results))

    # 6. Evidence: field JSON + PNG cards.
    write_evidence(evaluations)
    cards = write_cards(outcome.results, catalogue)
    log.info("wrote %d evidence cards", len(cards))

    # 7. Populated MOP copy. Results and evidence are already safe on disk.
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", site_name).strip("_") or cfg.site_id[:8]
    workbook = outcome.run_dir / f"MOP_{safe_name}_{outcome.run_id}.xlsx"
    ref_hash = _sha256(cfg.mop)
    try:
        cells = write_mop(mapping, outcome.results, catalogue, cfg.mop, workbook)
    except (PatchError, OSError) as e:
        _write_results_json(cfg, outcome, started, clock(), tz)
        raise ToolFailure(f"MOP workbook not written: {e}") from None
    if _sha256(cfg.mop) != ref_hash:  # never expected; the writer only reads src
        raise ToolFailure(f"reference workbook {cfg.mop} changed during the run")
    outcome.workbook = workbook
    log.info("workbook %s (%d cells written)", workbook, len(cells))

    _write_results_json(cfg, outcome, started, clock(), tz)


def _write_results_json(cfg, outcome: RunOutcome, started, finished, tz: str | None) -> None:
    tz = tz or "UTC"
    s_utc, s_local = timestamps(started, tz)
    f_utc, f_local = timestamps(finished, tz)
    header = {
        "run_id": outcome.run_id,
        "tool_version": __version__,
        "org_id": cfg.org_id,
        "site_id": cfg.site_id,
        "api_host": cfg.api_host,
        "catalogue": {"path": str(cfg.catalogue), "sha256": _sha256(cfg.catalogue)},
        "profile": {"path": str(cfg.profile), "sha256": _sha256(cfg.profile)},
        "mop": {
            "source": str(cfg.mop),
            "output": str(outcome.workbook) if outcome.workbook else None,
        },
        "started": {"utc": s_utc, "local": s_local, "timezone": tz},
        "finished": {"utc": f_utc, "local": f_local, "timezone": tz},
        "exit_code": outcome.exit_code,
        "tool_error": outcome.tool_error,
        "counts": {v.value: sum(r.verdict is v for r in outcome.results) for v in Verdict},
    }
    doc = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run": header,
        "results": [r.model_dump(mode="json", by_alias=True) for r in outcome.results],
    }
    outcome.results_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def _log_collection(log, phase: str, collection) -> None:
    for source, got in collection.items():
        if isinstance(got, Payload):
            log.info(
                "%s %s: %d item(s), raw %s", phase, source, len(got.items), Path(got.raw_path).name
            )
        else:
            log.error("%s %s: %s", phase, source, got)


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _logger(run_id: str, path: Path, stderr) -> logging.Logger:
    log = logging.getLogger(f"ivp_runner.run.{run_id}")
    log.setLevel(logging.INFO)
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%SZ")
    fmt.converter = lambda *_: datetime.now(UTC).timetuple()
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if stderr is not None:
        sh = logging.StreamHandler(stderr)
        sh.setFormatter(logging.Formatter("ivp: %(message)s"))
        log.addHandler(sh)
    return log
