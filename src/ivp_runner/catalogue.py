"""Catalogue contract: checks as data. Adding a check means adding YAML.

    python -m ivp_runner.catalogue explain catalogue/ap.yaml

prints every check's pass criterion as the English sentences a reviewer signs off.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ivp_runner.criteria import PLACEHOLDER_RE, Assertion, Criterion, ExpectRef, describe
from ivp_runner.results import Method

SiteClassOrAll = Literal["all", "small", "medium", "large"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MopRef(_Strict):
    section: str = Field(min_length=1)
    description: str = Field(min_length=1)  # verbatim from the MOP


class Collect(_Strict):
    method: Method
    source: str = Field(min_length=1)  # named source; the method backend owns the URL
    sampling: Literal["once", "run_start_and_end"] = "once"


class Target(_Strict):
    kind: Literal["ap"]
    select: Assertion


class Check(_Strict):
    test_id: str = Field(pattern=r"^[A-Z]{2,}-\d{2}$")
    title: str = Field(min_length=1)
    mop: MopRef
    site_classes: list[SiteClassOrAll] = Field(min_length=1)
    coverage: Literal["full", "partial"]
    limitation: str | None = None
    requires: list[str] = Field(default_factory=list)
    vars: dict[str, ExpectRef | str] = Field(default_factory=dict)
    collect: Collect
    target: Target
    pass_when: Criterion
    evidence: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _rules(self) -> Check:
        if self.coverage == "partial" and not (self.limitation or "").strip():
            raise ValueError(f"{self.test_id}: partial coverage requires a limitation")
        if "all" in self.site_classes and len(self.site_classes) > 1:
            raise ValueError(f"{self.test_id}: 'all' cannot be combined with other site classes")
        twice = any(a.op == "unchanged_during_run" for _, a in self.pass_when.assertions())
        if twice != (self.collect.sampling == "run_start_and_end"):
            raise ValueError(
                f"{self.test_id}: unchanged_during_run requires sampling run_start_and_end, "
                "and vice versa"
            )
        used = {
            m.group(1)
            for path in self.field_paths(fill_vars=False)
            for m in PLACEHOLDER_RE.finditer(path)
        }
        loop_vars = {
            prefix.rsplit(".", 1)[1].strip("<>")
            for prefix, _ in self.pass_when.assertions()
            if prefix
        }
        undefined = sorted(used - set(self.vars) - loop_vars)
        if undefined:
            raise ValueError(f"{self.test_id}: placeholders without a var: {undefined}")
        return self

    def applies_to(self, site_class: str) -> bool:
        return "all" in self.site_classes or site_class in self.site_classes

    def field_paths(self, fill_vars: bool = False) -> list[str]:
        """Every field path this check reads, with placeholders left as ``<name>``."""
        paths = [self.target.select.field]
        for prefix, a in self.pass_when.assertions():
            paths.append(f"{prefix}.{a.field}" if prefix else a.field)
            if a.reset_guard:
                paths.append(a.reset_guard)
        for prefix, _ in self.pass_when.assertions():
            if prefix:
                paths.append(prefix.rsplit(".", 1)[0])
        paths.extend(self.evidence)
        return list(dict.fromkeys(paths))


class Catalogue(_Strict):
    schema_version: Literal[2]
    checks: list[Check] = Field(min_length=1)

    @model_validator(mode="after")
    def _graph(self) -> Catalogue:
        ids = [c.test_id for c in self.checks]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate test IDs {dupes}")
        for c in self.checks:
            unknown = [r for r in c.requires if r not in ids]
            if unknown:
                raise ValueError(f"{c.test_id}.requires: unknown test IDs {unknown}")
        self.ordered()  # raises on cycles
        return self

    def get(self, test_id: str) -> Check:
        return next(c for c in self.checks if c.test_id == test_id)

    def ordered(self) -> list[Check]:
        """Checks in dependency order (preconditions first), stable otherwise."""
        by_id = {c.test_id: c for c in self.checks}
        done: list[str] = []
        visiting: set[str] = set()

        def visit(tid: str) -> None:
            if tid in done:
                return
            if tid in visiting:
                raise ValueError(f"requires cycle through {tid}")
            visiting.add(tid)
            for r in by_id[tid].requires:
                visit(r)
            visiting.discard(tid)
            done.append(tid)

        for c in self.checks:
            visit(c.test_id)
        return [by_id[t] for t in done]


def load_catalogue(path: str | Path) -> Catalogue:
    with open(path, encoding="utf-8") as fh:
        return Catalogue.model_validate(yaml.safe_load(fh))


# ---------------------------------------------------------------- field inventory


def inventory_paths(markdown: str) -> set[str]:
    """Field paths listed in docs/field_inventory.md tables (first column, in backticks)."""
    return set(re.findall(r"^\| `([^`]+)` \|", markdown, re.M))


def unknown_field_paths(catalogue: Catalogue, inventory: set[str]) -> dict[str, list[str]]:
    """test_id -> paths not in the inventory. Placeholder names must match the inventory's."""
    out = {}
    for c in catalogue.checks:
        missing = [p for p in c.field_paths() if p not in inventory]
        if missing:
            out[c.test_id] = missing
    return out


# ---------------------------------------------------------------- explain


def explain(catalogue: Catalogue) -> str:
    lines = []
    for c in catalogue.checks:
        classes = ", ".join(c.site_classes)
        lines.append(f"{c.test_id}  {c.title}")
        lines.append(f"  MOP: {c.mop.section} / {c.mop.description.strip()}")
        lines.append(
            f"  Applies to: {classes}; method {c.collect.method}:{c.collect.source}"
            f" ({c.collect.sampling}); coverage {c.coverage}"
        )
        if c.requires:
            lines.append(f"  Only if PASS: {', '.join(c.requires)} (otherwise SKIP)")
        if c.vars:
            shown = {
                k: (f"site's {v.expect}" if isinstance(v, ExpectRef) else v)
                for k, v in c.vars.items()
            }
            lines.append(f"  Where: {', '.join(f'<{k}> = {v}' for k, v in shown.items())}")
        lines.append("  PASS when ALL of:")
        lines.extend(
            f"    - {s}" if not s.startswith("  ") else f"      {s.strip()}"
            for s in describe(c.pass_when)
        )
        if c.limitation:
            lines.append(f"  Limitation: {' '.join(c.limitation.split())}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] != "explain":
        print("usage: python -m ivp_runner.catalogue explain <catalogue.yaml>", file=sys.stderr)
        return 2
    print(explain(load_catalogue(argv[2])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
