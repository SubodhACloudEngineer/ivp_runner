"""Pass criteria: a small declarative language that reads as English.

A criterion is a list of assertions. Each assertion names one field (a literal
path from docs/field_inventory.md) and exactly one operator:

    {field: power_constrained, equals: false}
    {field: ip_stat.ip, in_subnet: {expect: mgmt_subnet}}

A ``for_each`` group repeats assertions for every key of an object, e.g. every
radio band. ``describe()`` renders any criterion as the sentence a reviewer
approves, and ``evaluate()`` runs the same structure, so the two cannot drift.

Placeholders like ``<port>`` or ``<band>`` are filled from check ``vars`` or
from the ``for_each`` key.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ivp_runner.results import Reason, Verdict
from ivp_runner.site_profile import MissingExpectation, SiteProfile

OPERATORS = ("equals", "at_least", "in_subnet", "same_set_as", "present", "unchanged_during_run")
PLACEHOLDER_RE = re.compile(r"<([a-z_]+)>")


class ExpectRef(BaseModel):
    """A reference to a site-profile value, e.g. ``{expect: dns_servers}``."""

    model_config = ConfigDict(extra="forbid")
    expect: str = Field(min_length=1)


Value = ExpectRef | bool | int | float | str | list[str]


class Assertion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str = Field(min_length=1)
    name: str | None = None  # optional stable label, shown in results
    equals: Value | None = None
    at_least: ExpectRef | int | float | None = None
    in_subnet: ExpectRef | str | None = None
    same_set_as: ExpectRef | list[str] | None = None
    present: Literal[True] | None = None
    unchanged_during_run: Literal[True] | None = None
    # Field that only grows while a device stays up. If it went down between the
    # two samples, the device rebooted and the counter comparison is void.
    reset_guard: str | None = None

    @model_validator(mode="after")
    def _one_operator(self) -> Assertion:
        ops = [op for op in OPERATORS if op in self.model_fields_set]
        if len(ops) != 1:
            raise ValueError(
                f"{self.field}: exactly one operator of {OPERATORS} required, got {ops}"
            )
        if self.reset_guard is not None and ops[0] != "unchanged_during_run":
            raise ValueError(f"{self.field}: reset_guard only applies to unchanged_during_run")
        return self

    @property
    def op(self) -> str:
        return next(op for op in OPERATORS if op in self.model_fields_set)

    @property
    def operand(self) -> Any:
        return getattr(self, self.op)


class ForEach(BaseModel):
    """Repeat ``all`` for every key under ``for_each``; the last ``<var>`` names the key."""

    model_config = ConfigDict(extra="forbid")
    for_each: str = Field(pattern=r"^[\w.<>]+\.<[a-z_]+>$")
    skip_if: Assertion | None = None
    all: list[Assertion] = Field(min_length=1)
    min_evaluated: int = Field(default=0, ge=0)

    @property
    def container(self) -> str:
        return self.for_each.rsplit(".", 1)[0]

    @property
    def key_var(self) -> str:
        return self.for_each.rsplit(".", 1)[1].strip("<>")


class Criterion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    all: list[Assertion | ForEach] = Field(min_length=1)

    def assertions(self) -> list[tuple[str | None, Assertion]]:
        """(for_each prefix or None, assertion) for every assertion, skip_if included."""
        out: list[tuple[str | None, Assertion]] = []
        for item in self.all:
            if isinstance(item, ForEach):
                if item.skip_if:
                    out.append((item.for_each, item.skip_if))
                out.extend((item.for_each, a) for a in item.all)
            else:
                out.append((None, item))
        return out


# ---------------------------------------------------------------- describe


def _describe_value(v: Any) -> str:
    if isinstance(v, ExpectRef):
        return f"the site's expected {v.expect}"
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, str):
        return f'"{v}"'
    return str(v)


def describe_assertion(a: Assertion, prefix: str | None = None) -> str:
    path = f"{prefix}.{a.field}" if prefix else a.field
    v = _describe_value(a.operand)
    text = {
        "equals": f"{path} equals {v}",
        "at_least": f"{path} is at least {v}",
        "in_subnet": f"{path} is an address inside {v}",
        "same_set_as": f"{path} contains exactly the same entries as {v} (any order)",
        "present": f"{path} is reported",
        "unchanged_during_run": f"{path} is the same at run start and run end",
    }[a.op]
    if a.reset_guard:
        text += f" (if {a.reset_guard} went down, the device rebooted: ERROR, not PASS)"
    return text


def describe(c: Criterion) -> list[str]:
    """One English line per assertion, in catalogue order."""
    lines = []
    for item in c.all:
        if isinstance(item, ForEach):
            head = f"For each {item.key_var} in {item.container}"
            if item.skip_if:
                head += f" (ignoring any where {describe_assertion(item.skip_if)})"
            lines.append(head + ":")
            lines.extend(f"  {describe_assertion(a, item.for_each)}" for a in item.all)
            if item.min_evaluated:
                lines.append(f"  at least {item.min_evaluated} {item.key_var}(s) must be checked")
        else:
            lines.append(describe_assertion(item))
    return lines


# ---------------------------------------------------------------- evaluate

MISSING = object()


@dataclass
class Outcome:
    field: str
    rule: str
    expected: Any
    actual: Any
    verdict: Verdict
    reason: Reason | None = None
    message: str = ""


@dataclass
class Samples:
    """The target device's item at run start, and at run end when the check samples twice."""

    start: dict
    end: dict | None = None
    vars: dict[str, str] = field(default_factory=dict)


