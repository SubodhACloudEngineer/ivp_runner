"""Result contract: one record per executed (test, device).

Every timestamp is stored in UTC, with the site-local rendering alongside it.
``expected`` and ``actual`` are keyed by literal field path, so each value
traces back to the raw payload that ``raw_ref`` points at.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

RESULT_SCHEMA_VERSION = 1
Method = Literal["mist_api", "ssh", "probe"]


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    # Not evaluated because a precondition check did not pass for this device.
    SKIP = "SKIP"
    # The check could not be executed or its data cannot support a verdict.
    # Never a disguised FAIL.
    ERROR = "ERROR"


class Reason(StrEnum):
    """Machine-readable cause, required for every verdict except PASS."""

    CRITERIA_NOT_MET = "criteria_not_met"  # FAIL
    PRECONDITION_FAILED = "precondition_failed"  # SKIP
    COUNTER_RESET = "counter_reset"  # ERROR: device rebooted mid-run
    SAMPLE_MISSING = "sample_missing"  # ERROR: device absent from run-end sample
    FIELD_MISSING = "field_missing"  # ERROR: required field not reported
    EXPECTATION_MISSING = "expectation_missing"  # ERROR: site profile lacks a value
    BAD_DATA = "bad_data"  # ERROR: field has an unexpected type/format
    METHOD_UNAVAILABLE = "method_unavailable"  # ERROR: backend not implemented
    API_ERROR = "api_error"  # ERROR: collection failed


_REASONS_BY_VERDICT = {
    Verdict.FAIL: {Reason.CRITERIA_NOT_MET},
    Verdict.SKIP: {Reason.PRECONDITION_FAILED},
    Verdict.ERROR: set(Reason) - {Reason.CRITERIA_NOT_MET, Reason.PRECONDITION_FAILED},
}

# Rollup precedence, highest first. A confirmed FAIL is the most actionable
# outcome; SKIP only wins if nothing else happened.
ROLLUP_ORDER = (Verdict.FAIL, Verdict.ERROR, Verdict.PASS, Verdict.SKIP)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SiteRef(_Strict):
    id: str
    name: str
    site_class: Literal["small", "medium", "large"] = Field(alias="class")


class DeviceRef(_Strict):
    id: str
    name: str | None = None
    mac: str | None = None
    model: str | None = None


class RawRef(_Strict):
    """Where the device's raw data lives: a saved payload file plus a JSON pointer into it."""

    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pointer: str = Field(pattern=r"^(/[^/]*)*$")  # RFC 6901
    sample: Literal["once", "start", "end"] = "once"


class AssertionResult(_Strict):
    field: str
    rule: str  # the describe() sentence the reviewer approved
    expected: Any = None
    actual: Any = None
    verdict: Literal[Verdict.PASS, Verdict.FAIL, Verdict.ERROR]
    reason: Reason | None = None
    message: str = ""


class TestResult(_Strict):
    __test__ = False  # not a pytest test class

    schema_version: Literal[1] = RESULT_SCHEMA_VERSION
    run_id: str
    test_id: str = Field(pattern=r"^[A-Z]{2,}-\d{2}$")
    site: SiteRef
    # Null only for an ERROR that applies to the whole site (e.g. the API call failed).
    device: DeviceRef | None
    timestamp_utc: str = Field(pattern=r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
    timestamp_local: str
    timezone: str
    method: Method
    expected: dict[str, Any] = Field(default_factory=dict)
    actual: dict[str, Any] = Field(default_factory=dict)
    verdict: Verdict
    reason: Reason | None = None
    message: str = ""
    assertions: list[AssertionResult] = Field(default_factory=list)
    evidence_path: str | None = None
    raw_ref: list[RawRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> TestResult:
        if self.verdict is Verdict.PASS:
            if self.reason is not None:
                raise ValueError("PASS carries no reason")
        elif self.reason not in _REASONS_BY_VERDICT[self.verdict]:
            allowed = sorted(r.value for r in _REASONS_BY_VERDICT[self.verdict])
            raise ValueError(f"{self.verdict.value} requires a reason from {allowed}")
        if self.device is None and self.verdict is not Verdict.ERROR:
            raise ValueError("device may only be null for a site-wide ERROR")
        return self


def timestamps(dt: datetime, tz: str) -> tuple[str, str]:
    """(UTC ISO with Z, site-local ISO with offset) for an aware datetime."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime: timestamps must be timezone-aware")
    utc = dt.astimezone(UTC)
    return (
        utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
        utc.astimezone(ZoneInfo(tz)).isoformat(timespec="seconds"),
    )


def rollup(verdicts: Iterable[Verdict]) -> Verdict:
    """Collapse several verdicts into one. An empty input gives ERROR."""
    seen = set(verdicts)
    if not seen:
        return Verdict.ERROR
    return next(v for v in ROLLUP_ORDER if v in seen)
