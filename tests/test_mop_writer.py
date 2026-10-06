from pathlib import Path

import openpyxl
import pytest

from ivp_runner.assert_engine import evaluate
from ivp_runner.mop_writer import load_mapping, render_cells, write_results
from ivp_runner.results import Verdict
from ivp_runner.xlsx_patch import PatchError, read_cell_texts

from .factories import ap, catalogue, context, disconnected, payloads, profile

MAPPING = Path(__file__).resolve().parent.parent / "catalogue" / "mop_mapping.yaml"
DESCRIPTIONS = {
    57: "Go to Access-points and select the corresponding site",
    58: "Check the WLANs box to confirm the SSIDs",
    59: "Check the Power mode:",
    60: "Check the status to confirm the proper configuration",
    61: "Make sure the Ethernet properties are correct",
}


@pytest.fixture
def results():
    """AP 1 healthy; AP 2 power-constrained; AP 9 disconnected; AP-03 has no subnet."""
    start = [ap(1), ap(2, power_constrained=True), disconnected(9)]
    evs = evaluate(catalogue(), profile(mgmt_subnet=None), *payloads(start), context())
    return [e.result for e in evs]


@pytest.fixture
def template(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "IVP Test Plan"
    for r, text in DESCRIPTIONS.items():
        ws[f"B{r}"] = text
        ws[f"D{r}"] = "Not Started"
        ws[f"Z{r}"] = "pad"
    path = tmp_path / "MOP.xlsx"
    wb.save(path)
    return path


def test_shipped_mapping_loads_and_covers_ap_checks():
    m = load_mapping(MAPPING)
    assert [t for r in m.rows for t in r.test_ids] == [f"AP-0{i}" for i in range(6)]
    assert set(m.verdict_to_status) == set(Verdict)
    assert set(m.verdict_to_status.values()) <= set(m.allowed_statuses)


def test_status_per_row_uses_rollup(results):
    cells = render_cells(load_mapping(MAPPING), results, catalogue())
    assert cells["D57"] == "Issue Reported"  # AP-00: AP 9 disconnected
    assert cells["D58"] == "Complete"  # AP-01: PASS + SKIP
    assert cells["D59"] == "Issue Reported"  # AP-02: AP 2 constrained
    assert cells["D60"] == "Not Started"  # AP-03: ERROR, no subnet in profile
    assert cells["D61"] == "Complete"  # AP-04 + AP-05 both PASS (+ SKIP)


def test_summary_names_failures_and_partial_limitation(results):
    cells = render_cells(load_mapping(MAPPING), results, catalogue())
    assert cells["H59"].startswith("ivp-runner 2026-07-01T10:")
    assert "Europe/Madrid" in cells["H59"]
    assert "FAIL (criteria_not_met): AP-TEST-2" in cells["H59"]
    assert "[1 PASS, 1 FAIL, 1 SKIP]" in cells["H59"]
    assert "ERROR (expectation_missing): AP-TEST-1, AP-TEST-2" in cells["H60"]
    assert "Partial check:" in cells["H58"]
    assert "AP-04 PASS" in cells["H61"] and "AP-05 PASS" in cells["H61"]


def test_write_results_end_to_end(results, template, tmp_path):
    dst = tmp_path / "out.xlsx"
    cells = write_results(load_mapping(MAPPING), results, catalogue(), template, dst)
    assert read_cell_texts(dst, "IVP Test Plan", list(cells)) == cells
    assert read_cell_texts(template, "IVP Test Plan", ["D57"]) == {"D57": "Not Started"}


def test_rewrite_over_own_output_is_allowed(results, template, tmp_path):
    first, second = tmp_path / "1.xlsx", tmp_path / "2.xlsx"
    write_results(load_mapping(MAPPING), results, catalogue(), template, first)
    write_results(load_mapping(MAPPING), results, catalogue(), first, second)


def test_refuses_when_template_rows_moved(results, template, tmp_path):
    wb = openpyxl.load_workbook(template)
    wb["IVP Test Plan"]["B59"] = "Something else entirely"
    wb.save(template)
    with pytest.raises(PatchError, match="B59 does not start with"):
        write_results(load_mapping(MAPPING), results, catalogue(), template, tmp_path / "o.xlsx")


def test_refuses_to_overwrite_human_evidence(results, template, tmp_path):
    wb = openpyxl.load_workbook(template)
    wb["IVP Test Plan"]["H60"] = "engineer notes"
    wb.save(template)
    with pytest.raises(PatchError, match="H60 already holds text a human wrote"):
        write_results(load_mapping(MAPPING), results, catalogue(), template, tmp_path / "o.xlsx")