def fill(path: str, vars: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        if m.group(1) not in vars:
            raise MissingExpectation(f"<{m.group(1)}>")
        return vars[m.group(1)]

    return PLACEHOLDER_RE.sub(sub, path)


def get_path(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(obj, dict) or part not in obj:
            return MISSING
        obj = obj[part]
    return obj


def evaluate(c: Criterion, samples: Samples, profile: SiteProfile) -> list[Outcome]:
    out: list[Outcome] = []
    for item in c.all:
        if isinstance(item, ForEach):
            out.extend(_evaluate_for_each(item, samples, profile))
        else:
            out.append(_evaluate_one(item, samples, profile, samples.vars, item.field))
    return out


def _evaluate_for_each(fe: ForEach, samples: Samples, profile: SiteProfile) -> list[Outcome]:
    rule = f"for each {fe.key_var} in {fe.container}"
    try:
        container_path = fill(fe.container, samples.vars)
    except MissingExpectation as e:
        return [_error(fe.container, rule, Reason.EXPECTATION_MISSING, f"missing {e}")]
    container = get_path(samples.start, container_path)
    if container is MISSING:
        return [_error(container_path, rule, Reason.FIELD_MISSING, "not reported")]
    if not isinstance(container, dict):
        return [_error(container_path, rule, Reason.BAD_DATA, "expected an object")]

    out: list[Outcome] = []
    evaluated = []
    for key in sorted(container):
        vars_ = {**samples.vars, fe.key_var: key}
        if fe.skip_if is not None:
            skip_path = f"{container_path}.{key}.{fe.skip_if.field}"
            check = _evaluate_one(fe.skip_if, samples, profile, vars_, skip_path, optional=True)
            if check.verdict is Verdict.PASS:
                continue
        evaluated.append(key)
        for a in fe.all:
            out.append(
                _evaluate_one(a, samples, profile, vars_, f"{container_path}.{key}.{a.field}")
            )
    if len(evaluated) < fe.min_evaluated:
        out.append(
            Outcome(
                container_path,
                f"{rule}: at least {fe.min_evaluated} must be checked",
                fe.min_evaluated,
                len(evaluated),
                Verdict.FAIL,
                Reason.CRITERIA_NOT_MET,
                f"only {len(evaluated)} {fe.key_var}(s) left after exclusions",
            )
        )
    return out


def _evaluate_one(
    a: Assertion,
    samples: Samples,
    profile: SiteProfile,
    vars_: dict[str, str],
    path: str,
    optional: bool = False,
) -> Outcome:
    rule = describe_assertion(a)
    try:
        path = fill(path, vars_)
        rule = describe_assertion(a.model_copy(update={"field": path}))
        expected = _resolve(a.operand, profile, vars_)
    except MissingExpectation as e:
        return _error(path, rule, Reason.EXPECTATION_MISSING, f"site profile has no {e}")

    if a.op == "unchanged_during_run":
        return _unchanged(a, samples, path, rule, vars_)

    actual = get_path(samples.start, path)
    if actual is MISSING:
        if optional:
            return Outcome(path, rule, expected, None, Verdict.FAIL, Reason.CRITERIA_NOT_MET)
        if a.op == "present":
            return Outcome(
                path, rule, "reported", None, Verdict.FAIL, Reason.CRITERIA_NOT_MET, "not reported"
            )
        return _error(path, rule, Reason.FIELD_MISSING, "not reported")

    try:
        ok = _compare(a.op, actual, expected)
    except (TypeError, ValueError) as e:
        return Outcome(path, rule, expected, actual, Verdict.ERROR, Reason.BAD_DATA, str(e))
    if a.op == "present":
        expected = "reported"
    if ok:
        return Outcome(path, rule, expected, actual, Verdict.PASS)
    return Outcome(path, rule, expected, actual, Verdict.FAIL, Reason.CRITERIA_NOT_MET)


def _unchanged(a: Assertion, samples: Samples, path: str, rule: str, vars_) -> Outcome:
    expected = "unchanged during run"
    if samples.end is None:
        return _error(path, rule, Reason.SAMPLE_MISSING, "no run-end sample for this device")
    start, end = get_path(samples.start, path), get_path(samples.end, path)
    if start is MISSING or end is MISSING:
        return _error(path, rule, Reason.FIELD_MISSING, "not reported in both samples")
    if not _is_number(start) or not _is_number(end):
        return Outcome(
            path,
            rule,
            expected,
            {"start": start, "end": end},
            Verdict.ERROR,
            Reason.BAD_DATA,
            "not a number",
        )
    actual = {"start": start, "end": end, "delta": end - start}
    if a.reset_guard:
        guard = fill(a.reset_guard, vars_)
        g0, g1 = get_path(samples.start, guard), get_path(samples.end, guard)
        if _is_number(g0) and _is_number(g1) and g1 < g0:
            msg = f"{guard} went from {g0} to {g1}: device rebooted during the run"
            return Outcome(path, rule, expected, actual, Verdict.ERROR, Reason.COUNTER_RESET, msg)
    if end < start:
        msg = "counter went backwards: device rebooted or counters were cleared during the run"
        return Outcome(path, rule, expected, actual, Verdict.ERROR, Reason.COUNTER_RESET, msg)
    if end == start:
        return Outcome(path, rule, expected, actual, Verdict.PASS)
    return Outcome(path, rule, expected, actual, Verdict.FAIL, Reason.CRITERIA_NOT_MET)


def _resolve(v: Any, profile: SiteProfile, vars_: dict[str, str]) -> Any:
    if isinstance(v, ExpectRef):
        return profile.lookup(fill(v.expect, vars_))
    return v


def _is_number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _compare(op: str, actual: Any, expected: Any) -> bool:
    if op == "present":
        return True
    if op == "equals":
        if isinstance(expected, bool) or isinstance(actual, bool):
            if not (isinstance(expected, bool) and isinstance(actual, bool)):
                raise TypeError(f"expected a boolean, got {type(actual).__name__}")
        elif _is_number(expected) != _is_number(actual):
            raise TypeError(
                f"cannot compare {type(actual).__name__} with {type(expected).__name__}"
            )
        return actual == expected
    if op == "at_least":
        if not (_is_number(actual) and _is_number(expected)):
            raise TypeError(f"expected numbers, got {type(actual).__name__}")
        return actual >= expected
    if op == "in_subnet":
        if not isinstance(actual, str):
            raise TypeError(f"expected an address string, got {type(actual).__name__}")
        return ipaddress.ip_address(actual) in ipaddress.ip_network(expected)
    if op == "same_set_as":
        if not isinstance(actual, list):
            raise TypeError(f"expected a list, got {type(actual).__name__}")
        return {str(x) for x in actual} == {str(x) for x in expected}
    raise ValueError(f"unknown operator {op}")


def _error(path: str, rule: str, reason: Reason, message: str) -> Outcome:
    return Outcome(path, rule, None, None, Verdict.ERROR, reason, message)
