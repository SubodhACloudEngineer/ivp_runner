"""Assertion engine: catalogue entries + collected data -> result records.

Nothing here is specific to a check: every check runs through the same loop
over its catalogue entry, so a new check is new YAML.

Trust rules enforced here:
* A missing field, expectation, sample or payload is ERROR, never FAIL.
* An unexpected exception in one check, or for one device, becomes ERROR
  (engine_error) for that check or device only. The run always completes.

Pure apart from the write_* helpers.
"""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ivp_runner.catalogue import Catalogue, Check
from ivp_runner.collectors import IMPLEMENTED_METHODS, CollectError, Collection, Payload
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


def evaluate(
    catalogue: Catalogue,
    profile: SiteProfile,
    start: Collection,
    end: Collection | None,
    ctx: RunContext,
) -> list[Evaluation]:
    """Evaluate every applicable check. Never raises because of one check."""
    out: list[Evaluation] = []
    verdicts: dict[tuple[str, str], Verdict] = {}  # (test_id, device_id) -> verdict
    for check in catalogue.ordered():
        if not check.applies_to(profile.site_class):
            continue
        try:
            evaluations = _evaluate_check(check, profile, start, end or {}, ctx, verdicts)
        except Exception as e:  # one bad check must not abort the run
            evaluations = [_site_error(check, ctx, Reason.ENGINE_ERROR, _describe_exc(e))]
        for ev in evaluations:
            if ev.result.device is not None:
                verdicts[(check.test_id, ev.result.device.id)] = ev.result.verdict
            out.append(ev)
    return out


def _evaluate_check(
    check: Check, profile, start_c: Collection, end_c: Collection, ctx: RunContext, verdicts
) -> list[Evaluation]:
    method = check.collect.method
    if method not in IMPLEMENTED_METHODS:
        msg = f"method {method!r} is not implemented"
        return [_site_error(check, ctx, Reason.METHOD_UNAVAILABLE, msg)]

    try:
        vars_ = {
            k: str(profile.lookup(v.expect)) if isinstance(v, ExpectRef) else v
            for k, v in check.vars.items()
        }
    except MissingExpectation as e:
        return [_site_error(check, ctx, Reason.EXPECTATION_MISSING, f"site profile has no {e}")]

    source = check.collect.source
    twice = check.collect.sampling == "run_start_and_end"
    start = start_c.get(source)
    if not isinstance(start, Payload):
        why = str(start) if isinstance(start, CollectError) else "not collected"
        return [_site_error(check, ctx, Reason.API_ERROR, f"{method}:{source}: {why}")]
    end = None
    if twice:
        end = end_c.get(source)
        if not isinstance(end, Payload):
            why = str(end) if isinstance(end, CollectError) else "not collected"
            msg = f"{method}:{source} run-end sample: {why}"
            return [_site_error(check, ctx, Reason.SAMPLE_MISSING, msg)]
    end_index = _index_by_id(end) if end else {}
    out: list[Evaluation] = []

    select = Criterion(all=[check.target.select])
    for i, item in enumerate(start.items):
        if evaluate_criterion(select, Samples(item), profile)[0].verdict is not Verdict.PASS:
            continue
        device = _device(item, start.identity, i)
        try:
            out.append(
                _evaluate_device(
                    check,
                    profile,
                    ctx,
                    verdicts,
                    vars_,
                    twice,
                    start,
                    end,
                    end_index,
                    i,
                    item,
                    device,
                )
            )
        except Exception as e:  # one bad device must not abort the check
            base = _base(check, ctx, device, start.collected_at, [])
            out.append(
                Evaluation(
                    TestResult(
                        **base,
                        verdict=Verdict.ERROR,
                        reason=Reason.ENGINE_ERROR,
                        message=_describe_exc(e),
                    )
                )
            )
    return out


def _evaluate_device(
    check: Check,
    profile: SiteProfile,
    ctx: RunContext,
    verdicts,
    vars_: dict[str, str],
    twice: bool,
    start: Payload,
    end: Payload | None,
    end_index: dict,
    i: int,
    item: dict,
    device: DeviceRef,
) -> Evaluation:
    refs = [
        RawRef(path=start.raw_path, sha256=start.raw_sha256, pointer=f"/{i}", sample=start.sample)
    ]
    end_item = None
    if end is not None and device.id in end_index:
        j, end_item = end_index[device.id]
        refs.append(RawRef(path=end.raw_path, sha256=end.raw_sha256, pointer=f"/{j}", sample="end"))
    when = (end or start).collected_at
    base = _base(check, ctx, device, when, refs)

    blocked = [r for r in check.requires if verdicts.get((r, device.id)) is not Verdict.PASS]
    if blocked:
        msg = f"precondition {', '.join(blocked)} did not pass for this device"
        return Evaluation(
            TestResult(**base, verdict=Verdict.SKIP, reason=Reason.PRECONDITION_FAILED, message=msg)
        )

    outcomes = evaluate_criterion(check.pass_when, Samples(item, end_item, vars_), profile)
    return Evaluation(_result(base, outcomes), _evidence(check, item, end_item, twice, vars_))


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


def _describe_exc(e: BaseException) -> str:
    frame = traceback.extract_tb(e.__traceback__)[-1] if e.__traceback__ else None
    where = f" at {Path(frame.filename).name}:{frame.lineno}" if frame else ""
    return f"internal error {type(e).__name__}: {e}{where}"


def _plain(v: Any) -> Any:
    return None if v is MISSING else v


def _base(
    check: Check, ctx: RunContext, device: DeviceRef | None, when: datetime, refs: list[RawRef]
) -> dict:
    utc, local = timestamps(when, ctx.timezone)
    # One evidence card (PNG) per result, including SKIPs and site-wide errors;
    # write_evidence puts the field-level JSON next to it.
    name = device.id if device else "site"
    evidence_path = str(Path(ctx.out_dir) / "evidence" / check.test_id / f"{name}.png")
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
    """Write each evaluation's field-level evidence as JSON beside its card.

    The card itself (``evidence_path``, a PNG) is rendered by
    ``ivp_runner.evidence.write_cards``. Returns the number of JSON files written.
    """
    n = 0
    for ev in evaluations:
        path = ev.result.evidence_path
        if not path or not ev.evidence:
            continue
        p = Path(path).with_suffix(".json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(ev.evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        n += 1
    return n


def write_results(evaluations: list[Evaluation], path: str | Path) -> None:
    """Write all result records as a JSON array."""
    records = [ev.result.model_dump(mode="json", by_alias=True) for ev in evaluations]
    Path(path).write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
