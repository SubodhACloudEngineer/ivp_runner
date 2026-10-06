"""Evaluate a catalogue against collected payloads. Pure apart from write_* helpers.

Nothing here is specific to a check: every check is the same loop over its
catalogue entry. A new check is new YAML.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ivp_runner.catalogue import Catalogue, Check
from ivp_runner.criteria import (
    MISSING,
    Criterion,
    ExpectRef,
    Outcome,
    Samples,
    fill,
    get_path,
)
from ivp_runner.criteria import (
    evaluate as evaluate_criterion,
)
from ivp_runner.methods import IMPLEMENTED_METHODS, Payload
from ivp_runner.results import (
    AssertionResult,
    DeviceRef,
    RawRef,
    Reason,
    SiteRef,
    TestResult,
    Verdict,
    rollup,
    timestamps,
)
from ivp_runner.site_profile import MissingExpectation, SiteProfile


@dataclass(frozen=True)
class RunContext:
    run_id: str
    site: SiteRef
    timezone: str  # IANA zone for site-local timestamps
    out_dir: str  # run output folder; evidence goes under <out_dir>/evidence
    started_at: datetime


@dataclass(frozen=True)
class Evaluation:
    result: TestResult
    evidence: dict[str, Any] = field(default_factory=dict)


# Payloads keyed by (method, source) -> {sample: Payload}; sample is "once", "start" or "end".
Payloads = dict[tuple[str, str], dict[str, Payload]]


def evaluate(
    catalogue: Catalogue, profile: SiteProfile, payloads: Payloads, ctx: RunContext
) -> list[Evaluation]:
    out: list[Evaluation] = []
    verdicts: dict[tuple[str, str], Verdict] = {}  # (test_id, device_id) -> verdict
    for check in catalogue.ordered():
        if not check.applies_to(profile.site_class):
            continue
        for ev in _evaluate_check(check, profile, payloads, ctx, verdicts):
            if ev.result.device is not None:
                verdicts[(check.test_id, ev.result.device.id)] = ev.result.verdict
            out.append(ev)
    return out


def _evaluate_check(check: Check, profile, payloads: Payloads, ctx: RunContext, verdicts):
    method = check.collect.method
    if method not in IMPLEMENTED_METHODS:
        yield _site_error(
            check, ctx, Reason.METHOD_UNAVAILABLE, f"method {method!r} is not implemented"
        )
        return

    try:
        vars_ = {
            k: str(profile.lookup(v.expect)) if isinstance(v, ExpectRef) else v
            for k, v in check.vars.items()
        }
    except MissingExpectation as e:
        yield _site_error(check, ctx, Reason.EXPECTATION_MISSING, f"site profile has no {e}")
        return

    samples = payloads.get((method, check.collect.source), {})
    twice = check.collect.sampling == "run_start_and_end"
    start = samples.get("start") if twice else (samples.get("once") or samples.get("start"))
    end = samples.get("end") if twice else None
    if start is None:
        yield _site_error(
            check, ctx, Reason.API_ERROR, f"no {method}:{check.collect.source} payload collected"
        )
        return
    end_index = _index_by_id(end) if end else {}

    select = Criterion(all=[check.target.select])
    for i, item in enumerate(start.items):
        if evaluate_criterion(select, Samples(item), profile)[0].verdict is not Verdict.PASS:
            continue
        device = _device(item, start.identity, i)
        refs = [
            RawRef(
                path=start.raw_path, sha256=start.raw_sha256, pointer=f"/{i}", sample=start.sample
            )
        ]
        end_item = None
        if end is not None and device.id in end_index:
            j, end_item = end_index[device.id]
            refs.append(
                RawRef(path=end.raw_path, sha256=end.raw_sha256, pointer=f"/{j}", sample="end")
            )
        when = (end or start).collected_at
        base = _base(check, ctx, device, when, refs)

        blocked = [r for r in check.requires if verdicts.get((r, device.id)) is not Verdict.PASS]
        if blocked:
            msg = f"precondition {', '.join(blocked)} did not pass for this device"
            base["evidence_path"] = None  # nothing was evaluated, so no evidence file
            yield Evaluation(
                TestResult(
                    **base, verdict=Verdict.SKIP, reason=Reason.PRECONDITION_FAILED, message=msg
                )
            )
            continue

        outcomes = evaluate_criterion(check.pass_when, Samples(item, end_item, vars_), profile)
        yield Evaluation(_result(base, outcomes), _evidence(check, item, end_item, twice, vars_))


def _result(base: dict, outcomes: list[Outcome]) -> TestResult:
    verdict = rollup(o.verdict for o in outcomes)
    reason = None
    if verdict is Verdict.FAIL:
        reason = Reason.CRITERIA_NOT_MET
    elif verdict is Verdict.ERROR:
        reason = next(o.reason for o in outcomes if o.verdict is Verdict.ERROR)
    problems = [
        f"{o.field}: {o.message}" for o in outcomes if o.verdict is not Verdict.PASS and o.message
    ]
    return TestResult(
        **base,
        verdict=verdict,
        reason=reason,
        message="; ".join(problems),
        expected={o.field: o.expected for o in outcomes},
        actual={o.field: o.actual for o in outcomes},
        assertions=[
            AssertionResult(
                field=o.field,
                rule=o.rule,
                expected=o.expected,
                actual=o.actual,
                verdict=o.verdict,
                reason=o.reason,
                message=o.message,
            )
            for o in outcomes
        ],
    )


def _evidence(
    check: Check, item: dict, end_item: dict | None, twice: bool, vars_
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for path in check.evidence:
        try:
            path = fill(path, vars_)
        except MissingExpectation:
            continue
        start = _plain(get_path(item, path))
        if twice:
            fields[path] = {
                "start": start,
                "end": _plain(get_path(end_item, path)) if end_item else None,
            }
        else:
            fields[path] = start
    return {"fields": fields}


def _plain(v: Any) -> Any:
    return None if v is MISSING else v


def _base(
    check: Check, ctx: RunContext, device: DeviceRef | None, when: datetime, refs: list[RawRef]
) -> dict:
    utc, local = timestamps(when, ctx.timezone)
    evidence_path = (
        str(Path(ctx.out_dir) / "evidence" / check.test_id / f"{device.id}.json")
        if device
        else None
    )
    return {
        "run_id": ctx.run_id,
        "test_id": check.test_id,
        "site": ctx.site,
        "device": device,
        "timestamp_utc": utc,
        "timestamp_local": local,
        "timezone": ctx.timezone,
        "method": check.collect.method,
        "evidence_path": evidence_path,
        "raw_ref": refs,
    }


def _site_error(check: Check, ctx: RunContext, reason: Reason, message: str) -> Evaluation:
    base = _base(check, ctx, None, ctx.started_at, [])
    return Evaluation(TestResult(**base, verdict=Verdict.ERROR, reason=reason, message=message))


def _device(item: dict, identity: dict[str, str], index: int) -> DeviceRef:
    values = {k: item.get(v) for k, v in identity.items()}
    values = {k: str(v) for k, v in values.items() if v is not None}
    values.setdefault("id", f"item-{index}")
    return DeviceRef(**values)


def _index_by_id(payload: Payload) -> dict[str, tuple[int, dict]]:
    key = payload.identity.get("id", "id")
    return {str(it[key]): (j, it) for j, it in enumerate(payload.items) if key in it}


# ---------------------------------------------------------------- output helpers


def write_evidence(evaluations: list[Evaluation]) -> int:
    """Write each evaluation's evidence JSON to its evidence_path. Returns files written."""
    n = 0
    for ev in evaluations:
        path = ev.result.evidence_path
        if not path or not ev.evidence:
            continue
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(ev.evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        n += 1
    return n


def write_results(evaluations: list[Evaluation], path: str | Path) -> None:
    """Write all result records as a JSON array."""
    records = [ev.result.model_dump(mode="json", by_alias=True) for ev in evaluations]
    Path(path).write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
