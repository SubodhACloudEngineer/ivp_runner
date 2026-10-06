import json
from datetime import UTC, datetime

import pytest

from ivp_runner.results import (
    CheckResult,
    DeviceRef,
    DeviceResult,
    Reason,
    RunResult,
    SiteRef,
    Timestamp,
    Verdict,
    rollup,
)

DEV = DeviceRef(id="d1", name="AP-TEST-1", mac="000000000001", model="TEST")
DEV2 = DeviceRef(id="d2", name="AP-TEST-2", mac="000000000002", model="TEST")


def ok(dev=DEV):
    return DeviceResult(dev, Verdict.PASS)


def bad(verdict, reason, dev=DEV):
    return DeviceResult(dev, verdict, reason)


def test_timestamp_keeps_utc_and_site_local():
    ts = Timestamp.from_datetime(datetime(2026, 7, 1, 10, 0, tzinfo=UTC), "Europe/Madrid")
    assert ts.utc == "2026-07-01T10:00:00Z"
    assert ts.local == "2026-07-01T12:00:00+02:00"
    assert ts.tz == "Europe/Madrid"


def test_timestamp_rejects_naive_datetime():
    with pytest.raises(ValueError, match="naive"):
        Timestamp.from_datetime(datetime(2026, 7, 1, 10, 0), "Europe/Madrid")


def test_pass_has_no_reason_and_others_require_one():
    with pytest.raises(ValueError):
        DeviceResult(DEV, Verdict.PASS, Reason.CRITERIA_NOT_MET)
    with pytest.raises(ValueError):
        DeviceResult(DEV, Verdict.SKIP)


@pytest.mark.parametrize(
    "verdicts, expected",
    [
        ([Verdict.PASS, Verdict.PASS], Verdict.PASS),
        ([Verdict.PASS, Verdict.SKIP], Verdict.PASS),
        ([Verdict.SKIP, Verdict.SKIP], Verdict.SKIP),
        ([Verdict.PASS, Verdict.INCONCLUSIVE], Verdict.INCONCLUSIVE),
        ([Verdict.ERROR, Verdict.INCONCLUSIVE], Verdict.ERROR),
        ([Verdict.ERROR, Verdict.FAIL, Verdict.PASS], Verdict.FAIL),
        ([], Verdict.ERROR),
    ],
)
def test_rollup_precedence(verdicts, expected):
    assert rollup(verdicts) is expected


def test_disconnected_ap_shape_one_fail_four_skips():
    """A device failing AP-00 appears as FAIL there and SKIP in AP-01..05."""
    checks = [
        CheckResult(
            "AP-00", "t", "full", None, (ok(DEV), bad(Verdict.FAIL, Reason.CRITERIA_NOT_MET, DEV2))
        ),
    ] + [
        CheckResult(
            t, "t", "full", None, (ok(DEV), bad(Verdict.SKIP, Reason.PRECONDITION_FAILED, DEV2))
        )
        for t in ("AP-01", "AP-02", "AP-03", "AP-04", "AP-05")
    ]
    dev2 = [d.verdict for c in checks for d in c.devices if d.device is DEV2]
    assert dev2.count(Verdict.FAIL) == 1 and dev2.count(Verdict.SKIP) == 5
    assert checks[0].verdict is Verdict.FAIL
    assert checks[1].verdict is Verdict.PASS and checks[1].counts["SKIP"] == 1


def test_run_result_serialises_to_json():
    ts = Timestamp.from_epoch(0, "Europe/Madrid")
    run = RunResult(
        run_id="r1",
        tool_version="0.1.0",
        org_id="o",
        api_host="api.example",
        site=SiteRef("s", "Site", "Europe/Madrid"),
        catalogue_path="catalogue/ap.yaml",
        catalogue_sha256="0" * 64,
        started_at=ts,
        finished_at=ts,
        checks=(
            CheckResult(
                "AP-05",
                "t",
                "partial",
                "RX only",
                (
                    DeviceResult(
                        DEV,
                        Verdict.INCONCLUSIVE,
                        Reason.COUNTER_RESET,
                        evidence={"port_stat.eth0.rx_errors": {"start": 9, "end": 2}},
                    ),
                ),
            ),
        ),
    )
    out = json.loads(json.dumps(run.to_dict()))
    check = out["checks"][0]
    assert check["verdict"] == "INCONCLUSIVE"
    assert check["devices"][0]["reason"] == "counter_reset"
    assert out["started_at"]["utc"] == "1970-01-01T00:00:00Z"
