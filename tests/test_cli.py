"""End-to-end `ivp run` against a fake Mist API. No network."""

import json
import re
import zipfile
from pathlib import Path

import openpyxl
import pytest
import requests
import yaml

from ivp_runner.cli import main
from ivp_runner.results import TestResult
from ivp_runner.xlsx_patch import read_cell_texts

from .factories import ap, disconnected
from .fixtures_mist import FIXTURES, site_doc, stats_items

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "cli-test-token-0123456789abcdef"
SITE1 = "00000000-0000-4000-8000-0000000000a1"
SITE2 = "00000000-0000-4000-8000-0000000000a2"
ORG = "00000000-0000-4000-8000-0000000000f1"
FIXTURE_SITE = yaml.safe_load((FIXTURES / "site_profile.yaml").read_text())["site_id"]
DESCRIPTIONS = {
    57: "Go to Access-points and select the corresponding site",
    58: "Check the WLANs box to confirm the SSIDs that are being broadcasted",
    59: "Check the Power mode:",
    60: "Check the status to confirm the proper configuration is in place",
    61: "Make sure the Ethernet properties are correct, Full duplex and no errors are shown.",
}


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self.content = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.headers = headers or {}


class FakeMist:
    """Serves AP stats and site details for one site; records calls."""

    def __init__(self, site_id, items, site=None, status=200):
        self.site_id, self.items, self.status = site_id, items, status
        self.site = site or {**site_doc(), "id": site_id}
        self.calls = []

    def get(self, url, *, headers, params, timeout):
        self.calls.append(url)
        if self.status != 200:
            return Resp(self.status, {"detail": "nope"})
        if url.endswith(f"/sites/{self.site_id}"):
            return Resp(200, self.site)
        if url.endswith(f"/sites/{self.site_id}/stats/devices"):
            hdr = {"X-Page-Total": str(len(self.items)), "X-Page-Page": params["page"]}
            return Resp(200, self.items, hdr)
        return Resp(404, {})


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("MIST_API_TOKEN", TOKEN)


