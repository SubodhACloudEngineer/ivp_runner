"""Result schema: per-device verdicts, per-check rollups, and the run envelope.

Every timestamp is stored in UTC, with the site-local rendering alongside it.
Evidence keys are literal field paths from the Mist response, using the actual
port and band keys (e.g. ``port_stat.eth0.rx_errors``), so each value traces
back to the API.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any
from zoneinfo import ZoneInfo

RESULT_SCHEMA_VERSION = 1


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    # Not evaluated because a precondition check failed for this device.
    SKIP = "SKIP"
    # Evaluated, but the data cannot support a verdict (e.g. a counter reset).
    INCONCLUSIVE = "INCONCLUSIVE"
    # The runner could not evaluate: missing expectation, API error, bad data.
    ERROR = "ERROR"


class Reason(StrEnum):
    """Machine-readable cause for any verdict other than PASS."""

    CRITERIA_NOT_MET = "criteria_not_met"  # FAIL
    PRECONDITION_FAILED = "precondition_failed"  # SKIP
    COUNTER_RESET = "counter_reset"  # INCONCLUSIVE: AP rebooted mid-run
    END_SAMPLE_MISSING = "end_sample_missing"  # INCONCLUSIVE
    EXPECTATION_MISSING = "expectation_missing"  # ERROR: catalogue value is null
    FIELD_MISSING = "field_missing"  # ERROR: required field absent
    API_ERROR = "api_error"  # ERROR


# Rollup precedence, highest first. A confirmed FAIL outranks an ERROR elsewhere,
# because it is the most actionable outcome. SKIP only wins if every device skipped.
_ROLLUP_ORDER = (
    Verdict.FAIL,
    Verdict.ERROR,
    Verdict.INCONCLUSIVE,
    Verdict.PASS,
    Verdict.SKIP,
)


@dataclass(frozen=True)
class Timestamp:
    utc: str  # ISO 8601, "Z" suffix
    local: str  # ISO 8601 with site offset
    tz: str  # IANA zone used for `local`

    @classmethod
    def from_datetime(cls, dt: datetime, tz: str) -> Timestamp:
        if dt.tzinfo is None:
            raise ValueError("naive datetime: timestamps must be timezone-aware")
        utc = dt.astimezone(UTC)
        return cls(
            utc=utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
            local=utc.astimezone(ZoneInfo(tz)).isoformat(timespec="seconds"),
            tz=tz,
        )

    @classmethod
    def from_epoch(cls, seconds: float, tz: str) -> Timestamp:
        return cls.from_datetime(datetime.fromtimestamp(seconds, UTC), tz)


@dataclass(frozen=True)
class DeviceRef:
    id: str
    name: str
    mac: str
    model: str


@dataclass(frozen=True)
class AssertionResult:
    name: str
    verdict: Verdict
    expected: Any = None
    observed: Any = None


@dataclass(frozen=True)
class DeviceResult:
    device: DeviceRef
    verdict: Verdict
    reason: Reason | None = None
    message: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    assertions: tuple[AssertionResult, ...] = ()

    def __post_init__(self) -> None:
        if self.verdict is Verdict.PASS and self.reason is not None:
            raise ValueError("PASS carries no reason")
        if self.verdict is not Verdict.PASS and self.reason is None:
            raise ValueError(f"{self.verdict.value} requires a reason")


@dataclass(frozen=True)
class CheckResult:
    test_id: str
    title: str
    coverage: str
    limitation: str | None
    devices: tuple[DeviceResult, ...]
    verdict: Verdict = field(init=False)
    counts: dict[str, int] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "verdict", rollup(d.verdict for d in self.devices))
        counts = {v.value: 0 for v in Verdict}
        for d in self.devices:
            counts[d.verdict.value] += 1
        object.__setattr__(self, "counts", counts)


@dataclass(frozen=True)
class SiteRef:
    id: str
    name: str
    timezone: str


@dataclass(frozen=True)
class RunResult:
    run_id: str
    tool_version: str
    org_id: str
    api_host: str
    site: SiteRef
    catalogue_path: str
    catalogue_sha256: str
    started_at: Timestamp
    finished_at: Timestamp
    checks: tuple[CheckResult, ...]
    schema_version: int = RESULT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self, dict_factory=_json_dict)


def rollup(verdicts) -> Verdict:
    """Collapse device verdicts into one check verdict. An empty input gives ERROR."""
    seen = set(verdicts)
    if not seen:
        return Verdict.ERROR
    return next(v for v in _ROLLUP_ORDER if v in seen)


def _json_dict(items: list[tuple[str, Any]]) -> dict[str, Any]:
    return {k: (v.value if isinstance(v, Enum) else v) for k, v in items}
