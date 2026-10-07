"""Write result records into the customer MOP workbook.

Reads the source workbook (normally under reference/) and writes a patched
copy (normally under out/). The source is never opened for writing.

Per mapped row: the rolled-up status goes into the status column, and the
caller's picture for that row (a Mist portal screenshot) is anchored in the
evidence column. Rows without a picture get none: summary cards stay in the
run folder, never in the workbook.
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
class Section:
    title: str
    header_row: int


@dataclass(frozen=True)
class MopMapping:
    sheet: str
    description_column: str
    status_column: str
    evidence_column: str
    allowed_statuses: tuple[str, ...]
    verdict_to_status: dict[Verdict, str]
    rows: tuple[RowMap, ...]
    section_column: str = "A"
    sections: tuple[Section, ...] = ()

    def rows_in(self, section: Section) -> tuple[RowMap, ...]:
        """Mapped rows between this section's header and the next one."""
        later = [s.header_row for s in self.sections if s.header_row > section.header_row]
        end = min(later, default=10**9)
        return tuple(r for r in self.rows if section.header_row < r.row < end)


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
    sections = tuple(
        Section(str(s["title"]), int(s["header_row"])) for s in raw.get("sections") or []
    )
    if [s.header_row for s in sections] != sorted({s.header_row for s in sections}):
        raise ValueError("mop mapping: sections must be in sheet order, one per header row")
    return MopMapping(
        sheet=raw["sheet"],
        description_column=raw["description_column"],
        status_column=raw["status_column"],
        evidence_column=raw["evidence_column"],
        allowed_statuses=allowed,
        verdict_to_status=v2s,
        rows=rows,
        section_column=raw.get("section_column", "A"),
        sections=sections,
    )


@dataclass(frozen=True)
class MenuEntry:
    section: Section
    text: str  # header text as it appears in the workbook
    rows: tuple[RowMap, ...]

    @property
    def runnable(self) -> bool:
        return bool(self.rows)


def read_sections(mapping: MopMapping, src: str | Path) -> list[MenuEntry]:
    """Menu entries in sheet order; PatchError if a header has moved. Read-only."""
    refs = [f"{mapping.section_column}{s.header_row}" for s in mapping.sections]
    texts = read_cell_texts(src, mapping.sheet, refs)
    entries = []
    for s, ref in zip(mapping.sections, refs, strict=True):
        text = " ".join(texts[ref].split())
        if not text.startswith(s.title):
            raise PatchError(
                f"{mapping.sheet}!{ref} does not start with {s.title!r}; "
                "the template layout differs from the mapping"
            )
        entries.append(MenuEntry(s, text, mapping.rows_in(s)))
    return entries


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
    """One row summary card per mapped row. Pure.

    The cards are saved in the run folder (evidence/mop_rows/); the workbook's
    evidence column gets portal screenshots instead.
    """
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
    pictures: Sequence[Picture] = (),
) -> WriteReport:
    """Validate the template, then write the patched copy to ``dst``.

    Status text goes into the status column; each of ``pictures`` (normally a
    portal screenshot per row) is anchored at its cell in the evidence column.
    Refuses (PatchError, nothing written) if a row's description doesn't match
    the mapping, if an evidence cell holds text a human wrote, if it already
    holds someone else's picture, or if a picture is aimed outside the
    evidence column of a row that has results.
    """
    cells = render_cells(mapping, results, catalogue)
    if not cells:
        raise PatchError("no mapped checks in these results; nothing to write")
    allowed = {f"{mapping.evidence_column}{ref[len(mapping.status_column) :]}" for ref in cells}
    for ref in check_template(mapping, src):
        cells[ref] = ""  # text from an earlier ivp-runner version; the picture replaces it

    stray = [p.cell for p in pictures if p.cell not in allowed]
    if stray:
        raise PatchError(f"pictures aimed outside the evidence cells of these results: {stray}")
    patch_workbook(src, dst, mapping.sheet, cells, list(pictures))
    return WriteReport(cells=cells, pictures=[p.cell for p in pictures])


def check_template(mapping: MopMapping, src: str | Path) -> list[str]:
    """Refuse (PatchError) a workbook whose mapped rows don't match the mapping.

    Every mapped row's description must start with ``expect_description``, and
    its evidence cell must be empty or hold text this tool wrote. Returns the
    evidence cells holding our old text, which the caller clears. Read-only.
    """
    rows = [rm.row for rm in mapping.rows]
    desc_refs = [f"{mapping.description_column}{r}" for r in rows]
    evidence_refs = [f"{mapping.evidence_column}{r}" for r in rows]
    existing = read_cell_texts(src, mapping.sheet, desc_refs + evidence_refs)
    for rm, ref in zip(mapping.rows, desc_refs, strict=True):
        if not existing[ref].strip().startswith(rm.expect_description):
            raise PatchError(
                f"{mapping.sheet}!{ref} does not start with {rm.expect_description!r}; "
                "the template layout differs from the mapping"
            )
    ours = []
    for ref in evidence_refs:
        current = existing[ref].strip()
        if current and not current.startswith(MARKER):
            raise PatchError(
                f"{mapping.sheet}!{ref} already holds text a human wrote; not overwriting"
            )
        if current:
            ours.append(ref)
    return ours


def _mapped_rows(mapping: MopMapping, results: Sequence[TestResult]):
    by_test: dict[str, list[TestResult]] = {}
    for r in results:
        by_test.setdefault(r.test_id, []).append(r)
    for rm in mapping.rows:
        present = [t for t in rm.test_ids if t in by_test]
        if present:
            yield rm, present, by_test
