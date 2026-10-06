"""Check catalogue schema: load and strictly validate a catalogue YAML file.

Validation is structural only. It does not check that field paths exist in
docs/field_inventory.md; that is a separate step.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SCHEMA_VERSION = 1
TEST_ID_RE = re.compile(r"^[A-Z]{2,}-\d{2}$")
COVERAGE = ("full", "partial")
SAMPLING = ("single", "run_start_and_end")
BANDS = ("24", "5", "6")
EXPECTATION_KEYS = ("ssids", "mgmt_subnet", "dns_servers", "min_uplink_speed_mbps")


class CatalogueError(ValueError):
    """The catalogue file is malformed. The message names the offending key."""


@dataclass(frozen=True)
class ExpectedSsid:
    ssid: str
    bands: tuple[str, ...]


@dataclass(frozen=True)
class Expectations:
    """Design-intent values. None means not configured, so checks that use it ERROR."""

    ssids: tuple[ExpectedSsid, ...] | None = None
    mgmt_subnet: ipaddress.IPv4Network | ipaddress.IPv6Network | None = None
    dns_servers: frozenset[str] | None = None
    min_uplink_speed_mbps: int | None = None

    def ssid_count_per_band(self) -> dict[str, int] | None:
        """Expected SSID count keyed by stats band key (e.g. ``band_5``)."""
        if self.ssids is None:
            return None
        counts = {f"band_{b}": 0 for b in BANDS}
        for s in self.ssids:
            for b in s.bands:
                counts[f"band_{b}"] += 1
        return counts


@dataclass(frozen=True)
class Assertion:
    name: str
    pass_criteria: str


@dataclass(frozen=True)
class Check:
    id: str
    title: str
    coverage: str
    fields: tuple[str, ...]
    pass_criteria: str
    limitation: str | None = None
    precondition_for: tuple[str, ...] = ()
    uses_expectations: tuple[str, ...] = ()
    assertions: tuple[Assertion, ...] = ()
    sampling: str = "single"


@dataclass(frozen=True)
class Catalogue:
    schema_version: int
    endpoint: str
    endpoint_params: dict[str, str]
    uplink_port: str
    expectations: Expectations
    checks: tuple[Check, ...]
    _by_id: dict[str, Check] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._by_id.update({c.id: c for c in self.checks})

    def get(self, test_id: str) -> Check:
        return self._by_id[test_id]

    def preconditions_of(self, test_id: str) -> tuple[str, ...]:
        """IDs of checks that gate ``test_id``."""
        return tuple(c.id for c in self.checks if test_id in c.precondition_for)

    def missing_expectations(self, test_id: str) -> tuple[str, ...]:
        check = self.get(test_id)
        return tuple(k for k in check.uses_expectations if getattr(self.expectations, k) is None)


def load_catalogue(path: str | Path) -> Catalogue:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return parse_catalogue(raw)


def parse_catalogue(raw: Any) -> Catalogue:
    top = _mapping(raw, "catalogue")
    _only_keys(
        top,
        "catalogue",
        required={"schema_version", "source", "expectations", "uplink_port", "checks"},
    )
    if top["schema_version"] != SCHEMA_VERSION:
        raise CatalogueError(
            f"schema_version: expected {SCHEMA_VERSION}, got {top['schema_version']!r}"
        )

    source = _mapping(top["source"], "source")
    _only_keys(source, "source", required={"endpoint"}, optional={"params"})
    endpoint = _str(source["endpoint"], "source.endpoint")
    params = _mapping(source.get("params") or {}, "source.params")

    expectations = _parse_expectations(top["expectations"])
    uplink_port = _str(top["uplink_port"], "uplink_port")

    checks_raw = top["checks"]
    if not isinstance(checks_raw, list) or not checks_raw:
        raise CatalogueError("checks: must be a non-empty list")
    checks = tuple(_parse_check(c, i) for i, c in enumerate(checks_raw))

    ids = [c.id for c in checks]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise CatalogueError(f"checks: duplicate test IDs {dupes}")
    for c in checks:
        unknown = [t for t in c.precondition_for if t not in ids]
        if unknown:
            raise CatalogueError(f"{c.id}.precondition_for: unknown test IDs {unknown}")
        if c.id in c.precondition_for:
            raise CatalogueError(f"{c.id}.precondition_for: a check cannot gate itself")

    return Catalogue(
        schema_version=SCHEMA_VERSION,
        endpoint=endpoint,
        endpoint_params={str(k): str(v) for k, v in params.items()},
        uplink_port=uplink_port,
        expectations=expectations,
        checks=checks,
    )


def _parse_expectations(raw: Any) -> Expectations:
    exp = _mapping(raw, "expectations")
    _only_keys(exp, "expectations", required=set(EXPECTATION_KEYS))

    ssids = None
    if exp["ssids"] is not None:
        if not isinstance(exp["ssids"], list) or not exp["ssids"]:
            raise CatalogueError("expectations.ssids: must be a non-empty list or null")
        parsed = []
        for i, s in enumerate(exp["ssids"]):
            where = f"expectations.ssids[{i}]"
            s = _mapping(s, where)
            _only_keys(s, where, required={"ssid", "bands"})
            bands = s["bands"]
            if not isinstance(bands, list) or not bands or any(b not in BANDS for b in bands):
                raise CatalogueError(f"{where}.bands: must be a non-empty subset of {BANDS}")
            parsed.append(ExpectedSsid(_str(s["ssid"], f"{where}.ssid"), tuple(bands)))
        ssids = tuple(parsed)

    subnet = None
    if exp["mgmt_subnet"] is not None:
        try:
            subnet = ipaddress.ip_network(_str(exp["mgmt_subnet"], "expectations.mgmt_subnet"))
        except ValueError as e:
            raise CatalogueError(f"expectations.mgmt_subnet: {e}") from None

    dns = None
    if exp["dns_servers"] is not None:
        if not isinstance(exp["dns_servers"], list) or not exp["dns_servers"]:
            raise CatalogueError("expectations.dns_servers: must be a non-empty list or null")
        for d in exp["dns_servers"]:
            try:
                ipaddress.ip_address(d)
            except ValueError:
                raise CatalogueError(f"expectations.dns_servers: {d!r} is not an IP") from None
        dns = frozenset(exp["dns_servers"])

    speed = exp["min_uplink_speed_mbps"]
    if speed is not None and (not isinstance(speed, int) or isinstance(speed, bool) or speed <= 0):
        raise CatalogueError("expectations.min_uplink_speed_mbps: must be a positive int or null")

    return Expectations(ssids, subnet, dns, speed)


def _parse_check(raw: Any, index: int) -> Check:
    where = f"checks[{index}]"
    c = _mapping(raw, where)
    _only_keys(
        c,
        where,
        required={"id", "title", "coverage", "fields", "pass_criteria"},
        optional={"limitation", "precondition_for", "uses_expectations", "assertions", "sampling"},
    )
    test_id = _str(c["id"], f"{where}.id")
    if not TEST_ID_RE.match(test_id):
        raise CatalogueError(f"{where}.id: {test_id!r} does not match {TEST_ID_RE.pattern}")
    where = test_id

    coverage = c["coverage"]
    if coverage not in COVERAGE:
        raise CatalogueError(f"{where}.coverage: must be one of {COVERAGE}")
    limitation = c.get("limitation")
    if coverage == "partial" and not limitation:
        raise CatalogueError(f"{where}: partial coverage requires a limitation")

    fields_ = c["fields"]
    if not isinstance(fields_, list) or not fields_:
        raise CatalogueError(f"{where}.fields: must be a non-empty list")

    uses = tuple(c.get("uses_expectations") or ())
    bad = [u for u in uses if u not in EXPECTATION_KEYS]
    if bad:
        raise CatalogueError(f"{where}.uses_expectations: unknown keys {bad}")

    sampling = c.get("sampling", "single")
    if sampling not in SAMPLING:
        raise CatalogueError(f"{where}.sampling: must be one of {SAMPLING}")

    assertions = []
    for i, a in enumerate(c.get("assertions") or ()):
        aw = f"{where}.assertions[{i}]"
        a = _mapping(a, aw)
        _only_keys(a, aw, required={"name", "pass_criteria"})
        assertions.append(Assertion(_str(a["name"], aw), _str(a["pass_criteria"], aw)))

    return Check(
        id=test_id,
        title=_str(c["title"], f"{where}.title"),
        coverage=coverage,
        fields=tuple(_str(f, f"{where}.fields") for f in fields_),
        pass_criteria=_str(c["pass_criteria"], f"{where}.pass_criteria").strip(),
        limitation=limitation.strip() if limitation else None,
        precondition_for=tuple(c.get("precondition_for") or ()),
        uses_expectations=uses,
        assertions=tuple(assertions),
        sampling=sampling,
    )


def _mapping(v: Any, where: str) -> dict:
    if not isinstance(v, dict):
        raise CatalogueError(f"{where}: must be a mapping")
    return v


def _str(v: Any, where: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise CatalogueError(f"{where}: must be a non-empty string")
    return v


def _only_keys(d: dict, where: str, required: set[str], optional: set[str] = frozenset()) -> None:
    missing = sorted(required - d.keys())
    if missing:
        raise CatalogueError(f"{where}: missing keys {missing}")
    unknown = sorted(d.keys() - required - optional)
    if unknown:
        raise CatalogueError(f"{where}: unknown keys {unknown}")
