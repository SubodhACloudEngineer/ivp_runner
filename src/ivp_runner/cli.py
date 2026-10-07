"""Command line: ``ivp run``.

    ivp run --site <site-id> --org <org-id> --api-host api.eu.mist.com \\
            --catalogue catalogue/ap.yaml --mop reference/MOP_sample.xlsx --out out/

Fully non-interactive. Progress goes to stderr; stdout carries only the
summary table, or with --json only results.json, so a calling process can
parse it. Exit codes are listed in ivp_runner.runner.
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
    r.add_argument("--site", required=True, help="Mist site ID")
    r.add_argument("--org", required=True, help="Mist org ID (recorded in the results)")
    r.add_argument(
        "--api-host",
        required=True,
        help="Mist API host, e.g. api.mist.com or api.eu.mist.com (no default on purpose)",
    )
    r.add_argument("--catalogue", required=True, type=Path)
    r.add_argument("--mop", required=True, type=Path, help="reference workbook (read only)")
    r.add_argument("--out", required=True, type=Path, help="parent folder for run folders")
    r.add_argument("--profile", type=Path, help="site profile (default: sites/<site-id>.yaml)")
    r.add_argument("--mapping", type=Path, default=Path("catalogue/mop_mapping.yaml"))
    r.add_argument("--json", action="store_true", help="print results.json to stdout, no table")
    return p


def main(argv: list[str] | None = None, *, _session: Any = None, _sleep: Any = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = RunConfig(
        site_id=args.site,
        org_id=args.org,
        api_host=args.api_host,
        catalogue=args.catalogue,
        mop=args.mop,
        out=args.out,
        profile=args.profile or Path("sites") / f"{args.site}.yaml",
        mapping=args.mapping,
    )
    outcome = run(cfg, session=_session, sleep=_sleep, stderr=sys.stderr)
    if args.json:
        sys.stdout.write(outcome.results_path.read_text(encoding="utf-8"))
    else:
        sys.stdout.write(summary_table(outcome))
    return outcome.exit_code


def summary_table(outcome: RunOutcome) -> str:
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
    if outcome.tool_error:
        out.append(f"RUN INCOMPLETE: {outcome.tool_error}")
    out.append(f"run folder: {outcome.run_dir}")
    out.append(f"workbook:   {outcome.workbook or '(not written)'}")
    out.append(f"exit code:  {outcome.exit_code}")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    sys.exit(main())
