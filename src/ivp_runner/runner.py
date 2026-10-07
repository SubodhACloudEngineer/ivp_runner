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

Everything that makes a whole run pointless (bad token, wrong site, API
unreachable, missing or mismatched workbook) is caught before or at the
first request and reported once as a problem plus what to do, never as a
stack trace or as one ERROR per device and check.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import traceback
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ivp_runner import __version__
from ivp_runner.assert_engine import RunContext, evaluate, write_evidence
from ivp_runner.catalogue import CatalogueError, load_catalogue
from ivp_runner.collectors import CollectError, CollectorConfigError, Payload
from ivp_runner.collectors.mist_api import MistApiCollector
from ivp_runner.evidence import write_cards
from ivp_runner.mop_writer import check_template, load_mapping
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
    mop: Path | None  # None only with dry_run
    out: Path
    profile: Path
    mapping: Path
    dry_run: bool = False


@dataclass
class RunOutcome:
    exit_code: int
    run_id: str
    run_dir: Path
    results: list[TestResult] = field(default_factory=list)
    workbook: Path | None = None
    tool_error: str | None = None
    tool_fix: str | None = None
    dry_run: bool = False
    # Collection failures that left some checks unjudged (ERROR api_error),
    # each with what to check; reported once, not once per device.
    problems: list[tuple[str, str]] = field(default_factory=list)

    @property
    def results_path(self) -> Path:
        return self.run_dir / "results.json"


class ToolFailure(Exception):
    """The tool itself cannot complete the run (exit 3).

    ``problem`` says what went wrong; ``fix`` says what to check or change.
    """

    def __init__(self, problem: str, fix: str = ""):
        super().__init__(problem)
        self.problem = problem
        self.fix = fix


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
    outcome = RunOutcome(EXIT_TOOL, run_id, run_dir, dry_run=cfg.dry_run)
    try:
        _run(cfg, outcome, log, session, sleep, clock, started)
    except ToolFailure as e:
        _fail(cfg, outcome, log, started, clock, e.problem, e.fix)
    except Exception as e:  # a bug: keep the traceback in run.log, not on screen
        log.debug("unexpected error\n%s", traceback.format_exc())
        _fail(
            cfg,
            outcome,
            log,
            started,
            clock,
            f"internal error: {type(e).__name__}: {e}",
            "This is a bug in ivp-runner, not a network problem. Send run.log from "
            f"{run_dir} to the tool's maintainer.",
        )
    finally:
        log.info("exit code %d", outcome.exit_code)
        for h in list(log.handlers):
            h.close()
            log.removeHandler(h)
    return outcome


def _fail(cfg, outcome: RunOutcome, log, started, clock, problem: str, fix: str) -> None:
    outcome.exit_code = EXIT_TOOL
    outcome.tool_error, outcome.tool_fix = problem, fix or None
    outcome.workbook = None
    log.error("run could not complete: %s", problem)
    if fix:
        log.error("what to do: %s", fix)
    _write_results_json(cfg, outcome, started, clock(), None)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return len(value) == 36


