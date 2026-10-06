"""Write result records into the customer MOP workbook.

Reads the source workbook (normally under reference/) and writes a patched
copy (normally under out/). The source is never opened for writing.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from ivp_runner.catalogue import Catalogue
from ivp_runner.results import TestResult, Verdict, rollup
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


def render_cells(
    mapping: MopMapping, results: Sequence[TestResult], catalogue: Catalogue
) -> dict[str, str]:
    """Cell ref -> text for every mapped row that has results. Pure; touches no files."""
    by_test: dict[str, list[TestResult]] = {}
    for r in results:
        by_test.setdefault(r.test_id, []).append(r)
    cells: dict[str, str] = {}
    for rm in mapping.rows:
        present = [t for t in rm.test_ids if t in by_test]
        if not present:
            continue
        verdict = rollup(rollup(r.verdict for r in by_test[t]) for t in present)
        cells[f"{mapping.status_column}{rm.row}"] = mapping.verdict_to_status[verdict]
        cells[f"{mapping.summary_column}{rm.row}"] = _summary(present, by_test, catalogue)
    return cells


def write_results(
    mapping: MopMapping,
    results: Sequence[TestResult],
    catalogue: Catalogue,
    src: str | Path,
    dst: str | Path,
) -> dict[str, str]:
    """Validate the template, then write the patched copy to ``dst``. Returns the cells written."""
    cells = render_cells(mapping, results, catalogue)
    if not cells:
        raise PatchError("no mapped checks in these results; nothing to write")
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


def _summary(
    test_ids: list[str], by_test: dict[str, list[TestResult]], catalogue: Catalogue
) -> str:
    latest = max((r for t in test_ids for r in by_test[t]), key=lambda r: r.timestamp_utc)
    lines = [f"{MARKER} {latest.timestamp_utc} ({latest.timestamp_local} {latest.timezone})"]
    for t in test_ids:
        records = by_test[t]
        check = catalogue.get(t)
        verdict = rollup(r.verdict for r in records)
        counts = Counter(r.verdict for r in records)
        shown = ", ".join(f"{counts[v]} {v.value}" for v in Verdict if counts[v])
        lines.append(f"{t} {verdict.value}: {check.title} [{shown}]")
        for v in (Verdict.FAIL, Verdict.ERROR):
            groups: dict[str, list[str]] = {}
            for r in records:
                if r.verdict is v:
                    name = r.device.name or r.device.id if r.device else "site-wide"
                    groups.setdefault(r.reason.value, []).append(name)
            for reason, names in groups.items():
                more = len(names) - MAX_DEVICES_LISTED
                tail = f" +{more} more" if more > 0 else ""
                lines.append(
                    f"  {v.value} ({reason}): {', '.join(names[:MAX_DEVICES_LISTED])}{tail}"
                )
        if check.coverage == "partial" and check.limitation:
            first = " ".join(check.limitation.split()).split(". ")[0].rstrip(".")
            lines.append(f"  Partial check: {first}.")
    lines.append(f"Full results: run {latest.run_id}")
    return "\n".join(lines)
