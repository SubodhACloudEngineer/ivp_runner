from pathlib import Path

import openpyxl
import pytest

from ivp_runner.mop_writer import load_mapping, render_cells, write_results
from ivp_runner.results import (
    CheckResult,
    DeviceRef,
    DeviceResult,
    Reason,
    RunResult,
    SiteRef,
    Timestamp,
    Verdict,
)
from ivp_runner.xlsx_patch import PatchError, read_cell_texts

MAPPING = Path(__file__).resolve().parent.parent / "catalogue" / "mop_mapping.yaml"
DESCRIPTIONS = {
    57: "Go to Access-points and select the corresponding site",
    58: "Check the WLANs box to confirm the SSIDs",
    59: "Check the Power mode:",
    60: "Check the status to confirm the proper configuration",
    61: "Make sure the Ethernet properties are correct",
}


def dev(n):
    return DeviceRef(id=f"d{n}", name=f"AP-TEST-{n}", mac=f"{n:012x}", model="TEST")


def check(test_id, *verdicts, coverage="full", limitation=None):
    devices = tuple(
        DeviceResult(dev(i), v, None if v is Verdict.PASS else _reason(v))
        for i, v in enumerate(verdicts)
    )
    return CheckResult(test_id, f"title {test_id}", coverage, limitation, devices)


def _reason(v):
    return {
        Verdict.FAIL: Reason.CRITERIA_NOT_MET,
        Verdict.SKIP: Reason.PRECONDITION_FAILED,
        Verdict.INCONCLUSIVE: Reason.COUNTER_RESET,
        Verdict.ERROR: Reason.EXPECTATION_MISSING,
    }[v]


@pytest.fixture
def run():
    ts = Timestamp.from_epoch(1_790_000_000, "Europe/Madrid")
    P, F, S, INC, E = (
        Verdict.PASS,
        Verdict.FAIL,
        Verdict.SKIP,
        Verdict.INCONCLUSIVE,
        Verdict.ERROR,
    )
    return RunResult(
        run_id="run-1",
        tool_version="0.1.0",
        org_id="o",
        api_host="api.example",
        site=SiteRef("s", "Site", "Europe/Madrid"),
        catalogue_path="catalogue/ap.yaml",
        catalogue_sha256="0" * 64,
        started_at=ts,
        finished_at=ts,
        checks=(
            check("AP-00", P, F),
            check("AP-01", F, S, coverage="partial", limitation="Counts only. More text."),
            check("AP-02", P, S),
            check("AP-03", E, S),
            check("AP-04", P, S),
            check("AP-05", INC, S),
        ),
    )


@pytest.fixture
def template(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "IVP Test Plan"
    for r, text in DESCRIPTIONS.items():
        ws[f"B{r}"] = text
        ws[f"D{r}"] = "Not Started"
        ws[f"H{r}"] = None
        ws[f"Z{r}"] = "pad"  # makes row exist with cells after H
    path = tmp_path / "MOP.xlsx"
    wb.save(path)
    return path


def test_shipped_mapping_loads_and_covers_ap_checks():
    m = load_mapping(MAPPING)
    mapped = [t for r in m.rows for t in r.test_ids]
    assert mapped == ["AP-00", "AP-01", "AP-02", "AP-03", "AP-04", "AP-05"]
    assert set(m.verdict_to_status.values()) <= set(m.allowed_statuses)


def test_status_per_row_uses_rollup(run):
    cells = render_cells(load_mapping(MAPPING), run)
    assert cells["D57"] == "Issue Reported"  # AP-00 has a FAIL
    assert cells["D58"] == "Issue Reported"  # AP-01 FAIL
    assert cells["D59"] == "Complete"  # PASS + SKIP rolls up to PASS
    assert cells["D60"] == "Not Started"  # ERROR: a human must do it
    assert cells["D61"] == "Issue Reported"  # AP-04 PASS + AP-05 INCONCLUSIVE


def test_summary_names_failures_and_partial_limitation(run):
    h58 = render_cells(load_mapping(MAPPING), run)["H58"]
    assert h58.startswith("ivp-runner 2026-09-21T")
    assert "Europe/Madrid" in h58
    assert "FAIL (criteria_not_met): AP-TEST-0" in h58
    assert "Partial check: Counts only." in h58
    h61 = render_cells(load_mapping(MAPPING), run)["H61"]
    assert "AP-04 PASS" in h61 and "AP-05 INCONCLUSIVE" in h61


def test_write_results_end_to_end(run, template, tmp_path):
    dst = tmp_path / "out.xlsx"
    cells = write_results(load_mapping(MAPPING), run, template, dst)
    assert read_cell_texts(dst, "IVP Test Plan", list(cells)) == cells
    assert read_cell_texts(template, "IVP Test Plan", ["D57"]) == {"D57": "Not Started"}


def test_rewrite_over_own_output_is_allowed(run, template, tmp_path):
    first, second = tmp_path / "1.xlsx", tmp_path / "2.xlsx"
    write_results(load_mapping(MAPPING), run, template, first)
    write_results(load_mapping(MAPPING), run, first, second)


def test_refuses_when_template_rows_moved(run, template, tmp_path):
    wb = openpyxl.load_workbook(template)
    wb["IVP Test Plan"]["B59"] = "Something else entirely"
    wb.save(template)
    with pytest.raises(PatchError, match="B59 does not start with"):
        write_results(load_mapping(MAPPING), run, template, tmp_path / "o.xlsx")


def test_refuses_to_overwrite_human_evidence(run, template, tmp_path):
    wb = openpyxl.load_workbook(template)
    wb["IVP Test Plan"]["H60"] = "engineer notes"
    wb.save(template)
    with pytest.raises(PatchError, match="H60 already holds text a human wrote"):
        write_results(load_mapping(MAPPING), run, template, tmp_path / "o.xlsx")
