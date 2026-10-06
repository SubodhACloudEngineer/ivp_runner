"""Write a RunResult into the customer MOP workbook.

Reads the source workbook (normally under reference/) and writes a patched
copy (normally under out/). The source is never opened for writing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ivp_runner.results import CheckResult, RunResult, Verdict, rollup
from ivp_runner.xlsx_patch import PatchError, patch_cells, read_cell_texts

MARKER = "ivp-runner"  # first word of every summary cell this tool writes
MAX_DEVICES_LISTED = 10


@dataclass(frozen=True)
class RowMap:
    row: int
    test_ids: tuple[str, ...]
    expect_description: str


@dataclass(frozen=True)
class MopMapping:
    sheet: str
    description_column: str
    status_column: str
    summary_column: str
    allowed_statuses: tuple[str, ...]
    verdict_to_status: dict[Verdict, str]
    rows: tuple[RowMap, ...]


def load_mapping(path: str | Path) -> MopMapping:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if raw.get("schema_version") != 1:
        raise ValueError("mop mapping: schema_version must be 1")
    allowed = tuple(raw["allowed_statuses"])
    v2s = {Verdict(k): v for k, v in raw["verdict_to_status"].items()}
    missing = set(Verdict) - set(v2s)
    if missing:
        raise ValueError(
            f"mop mapping: verdict_to_status missing {sorted(v.value for v in missing)}"
        )
    bad = sorted({v for v in v2s.values() if v not in allowed})
    if bad:
        raise ValueError(f"mop mapping: statuses {bad} are not in allowed_statuses")
    rows = tuple(
        RowMap(int(r["row"]), tuple(r["test_ids"]), str(r["expect_description"]))
        for r in raw["rows"]
    )
    seen = [t for r in rows for t in r.test_ids]
    if len(seen) != len(set(seen)):
        raise ValueError("mop mapping: a test ID is mapped to more than one row")
    return MopMapping(
        sheet=raw["sheet"],
        description_column=raw["description_column"],
        status_column=raw["status_column"],
        summary_column=raw["summary_column"],
        allowed_statuses=allowed,
        verdict_to_status=v2s,
        rows=rows,
    )


def render_cells(mapping: MopMapping, run: RunResult) -> dict[str, str]:
    """Cell ref -> text for every mapped row. Pure; touches no files."""
    by_id = {c.test_id: c for c in run.checks}
    cells: dict[str, str] = {}
    for rm in mapping.rows:
        checks = [by_id[t] for t in rm.test_ids if t in by_id]
        if not checks:
            continue
        verdict = rollup(c.verdict for c in checks)
        cells[f"{mapping.status_column}{rm.row}"] = mapping.verdict_to_status[verdict]
        cells[f"{mapping.summary_column}{rm.row}"] = _summary(run, checks)
    return cells


def write_results(
    mapping: MopMapping, run: RunResult, src: str | Path, dst: str | Path
) -> dict[str, str]:
    """Validate the template, then write the patched copy to ``dst``. Returns the cells written."""
    cells = render_cells(mapping, run)
    if not cells:
        raise PatchError("no mapped checks in this run; nothing to write")
    rows = sorted({int(ref[len(mapping.status_column) :]) for ref in cells})
    desc_refs = [f"{mapping.description_column}{r}" for r in rows]
    summary_refs = [f"{mapping.summary_column}{r}" for r in rows]
    existing = read_cell_texts(src, mapping.sheet, desc_refs + summary_refs)

    expected = {rm.row: rm.expect_description for rm in mapping.rows}
    for r, ref in zip(rows, desc_refs, strict=True):
        if not existing[ref].strip().startswith(expected[r]):
            raise PatchError(
                f"{mapping.sheet}!{ref} does not start with {expected[r]!r}; "
                "the template layout differs from the mapping"
            )
    for ref in summary_refs:
        current = existing[ref].strip()
        if current and not current.startswith(MARKER):
            raise PatchError(
                f"{mapping.sheet}!{ref} already holds text a human wrote; not overwriting"
            )

    patch_cells(src, dst, mapping.sheet, cells)
    return cells


def _summary(run: RunResult, checks: list[CheckResult]) -> str:
    lines = [f"{MARKER} {run.finished_at.utc} ({run.finished_at.local} {run.finished_at.tz})"]
    for c in checks:
        counts = ", ".join(f"{n} {v}" for v, n in c.counts.items() if n)
        lines.append(f"{c.test_id} {c.verdict.value}: {c.title} [{counts}]")
        for verdict in (Verdict.FAIL, Verdict.ERROR, Verdict.INCONCLUSIVE):
            by_reason: dict[str, list[str]] = {}
            for d in c.devices:
                if d.verdict is verdict:
                    by_reason.setdefault(d.reason.value, []).append(d.device.name)
            for reason, names in by_reason.items():
                shown = ", ".join(names[:MAX_DEVICES_LISTED])
                more = (
                    f" +{len(names) - MAX_DEVICES_LISTED} more"
                    if len(names) > MAX_DEVICES_LISTED
                    else ""
                )
                lines.append(f"  {verdict.value} ({reason}): {shown}{more}")
        if c.coverage == "partial" and c.limitation:
            lines.append(f"  Partial check: {c.limitation.split('. ')[0].rstrip('.')}.")
    lines.append(f"Full results: run {run.run_id}")
    return "\n".join(lines)
