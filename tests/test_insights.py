from datetime import UTC, datetime

from ivp_runner.catalogue import DEFAULT_INVENTORY, inventory_sections
from ivp_runner.insights import FIELDS, site_insights

from .factories import ap, disconnected
from .fixtures_mist import site_doc, stats_items

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def test_every_field_read_is_in_the_inventory():
    sections = inventory_sections(DEFAULT_INVENTORY.read_text(encoding="utf-8"))
    missing = [(sec, f) for sec, f in FIELDS if f not in sections.get(sec, set())]
    assert missing == []


def test_insights_on_captured_site():
    lines = dict(site_insights(site_doc(), stats_items(), "eth0", NOW))
    assert lines["Access points"].startswith("91 total: 90 connected, 1 not connected")
    assert "local time 2026-10-07 14:00 CEST" in lines["Site"]
    assert lines["Uplinks (eth0)"].startswith("2500 Mbps") or "Mbps" in lines["Uplinks (eth0)"]
    assert "not captured yet" in lines["SLE insights"]


def test_insights_name_the_problem_aps():
    aps = [ap(1), ap(2, power_constrained=True), disconnected(9)]
    lines = dict(site_insights({"name": "S", "timezone": "Nowhere/Land"}, aps, "eth0", NOW))
    assert "(AP-TEST-9)" in lines["Access points"]
    assert lines["PoE"] == "1 power-constrained (AP-TEST-2)"
    assert "unknown time zone" in lines["Site"]
