"""Command line: ``ivp run``.

    ivp run --site <site-id> --org <org-id> --api-host api.eu.mist.com \\
            --catalogue catalogue/ap.yaml --mop reference/MOP_sample.xlsx --out out/

``--dry-run`` collects and evaluates as usual (results.json, raw data and
evidence are still written) but writes no workbook; ``--mop`` is then
optional and, if given, only checked.

With every argument given it never prompts, so it can be scripted. Missing
arguments are asked for when run at a terminal; ``ivp`` with no arguments
starts the guided mode (ivp_runner.interactive). Progress goes to stderr;
stdout carries only the summary table, or with --json only results.json.
Exit codes are listed in ivp_runner.runner.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from ivp_runner.results import Verdict
from ivp_runner.runner import EXIT_TOOL, RunConfig, RunOutcome, run


class _Parser(argparse.ArgumentParser):
    """Usage errors exit 3 ("couldn't run"), never 2, which means "a check errored"."""

    def error(self, message: str):  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_TOOL, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="ivp", description="Post-deployment IVP runner for Juniper Mist sites.")
    sub = p.add_subparsers(dest="command", required=True, parser_class=_Parser)
    r = sub.add_parser("run", help="collect, evaluate and populate the MOP for one site")
    r.add_argument("--site", help="Mist site ID")
    r.add_argument("--org", help="Mist org ID (recorded in the results)")
    r.add_argument(
        "--api-host",
        help="Mist API host, e.g. api.mist.com or api.eu.mist.com (no default on purpose)",
    )
    r.add_argument("--catalogue", type=Path, default=Path("catalogue/ap.yaml"))
    r.add_argument(
        "--mop", type=Path, help="reference workbook (read only); optional with --dry-run"
    )
    r.add_argument("--out", type=Path, default=Path("out"), help="parent folder for run folders")
    r.add_argument("--profile", type=Path, help="site profile (default: sites/<site-id>.yaml)")
    r.add_argument("--mapping", type=Path, default=Path("catalogue/mop_mapping.yaml"))
    r.add_argument("--json", action="store_true", help="print results.json to stdout, no table")
    r.add_argument(
        "--dry-run",
        action="store_true",
        help="collect and evaluate, but write no workbook",
    )
    return p


_PROMPTS = {
    "site": "Site ID",
    "org": "Org ID",
    "api_host": "Mist API host (api.mist.com global, api.eu.mist.com EU)",
    "mop": "MOP workbook (.xlsx)",
}


def main(argv: list[str] | None = None, *, _session: Any = None, _sleep: Any = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        from ivp_runner.interactive import main as guided

        return guided(session=_session, sleep=_sleep)
    parser = build_parser()
    args = parser.parse_args(argv)
    missing = [k for k in _PROMPTS if getattr(args, k) is None]
    if args.dry_run and "mop" in missing:
        missing.remove("mop")
    if missing:
        if not sys.stdin.isatty():
            flags = ", ".join("--" + k.replace("_", "-") for k in missing)
            hint = "; --dry-run skips the workbook" if "mop" in missing else ""
            parser.error(
                f"missing {flags}{hint} (or run `ivp` with no arguments for the guided mode)"
            )
        from ivp_runner.interactive import Ui

        ui = Ui()
        for k in missing:
            value = ui.ask(_PROMPTS[k])
            setattr(args, k, Path(value) if k == "mop" else value)
    cfg = RunConfig(
        site_id=args.site.strip(),
        org_id=args.org.strip(),
        api_host=args.api_host,
        catalogue=args.catalogue,
        mop=args.mop,
        out=args.out,
        profile=args.profile or Path("sites") / f"{args.site.strip()}.yaml",
        mapping=args.mapping,
        dry_run=args.dry_run,
    )
    try:
        outcome = run(cfg, session=_session, sleep=_sleep, stderr=sys.stderr)
    except OSError as e:  # the run folder itself could not be created
        sys.stdout.write(
            _failure_block(
                f"cannot create a run folder under --out {cfg.out}: {e.strerror or e}",
                "Choose a folder you can write to, e.g. --out out/",
            )
        )
        return EXIT_TOOL
    except KeyboardInterrupt:
        sys.stderr.write("ivp: interrupted; nothing further written\n")
        return EXIT_TOOL
    if args.json:
        sys.stdout.write(outcome.results_path.read_text(encoding="utf-8"))
    else:
        sys.stdout.write(summary_table(outcome))
    return outcome.exit_code


def _failure_block(problem: str, fix: str | None) -> str:
    out = ["", "RUN FAILED - the checks were not completed.", f"  Problem:    {problem}"]
    if fix:
        out.append(f"  What to do: {fix}")
    return "\n".join(out) + "\n\n"


def summary_table(outcome: RunOutcome) -> str:
    if outcome.tool_error and not outcome.results:
        return (
            _failure_block(outcome.tool_error, outcome.tool_fix)
            + f"run folder: {outcome.run_dir} (run.log has the details)\n"
            + f"exit code:  {outcome.exit_code}\n"
        )
    rows = sorted(
        (
            r.test_id,
            (r.device.name or r.device.id) if r.device else "(site)",
            r.verdict.value,
            r.reason.value if r.reason else "",
        )
        for r in outcome.results
    )
    head = ("TEST ID", "DEVICE", "VERDICT", "REASON")
    widths = [max([len(head[i])] + [len(row[i]) for row in rows]) for i in range(4)]
    line = "  ".join(f"{{:<{w}}}" for w in widths)
    out = [line.format(*head), line.format(*("-" * w for w in widths))]
    out += [line.format(*row).rstrip() for row in rows]
    out.append("")

    per_test: dict[str, Counter] = {}
    for r in outcome.results:
        per_test.setdefault(r.test_id, Counter())[r.verdict] += 1
    for test_id in sorted(per_test):
        c = per_test[test_id]
        out.append(f"{test_id}: " + ", ".join(f"{c[v]} {v.value}" for v in Verdict if c[v]))
    totals = Counter(r.verdict for r in outcome.results)
    out.append(
        "TOTAL: "
        + (", ".join(f"{totals[v]} {v.value}" for v in Verdict if totals[v]) or "no results")
    )
    out.append("")
    if outcome.problems:
        out.append("Some data could not be collected; checks that needed it are ERROR (api_error):")
        for problem, fix in outcome.problems:
            out.append(f"  Problem:    {problem}")
            if fix:
                out.append(f"  What to do: {fix}")
        out.append("")
    if outcome.tool_error:
        out.append(_failure_block(outcome.tool_error, outcome.tool_fix).strip("\n"))
        out.append("")
    out.append(f"run folder: {outcome.run_dir}")
    if outcome.dry_run:
        out.append("workbook:   (dry run - not written)")
    else:
        out.append(f"workbook:   {outcome.workbook or '(not written)'}")
    out.append(f"exit code:  {outcome.exit_code}")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    sys.exit(main())