def _run(cfg, outcome: RunOutcome, log, session, sleep, clock, started) -> None:
    log.info("ivp-runner %s run %s", __version__, outcome.run_id)
    log.info("site %s, org %s, api host %s", cfg.site_id, cfg.org_id, cfg.api_host)

    # 1. Inputs: validate everything before any API call.
    for flag, value in (("--site", cfg.site_id), ("--org", cfg.org_id)):
        if not _is_uuid(value):
            raise ToolFailure(
                f"{flag} {value!r} is not a Mist ID",
                f"Mist {flag[2:]} IDs are UUIDs like 0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0. "
                "Copy it from the Mist portal; do not use the site or org name.",
            )
    if not cfg.catalogue.is_file():
        raise ToolFailure(
            f"catalogue not found: {cfg.catalogue}",
            "Point --catalogue at the check catalogue, normally catalogue/ap.yaml.",
        )
    if not cfg.mapping.is_file():
        raise ToolFailure(
            f"MOP mapping not found: {cfg.mapping}",
            "Point --mapping at catalogue/mop_mapping.yaml, or run from the repository root.",
        )
    if cfg.mop is None and not cfg.dry_run:
        raise ToolFailure("no --mop workbook given", "Give --mop, or use --dry-run.")
    if cfg.mop is not None:
        if not cfg.mop.is_file():
            raise ToolFailure(
                f"MOP workbook not found: {cfg.mop}",
                "Point --mop at the customer MOP template (.xlsx). It is only read; the "
                "populated copy goes to the run folder.",
            )
        if not zipfile.is_zipfile(cfg.mop):
            raise ToolFailure(
                f"{cfg.mop} is not an .xlsx workbook",
                "Save the MOP as an Excel workbook (.xlsx), not .xls or .csv. If it is open "
                "in Excel with a sync client, wait for the file to finish syncing.",
            )
    if not cfg.profile.is_file():
        raise ToolFailure(
            f"site profile not found: {cfg.profile}",
            "Copy sites/TEMPLATE.yaml to that path and fill it from the LLD (uplink_port, "
            "ssids, mgmt_subnet, dns_servers, min_uplink_speed_mbps); blanks make the "
            "checks that need them report ERROR.",
        )
    try:
        catalogue = load_catalogue(cfg.catalogue)
    except (CatalogueError, ValueError) as e:
        raise ToolFailure(f"invalid catalogue {cfg.catalogue}: {e}") from None
    try:
        profile = load_site_profile(cfg.profile)
    except (ValueError, OSError) as e:
        raise ToolFailure(
            f"invalid site profile {cfg.profile}: {e}",
            "Compare it with sites/TEMPLATE.yaml; IP values must be valid addresses or subnets.",
        ) from None
    try:
        mapping = load_mapping(cfg.mapping)
    except (ValueError, KeyError, OSError) as e:
        raise ToolFailure(f"invalid MOP mapping {cfg.mapping}: {e}") from None
    if profile.site_id != cfg.site_id:
        raise ToolFailure(
            f"profile {cfg.profile} is for site {profile.site_id}, not --site {cfg.site_id}",
            "Use the profile for this site (--profile), or fix site_id in the profile.",
        )
    if cfg.mop is not None:
        # Before collecting, so a wrong MOP version costs seconds, not a whole run.
        try:
            check_template(mapping, cfg.mop)
        except PatchError as e:
            raise ToolFailure(
                f"MOP workbook {cfg.mop} does not match the mapping: {e}",
                "Use the MOP template this mapping was written for, or update "
                "catalogue/mop_mapping.yaml to the new row layout.",
            ) from None
        except (OSError, zipfile.BadZipFile, KeyError) as e:
            raise ToolFailure(
                f"cannot read MOP workbook {cfg.mop}: {e}",
                "Close the workbook in Excel if it is open, and check it opens there.",
            ) from None
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
    log.info("connecting to %s", collector._host)

    # 3-4. Collect start and end samples (no added wait between them).
    start = collector.collect(catalogue, profile.site_class, cfg.site_id, outcome.run_dir, "start")
    _log_collection(log, "start", start)
    fatal = next((g for g in start.values() if isinstance(g, CollectError) and g.fatal), None)
    if fatal is not None:  # every request would fail the same way: stop here
        raise ToolFailure(str(fatal), fatal.fix)
    end = collector.collect(catalogue, profile.site_class, cfg.site_id, outcome.run_dir, "end")
    _log_collection(log, "end", end)
    for phase, got in (("start", start), ("end", end)):
        for source, value in got.items():
            if isinstance(value, CollectError):
                outcome.problems.append((f"{phase} {source}: {value}", value.fix))

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
    if cfg.dry_run:
        log.info("dry run: workbook not written")
        _write_results_json(cfg, outcome, started, clock(), tz)
        return
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", site_name).strip("_") or cfg.site_id[:8]
    workbook = outcome.run_dir / f"MOP_{safe_name}_{outcome.run_id}.xlsx"
    ref_hash = _sha256(cfg.mop)
    try:
        report = write_mop(mapping, outcome.results, catalogue, cfg.mop, workbook)
    except PatchError as e:
        raise ToolFailure(
            f"MOP workbook not written: {e}",
            "Results, raw data and evidence are in the run folder. Fix the workbook and run again.",
        ) from None
    except OSError as e:
        raise ToolFailure(
            f"MOP workbook not written: {e.strerror or e} ({e.filename or workbook})",
            "Check there is free disk space and that --out is writable. Results, raw data "
            "and evidence are in the run folder.",
        ) from None
    if _sha256(cfg.mop) != ref_hash:  # never expected; the writer only reads src
        raise ToolFailure(f"reference workbook {cfg.mop} changed during the run")
    outcome.workbook = workbook
    log.info(
        "workbook %s: %d status cells, %d evidence pictures (%s)",
        workbook,
        len(report.cells),
        len(report.pictures),
        ", ".join(f"{mapping.sheet}!{c}" for c in report.pictures),
    )

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
            "source": str(cfg.mop) if cfg.mop else None,
            "output": str(outcome.workbook) if outcome.workbook else None,
        },
        "dry_run": cfg.dry_run,
        "started": {"utc": s_utc, "local": s_local, "timezone": tz},
        "finished": {"utc": f_utc, "local": f_local, "timezone": tz},
        "exit_code": outcome.exit_code,
        "tool_error": outcome.tool_error,
        "tool_fix": outcome.tool_fix,
        "collection_problems": [{"problem": p, "fix": f} for p, f in outcome.problems],
        "counts": {v.value: sum(r.verdict is v for r in outcome.results) for v in Verdict},
    }
    doc = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run": header,
        "results": [r.model_dump(mode="json", by_alias=True) for r in outcome.results],
    }
    outcome.results_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def _log_collection(log, phase: str, collection) -> None:
    reported: set[int] = set()
    for source, got in collection.items():
        if isinstance(got, Payload):
            log.info(
                "%s %s: %d item(s), raw %s", phase, source, len(got.items), Path(got.raw_path).name
            )
        elif id(got) in reported:  # a fatal error reused for sources never requested
            log.info("%s %s: not requested (same failure)", phase, source)
        else:
            reported.add(id(got))
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
    log.setLevel(logging.DEBUG)
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%SZ")
    fmt.converter = lambda *_: datetime.now(UTC).timetuple()
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if stderr is not None:
        sh = logging.StreamHandler(stderr)
        sh.setLevel(logging.INFO)  # tracebacks (DEBUG) go to run.log only
        sh.setFormatter(logging.Formatter("ivp: %(message)s"))
        log.addHandler(sh)
    return log
