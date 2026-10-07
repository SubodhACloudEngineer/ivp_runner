"""Write result records into the customer MOP workbook.

Reads the source workbook (normally under reference/) and writes a patched
copy (normally under out/). The source is never opened for writing.

Per mapped row: the rolled-up status goes into the status column, and a row
summary card (PNG) is anchored in the evidence column. Per-device cards stay
in the run folder's evidence/ directory.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from ivp_runner.catalogue import Catalogue
from ivp_runner.evidence import render_row_card
from ivp_runner.results import TestResult, Verdict, rollup
from ivp_runner.xlsx_patch import PatchError, Picture, patch_workbook, read_cell_texts

MARKER = "ivp-runner"  # marks text/pictures this tool wrote


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
    evidence_column: str
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
        evidence_column=raw["evidence_column"],
        allowed_statuses=allowed,
        verdict_to_status=v2s,
        rows=rows,
    )


def render_cells(
    mapping: MopMapping, results: Sequence[TestResult], catalogue: Catalogue
) -> dict[str, str]:
    """Status cell ref -> MOP status for every mapped row that has results. Pure."""
    cells: dict[str, str] = {}
    for rm, present, by_test in _mapped_rows(mapping, results):
        verdict = rollup(rollup(r.verdict for r in by_test[t]) for t in present)
        cells[f"{mapping.status_column}{rm.row}"] = mapping.verdict_to_status[verdict]
    return cells


def render_pictures(
    mapping: MopMapping, results: Sequence[TestResult], catalogue: Catalogue
) -> list[Picture]:
    """One row summary card per mapped row, anchored in the evidence column. Pure."""
    pictures = []
    for rm, present, by_test in _mapped_rows(mapping, results):
        rows_results = [r for t in present for r in by_test[t]]
        pictures.append(
            Picture(
                cell=f"{mapping.evidence_column}{rm.row}",
                png=render_row_card(list(rm.test_ids), rows_results, catalogue),
                name=f"{'+'.join(present)}:r{rm.row}",
                description=f"{MARKER} evidence for {', '.join(present)}",
            )
        )
    return pictures


@dataclass(frozen=True)
class WriteReport:
    cells: dict[str, str]
    pictures: list[str]  # cells that received a picture


def write_results(
    mapping: MopMapping,
    results: Sequence[TestResult],
    catalogue: Catalogue,
    src: str | Path,
    dst: str | Path,
) -> WriteReport:
    """Validate the template, then write the patched copy to ``dst``.

    Status text goes into the status column; a row summary card is anchored in
    the evidence column. Refuses (PatchError, nothing written) if a row's
    description doesn't match the mapping, if an evidence cell holds text a
    human wrote, or if it already holds someone else's picture.
    """
    cells = render_cells(mapping, results, catalogue)
    if not cells:
        raise PatchError("no mapped checks in these results; nothing to write")
    rows = sorted({int(ref[len(mapping.status_column) :]) for ref in cells})
    desc_refs = [f"{mapping.description_column}{r}" for r in rows]
    evidence_refs = [f"{mapping.evidence_column}{r}" for r in rows]
    existing = read_cell_texts(src, mapping.sheet, desc_refs + evidence_refs)

    expected = {rm.row: rm.expect_description for rm in mapping.rows}
    for r, ref in zip(rows, desc_refs, strict=True):
        if not existing[ref].strip().startswith(expected[r]):
            raise PatchError(
                f"{mapping.sheet}!{ref} does not start with {expected[r]!r}; "
                "the template layout differs from the mapping"
            )
    for ref in evidence_refs:
        current = existing[ref].strip()
        if current and not current.startswith(MARKER):
            raise PatchError(
                f"{mapping.sheet}!{ref} already holds text a human wrote; not overwriting"
            )
        if current:  # text from an earlier ivp-runner version; the picture replaces it
            cells[ref] = ""

    pictures = render_pictures(mapping, results, catalogue)
    patch_workbook(src, dst, mapping.sheet, cells, pictures)
    return WriteReport(cells=cells, pictures=[p.cell for p in pictures])


def _mapped_rows(mapping: MopMapping, results: Sequence[TestResult]):
    by_test: dict[str, list[TestResult]] = {}
    for r in results:
        by_test.setdefault(r.test_id, []).append(r)
    for rm in mapping.rows:
        present = [t for t in rm.test_ids if t in by_test]
        if present:
            yield rm, present, by_test