@pytest.fixture
def mop(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "IVP Test Plan"
    for r, text in DESCRIPTIONS.items():
        ws[f"A{r}"] = 1  # numbering in the real MOP repeats; we never use it
        ws[f"B{r}"] = text
        ws[f"D{r}"] = "Not Started"
        ws[f"Z{r}"] = "pad"
    wb.create_sheet("Cover Sheet")["A1"] = "keep me"
    path = tmp_path / "MOP_template.xlsx"
    wb.save(path)
    return path


def profile_file(tmp_path, site_id, **exp):
    base = {
        "uplink_port": "eth0",
        "ssids": [{"ssid": "A", "bands": ["24", "5"]}, {"ssid": "B", "bands": ["5", "6"]}],
        "mgmt_subnet": "192.0.2.0/24",
        "dns_servers": ["192.0.2.53", "198.51.100.53"],
        "min_uplink_speed_mbps": 1000,
    }
    base.update(exp)
    doc = {"schema_version": 1, "site_id": site_id, "site_class": "large", "expectations": base}
    path = tmp_path / f"{site_id}.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def argv(site, mop, out, profile=None, *extra):
    a = [
        "run", "--site", site, "--org", ORG, "--api-host", "api.example",
        "--catalogue", str(ROOT / "catalogue" / "ap.yaml"),
        "--out", str(out),
        "--mapping", str(ROOT / "catalogue" / "mop_mapping.yaml"),
    ]  # fmt: skip
    if mop is not None:
        a += ["--mop", str(mop)]
    if profile:
        a += ["--profile", str(profile)]
    return a + list(extra)


def run_cli(args, fake, capsys):
    code = main(args, _session=fake, _sleep=lambda s: None)
    out = capsys.readouterr()
    return code, out.out, out.err


def only_run_dir(out: Path) -> Path:
    (d,) = [p for p in out.iterdir() if p.is_dir()]
    return d


# ---------------------------------------------------------------- full run on captured data


def test_full_run_on_captured_fixtures(tmp_path, mop, capsys):
    out = tmp_path / "out"
    fake = FakeMist(FIXTURE_SITE, stats_items())
    code, stdout, stderr = run_cli(
        argv(FIXTURE_SITE, mop, out, FIXTURES / "site_profile.yaml"), fake, capsys
    )
    assert code == 1  # captured site has FAILs and no ERROR

    run_dir = only_run_dir(out)
    assert re.fullmatch(r"\d{8}T\d{6}Z-" + FIXTURE_SITE[:8], run_dir.name)
    doc = json.loads((run_dir / "results.json").read_text())
    assert doc["run"]["exit_code"] == 1 and doc["run"]["site_id"] == FIXTURE_SITE
    assert len(doc["results"]) == 6 * 91
    assert all(TestResult.model_validate(r) for r in doc["results"])

    assert (run_dir / "raw" / "manifest.json").is_file()
    assert (run_dir / "raw" / "site_device_stats.start.json").is_file()
    assert (run_dir / "raw" / "site_device_stats.end.json").is_file()
    assert len(list((run_dir / "evidence").rglob("*.png"))) == 6 * 91
    assert "exit code 1" in (run_dir / "run.log").read_text()

    (book,) = run_dir.glob("MOP_*.xlsx")
    cells = read_cell_texts(book, "IVP Test Plan", [f"D{r}" for r in DESCRIPTIONS])
    assert cells == {
        "D57": "Issue Reported",  # one AP disconnected
        "D58": "Issue Reported",  # AP-01 FAILs
        "D59": "Issue Reported",  # one power-constrained AP
        "D60": "Complete",
        "D61": "Issue Reported",  # two APs at 100 Mbps
    }
    assert read_cell_texts(mop, "IVP Test Plan", ["D57"]) == {"D57": "Not Started"}
    with zipfile.ZipFile(book) as z:
        cards = [n for n in z.namelist() if n.startswith("xl/media/ivp_runner_")]
    assert len(cards) == 5  # one row card per mapped row, anchored in H57..H61
    assert "5 evidence pictures" in (run_dir / "run.log").read_text()

    # 1 site request + 1 stats page at start, 1 stats page at end
    assert len(fake.calls) == 3
    assert "TEST ID" in stdout and "AP-05" in stdout and f"workbook:   {book}" in stdout
    assert "TOTAL:" in stdout and "exit code:  1" in stdout
    assert "ivp: " in stderr  # progress goes to stderr


def test_json_mode_emits_only_results_json(tmp_path, mop, capsys):
    prof = profile_file(tmp_path, SITE1)
    items = [ap(1), ap(2, power_constrained=True)]
    code, stdout, stderr = run_cli(
        argv(SITE1, mop, tmp_path / "out", prof, "--json"),
        FakeMist(SITE1, items),
        capsys,
    )
    doc = json.loads(stdout)  # nothing but JSON on stdout
    assert code == doc["run"]["exit_code"] == 1
    assert "TEST ID" not in stdout and "ivp: " in stderr
    assert len(doc["results"]) == 12
    assert doc["run"]["counts"] == {"PASS": 11, "FAIL": 1, "SKIP": 0, "ERROR": 0}
    assert doc["run"]["mop"]["output"].endswith(".xlsx")


# ---------------------------------------------------------------- exit codes


def test_exit_0_when_everything_passes(tmp_path, mop, capsys):
    prof = profile_file(tmp_path, SITE1)
    code, stdout, _ = run_cli(
        argv(SITE1, mop, tmp_path / "o", prof),
        FakeMist(SITE1, [ap(1)]),
        capsys,
    )
    assert code == 0 and "6 PASS" in stdout


def test_exit_1_on_fail(tmp_path, mop, capsys):
    prof = profile_file(tmp_path, SITE1)
    items = [ap(1, power_constrained=True)]
    assert (
        run_cli(
            argv(SITE1, mop, tmp_path / "o", prof),
            FakeMist(SITE1, items),
            capsys,
        )[0]
        == 1
    )


def test_exit_2_when_error_even_with_fail(tmp_path, mop, capsys):
    prof = profile_file(tmp_path, SITE1, mgmt_subnet=None)  # AP-03 cannot be judged
    items = [ap(1, power_constrained=True), disconnected(9)]  # and there are FAILs
    code, stdout, _ = run_cli(
        argv(SITE1, mop, tmp_path / "o", prof),
        FakeMist(SITE1, items),
        capsys,
    )
    assert code == 2
    assert "expectation_missing" in stdout and "criteria_not_met" in stdout


def test_exit_3_missing_token(tmp_path, mop, capsys, monkeypatch):
    monkeypatch.delenv("MIST_API_TOKEN")
    prof = profile_file(tmp_path, SITE1)
    fake = FakeMist(SITE1, [ap(1)])
    code, stdout, _ = run_cli(argv(SITE1, mop, tmp_path / "o", prof), fake, capsys)
    assert code == 3 and "MIST_API_TOKEN is not set" in stdout
    assert fake.calls == []
    doc = json.loads((only_run_dir(tmp_path / "o") / "results.json").read_text())
    assert doc["run"]["exit_code"] == 3 and "MIST_API_TOKEN" in doc["run"]["tool_error"]


def test_exit_3_missing_profile_points_to_template(tmp_path, mop, capsys):
    code, stdout, _ = run_cli(
        argv(SITE1, mop, tmp_path / "o", tmp_path / "nope.yaml"),
        FakeMist(SITE1, []),
        capsys,
    )
    assert code == 3 and "sites/TEMPLATE.yaml" in stdout


def test_exit_3_profile_for_another_site(tmp_path, mop, capsys):
    prof = profile_file(tmp_path, SITE2)
    code, stdout, _ = run_cli(
        argv(SITE1, mop, tmp_path / "o", prof),
        FakeMist(SITE1, []),
        capsys,
    )
    assert code == 3 and f"is for site {SITE2}" in stdout


def test_exit_3_on_bad_arguments(capsys):
    with pytest.raises(SystemExit) as e:
        main(["run", "--site", "x"])
    assert e.value.code == 3


def test_api_host_is_required(capsys):
    with pytest.raises(SystemExit) as e:
        main(["run", "--site", "s", "--org", "o", "--catalogue", "c", "--mop", "m", "--out", "o"])
    assert e.value.code == 3
    assert "--api-host" in capsys.readouterr().err


# ---------------------------------------------------------------- safety


def test_token_never_written_or_printed(tmp_path, mop, capsys):
    prof = profile_file(tmp_path, SITE1)
    items = [ap(1, echo=TOKEN)]  # even if the API echoes it
    code, stdout, stderr = run_cli(
        argv(SITE1, mop, tmp_path / "o", prof),
        FakeMist(SITE1, items),
        capsys,
    )
    assert TOKEN not in stdout + stderr
    for f in (tmp_path / "o").rglob("*"):
        if f.is_file() and f.suffix in {".json", ".log", ".yaml"}:
            assert TOKEN not in f.read_text(encoding="utf-8"), f


def test_no_interactive_prompts_in_source():
    for f in (ROOT / "src").rglob("*.py"):
        assert "input(" not in f.read_text(encoding="utf-8"), f


def test_template_profile_is_valid_once_site_id_filled(tmp_path):
    from ivp_runner.site_profile import SiteProfile

    doc = yaml.safe_load((ROOT / "sites" / "TEMPLATE.yaml").read_text())
    doc["site_id"] = "abc"
    p = SiteProfile.model_validate(doc)
    assert p.expectations.mgmt_subnet is None and p.expectations.uplink_port == "eth0"


# ---------------------------------------------------------------- failure modes
#
# Each must print a problem and what to do, exit 3, and never show a traceback.


def assert_legible_failure(code, stdout, stderr, problem, fix):
    assert code == 3
    assert "RUN FAILED" in stdout and problem in stdout and fix in stdout, stdout
    assert "Traceback" not in stdout + stderr


class EndSampleFails(FakeMist):
    """Serves the start sample, then a 500 for every later stats request."""

    def get(self, url, *, headers, params, timeout):
        if url.endswith("/stats/devices") and any("stats" in c for c in self.calls):
            self.calls.append(url)
            return Resp(500, {"detail": "boom"})
        return super().get(url, headers=headers, params=params, timeout=timeout)


class Unreachable:
    def __init__(self, exc):
        self.exc, self.calls = exc, []

    def get(self, url, **kw):
        self.calls.append(url)
        raise self.exc


def test_bad_token_stops_after_one_request(tmp_path, mop, capsys):
    fake = FakeMist(SITE1, [ap(1)], status=401)
    code, stdout, stderr = run_cli(
        argv(SITE1, mop, tmp_path / "o", profile_file(tmp_path, SITE1)), fake, capsys
    )
    assert_legible_failure(
        code, stdout, stderr, "Mist rejected the API token (HTTP 401", "export MIST_API_TOKEN"
    )
    assert len(fake.calls) == 1  # not one 401 per source, device and check
    doc = json.loads((only_run_dir(tmp_path / "o") / "results.json").read_text())
    assert doc["results"] == [] and "different Mist cloud" in doc["run"]["tool_fix"]
    assert "api_error" not in stdout


def test_wrong_site_id(tmp_path, mop, capsys):
    fake = FakeMist(SITE2, [ap(1)])  # the API knows another site only: 404
    code, stdout, stderr = run_cli(
        argv(SITE1, mop, tmp_path / "o", profile_file(tmp_path, SITE1)), fake, capsys
    )
    assert_legible_failure(
        code, stdout, stderr, f"site {SITE1} was not found on api.example", "Check --site"
    )


def test_site_name_instead_of_id_is_caught_before_any_request(tmp_path, mop, capsys):
    fake = FakeMist(SITE1, [ap(1)])
    code, stdout, stderr = run_cli(
        argv("Branch-Office", mop, tmp_path / "o", profile_file(tmp_path, SITE1)),
        fake,
        capsys,
    )
    assert_legible_failure(code, stdout, stderr, "is not a Mist ID", "UUIDs like")
    assert fake.calls == []


@pytest.mark.parametrize(
    "exc, problem, fix",
    [
        (
            requests.ConnectionError("NameResolutionError: Failed to resolve 'api.example'"),
            "cannot resolve api.example",
            "nslookup api.example",
        ),
        (requests.ConnectTimeout("timed out"), "no usable response from api.example", "curl -sI"),
        (requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED"), "TLS", "REQUESTS_CA_BUNDLE"),
    ],
    ids=["dns", "timeout", "tls"],
)
def test_unreachable_api(tmp_path, mop, capsys, exc, problem, fix):
    code, stdout, stderr = run_cli(
        argv(SITE1, mop, tmp_path / "o", profile_file(tmp_path, SITE1)), Unreachable(exc), capsys
    )
    assert_legible_failure(code, stdout, stderr, problem, fix)


def test_missing_workbook_is_caught_before_any_request(tmp_path, capsys):
    fake = FakeMist(SITE1, [ap(1)])
    code, stdout, stderr = run_cli(
        argv(SITE1, tmp_path / "MOP.xlsx", tmp_path / "o", profile_file(tmp_path, SITE1)),
        fake,
        capsys,
    )
    assert_legible_failure(code, stdout, stderr, "MOP workbook not found", "Point --mop at")
    assert fake.calls == []


def test_workbook_that_is_not_xlsx(tmp_path, capsys):
    bad = tmp_path / "MOP.xlsx"
    bad.write_text("Test,Status\n", encoding="utf-8")
    code, stdout, stderr = run_cli(
        argv(SITE1, bad, tmp_path / "o", profile_file(tmp_path, SITE1)),
        FakeMist(SITE1, [ap(1)]),
        capsys,
    )
    assert_legible_failure(code, stdout, stderr, "is not an .xlsx workbook", ".xls or .csv")


def test_mop_template_mismatch_is_caught_before_any_request(tmp_path, mop, capsys):
    wb = openpyxl.load_workbook(mop)
    wb["IVP Test Plan"]["B59"] = "A different MOP version"
    wb.save(mop)
    fake = FakeMist(SITE1, [ap(1)])
    code, stdout, stderr = run_cli(
        argv(SITE1, mop, tmp_path / "o", profile_file(tmp_path, SITE1)), fake, capsys
    )
    assert_legible_failure(
        code, stdout, stderr, "B59 does not start with", "catalogue/mop_mapping.yaml"
    )
    assert fake.calls == []
    assert list(only_run_dir(tmp_path / "o").glob("MOP_*.xlsx")) == []


def test_partial_collection_failure_is_explained_once(tmp_path, mop, capsys):
    """The end sample fails: only AP-05 needs it, so the run completes with exit 2."""
    code, stdout, stderr = run_cli(
        argv(SITE1, mop, tmp_path / "o", profile_file(tmp_path, SITE1)),
        EndSampleFails(SITE1, [ap(1)]),
        capsys,
    )
    assert code == 2 and "RUN FAILED" not in stdout
    assert stdout.count("server error 500") == 1 and "Mist status page" in stdout
    doc = json.loads((only_run_dir(tmp_path / "o") / "results.json").read_text())
    assert [p["problem"] for p in doc["run"]["collection_problems"]] == [
        "end site_device_stats: GET /api/v1/sites/"
        f"{SITE1}/stats/devices: server error 500 after 6 attempts"
    ]


def test_internal_error_shows_no_traceback_but_logs_it(tmp_path, mop, capsys, monkeypatch):
    import ivp_runner.runner as runner

    def boom(*a, **k):
        raise ZeroDivisionError("simulated bug")

    monkeypatch.setattr(runner, "write_cards", boom)
    code, stdout, stderr = run_cli(
        argv(SITE1, mop, tmp_path / "o", profile_file(tmp_path, SITE1)),
        FakeMist(SITE1, [ap(1)]),
        capsys,
    )
    assert_legible_failure(code, stdout, stderr, "internal error: ZeroDivisionError", "bug")
    log = (only_run_dir(tmp_path / "o") / "run.log").read_text()
    assert "Traceback" in log and "simulated bug" in log


def test_unwritable_out_folder(tmp_path, mop, capsys):
    blocker = tmp_path / "out"
    blocker.write_text("a file, not a folder")
    code, stdout, stderr = run_cli(
        argv(SITE1, mop, blocker, profile_file(tmp_path, SITE1)), FakeMist(SITE1, []), capsys
    )
    assert_legible_failure(code, stdout, stderr, "cannot create a run folder", "--out")


# ---------------------------------------------------------------- dry run


def test_dry_run_evaluates_but_writes_no_workbook(tmp_path, mop, capsys):
    before = mop.read_bytes()
    code, stdout, _ = run_cli(
        argv(SITE1, mop, tmp_path / "o", profile_file(tmp_path, SITE1), "--dry-run"),
        FakeMist(SITE1, [ap(1), ap(2, power_constrained=True)]),
        capsys,
    )
    assert code == 1 and "dry run - not written" in stdout
    run_dir = only_run_dir(tmp_path / "o")
    assert list(run_dir.glob("*.xlsx")) == [] and mop.read_bytes() == before
    doc = json.loads((run_dir / "results.json").read_text())
    assert doc["run"]["dry_run"] is True and doc["run"]["mop"]["output"] is None
    assert len(doc["results"]) == 12 and len(list(run_dir.rglob("*.png"))) == 12
    assert "dry run: workbook not written" in (run_dir / "run.log").read_text()


def test_dry_run_needs_no_workbook(tmp_path, capsys):
    code, stdout, _ = run_cli(
        argv(SITE1, None, tmp_path / "o", profile_file(tmp_path, SITE1), "--dry-run"),
        FakeMist(SITE1, [ap(1)]),
        capsys,
    )
    assert code == 0 and "dry run - not written" in stdout


def test_dry_run_still_checks_a_given_workbook(tmp_path, capsys):
    code, stdout, stderr = run_cli(
        argv(
            SITE1,
            tmp_path / "nope.xlsx",
            tmp_path / "o",
            profile_file(tmp_path, SITE1),
            "--dry-run",
        ),
        FakeMist(SITE1, [ap(1)]),
        capsys,
    )
    assert_legible_failure(code, stdout, stderr, "MOP workbook not found", "--mop")


def test_mop_required_without_dry_run(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        main(argv(SITE1, None, tmp_path / "o"))
    assert e.value.code == 3 and "--dry-run" in capsys.readouterr().err
