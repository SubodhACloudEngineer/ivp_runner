import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ivp_runner import schemas
from ivp_runner.results import Reason, TestResult, Verdict, rollup, timestamps


def record(**overrides):
    base = {
        "run_id": "run-1",
        "test_id": "AP-02",
        "site": {"id": "s", "name": "Site", "class": "large"},
        "device": {"id": "d1", "name": "AP-TEST-1"},
        "timestamp_utc": "2026-07-01T10:00:00Z",
        "timestamp_local": "2026-07-01T12:00:00+02:00",
        "timezone": "Europe/Madrid",
        "method": "mist_api",
        "expected": {"power_constrained": False},
        "actual": {"power_constrained": False},
        "verdict": "PASS",
        "evidence_path": "out/run-1/evidence/AP-02/d1.json",
        "raw_ref": [{"path": "out/run-1/raw/x.json", "sha256": "a" * 64, "pointer": "/0"}],
    }
    base.update(overrides)
    return base


def test_verdicts_are_exactly_four():
    assert {v.value for v in Verdict} == {"PASS", "FAIL", "SKIP", "ERROR"}
    with pytest.raises(ValidationError):
        TestResult.model_validate(record(verdict="INCONCLUSIVE", reason="counter_reset"))


@pytest.mark.parametrize(
    "verdict, reason, ok",
    [
        ("PASS", None, True),
        ("PASS", "criteria_not_met", False),
        ("FAIL", None, False),
        ("FAIL", "criteria_not_met", True),
        ("FAIL", "field_missing", False),  # a missing field is never a FAIL
        ("SKIP", "precondition_failed", True),
        ("ERROR", "counter_reset", True),
        ("ERROR", "criteria_not_met", False),  # ERROR is never a disguised FAIL
    ],
)
def test_reason_must_match_verdict(verdict, reason, ok):
    data = record(verdict=verdict, reason=reason)
    if ok:
        TestResult.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            TestResult.model_validate(data)


def test_device_may_be_null_only_for_site_wide_error():
    TestResult.model_validate(record(device=None, verdict="ERROR", reason="api_error"))
    with pytest.raises(ValidationError, match="site-wide ERROR"):
        TestResult.model_validate(record(device=None))


def test_unknown_keys_and_bad_formats_rejected():
    for bad in (
        record(extra=1),
        record(timestamp_utc="2026-07-01 10:00"),
        record(method="snmp"),
        record(raw_ref=[{"path": "x", "sha256": "nothex", "pointer": "/0"}]),
    ):
        with pytest.raises(ValidationError):
            TestResult.model_validate(bad)


def test_json_round_trip_uses_class_alias():
    r = TestResult.model_validate(record())
    dumped = json.loads(r.model_dump_json(by_alias=True))
    assert dumped["site"]["class"] == "large"
    assert TestResult.model_validate(dumped) == r


def test_timestamps_utc_and_site_local():
    utc, local = timestamps(datetime(2026, 1, 15, 9, 30, tzinfo=UTC), "Europe/Madrid")
    assert (utc, local) == ("2026-01-15T09:30:00Z", "2026-01-15T10:30:00+01:00")
    with pytest.raises(ValueError, match="naive"):
        timestamps(datetime(2026, 1, 15, 9, 30), "Europe/Madrid")


@pytest.mark.parametrize(
    "verdicts, expected",
    [
        (["PASS", "SKIP"], "PASS"),
        (["SKIP"], "SKIP"),
        (["PASS", "ERROR"], "ERROR"),
        (["ERROR", "FAIL"], "FAIL"),
        ([], "ERROR"),
    ],
)
def test_rollup(verdicts, expected):
    assert rollup(Verdict(v) for v in verdicts) is Verdict(expected)


@pytest.mark.parametrize("name", sorted(schemas.MODELS))
def test_committed_json_schemas_match_models(name):
    path = schemas.SCHEMA_DIR / f"{name}.schema.json"
    assert path.read_text(encoding="utf-8") == schemas.render(schemas.MODELS[name]), (
        f"{path} is stale: run `python -m ivp_runner.schemas`"
    )


def test_result_schema_lists_exactly_four_verdicts():
    schema = json.loads((schemas.SCHEMA_DIR / "result.schema.json").read_text())
    assert sorted(schema["$defs"]["Verdict"]["enum"]) == ["ERROR", "FAIL", "PASS", "SKIP"]
    assert set(schema["$defs"]["Reason"]["enum"]) == {r.value for r in Reason}
