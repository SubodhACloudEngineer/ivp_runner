"""Loaders for the sanitised captured-sample fixtures (see scripts/make_fixtures.py)."""

from __future__ import annotations

import copy
import json
from functools import cache
from pathlib import Path

from ivp_runner.site_profile import load_site_profile

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "mist"


@cache
def _stats() -> list[dict]:
    return json.loads((FIXTURES / "site_stats_devices_ap.json").read_text(encoding="utf-8"))


def stats_items() -> list[dict]:
    """The 91 captured AP stats items, sanitised. A fresh copy on every call."""
    return copy.deepcopy(_stats())


def site_doc() -> dict:
    return json.loads((FIXTURES / "site.json").read_text(encoding="utf-8"))


def fixture_profile():
    return load_site_profile(FIXTURES / "site_profile.yaml")


def find(items: list[dict], **match) -> dict:
    """First item whose top-level fields match, e.g. find(items, status="disconnected")."""
    return next(it for it in items if all(it.get(k) == v for k, v in match.items()))
