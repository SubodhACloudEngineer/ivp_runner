"""Assertion engine against the sanitised captured samples (91 real AP stats items)."""

from collections import Counter

import pytest

from ivp_runner import assert_engine
from ivp_runner.assert_engine import evaluate
from ivp_runner.collectors import CollectError
from ivp_runner.results import Reason, Verdict

from .factories import catalogue, context, payload
from .fixtures_mist import find, fixture_profile, stats_items


def run(start_items, end_items=None, *, start=None, end=None):
    start = start if start is not None else {"site_device_stats": payload(start_items)}
    if end is None:
        end_src = end_items if end_items is not None else start_items
        end = {"site_device_stats": payload(end_src, "end")}
    return evaluate(catalogue(), fixture_profile(), start, end, context())


def by(evs, test_id, name=None):
    rs = [e.result for e in evs if e.result.test_id == test_id]
    if name is None:
        return rs
    return next(r for r in rs if r.device and r.device.name == name)


def counts(evs, test_id):
    return Counter(r.verdict.value for r in by(evs, test_id))


def test_full_site_counts_match_verification_run():
    evs = run(stats_items())
    assert len(evs) == 6 * 91
    assert counts(evs, "AP-00") == {"PASS": 90, "FAIL": 1}
    assert counts(evs, "AP-01") == {"PASS": 75, "FAIL": 15, "SKIP": 1}
    assert counts(evs, "AP-02") == {"PASS": 89, "FAIL": 1, "SKIP": 1}
    assert counts(evs, "AP-03") == {"PASS": 90, "SKIP": 1}
    assert counts(evs, "AP-04") == {"PASS": 88, "FAIL": 2, "SKIP": 1}
    assert counts(evs, "AP-05") == {"PASS": 90, "SKIP": 1}


def test_disconnected_ap_is_one_fail_five_skips():
    items = stats_items()
    dead = find(items, status="disconnected")["name"]
    evs = run(items)
    got = Counter(
        (r.test_id, r.verdict.value) for e in evs for r in [e.result] if r.device.name == dead
    )
    assert got == {("AP-00", "FAIL"): 1, **{(f"AP-0{i}", "SKIP"): 1 for i in range(1, 6)}}


def test_known_failing_port_errors_give_fail_with_expected_and_actual():
    """Force the uplink rx_errors above zero during the run: AP-05 must FAIL."""
    items = stats_items()
    target = find(items, status="connected")
    target["port_stat"]["eth0"]["rx_errors"] = 0
    end_items = stats_items()
    end_target = find(end_items, name=target["name"])
    end_target["port_stat"]["eth0"]["rx_errors"] = 4

    r = by(run(items, end_items), "AP-05", target["name"])
    assert r.verdict is Verdict.FAIL
    assert r.reason is Reason.CRITERIA_NOT_MET
    assert r.expected == {"port_stat.eth0.rx_errors": {"start": 0, "end": 0, "delta": 0}}
    assert r.actual == {"port_stat.eth0.rx_errors": {"start": 0, "end": 4, "delta": 4}}
    (a,) = r.assertions
    assert a.rule.startswith("port_stat.eth0.rx_errors is the same at run start and run end")
    assert [ref.sample for ref in r.raw_ref] == ["start", "end"]


def test_nonzero_but_unchanged_errors_pass_by_design():
    items = stats_items()
    name = next(
        it["name"] for it in items if it.get("port_stat", {}).get("eth0", {}).get("rx_errors")
    )
    r = by(run(items), "AP-05", name)
    assert r.verdict is Verdict.PASS
    start = r.actual["port_stat.eth0.rx_errors"]["start"]
    assert start > 0 and r.actual["port_stat.eth0.rx_errors"]["delta"] == 0


def test_missing_field_is_error_not_fail():
    items = stats_items()
    target = find(items, status="connected")
    del target["port_stat"]["eth0"]["speed"]  # AP-00 does not require speed
    r = by(run(items), "AP-04", target["name"])
    assert (r.verdict, r.reason) == (Verdict.ERROR, Reason.FIELD_MISSING)
    speed = next(a for a in r.assertions if a.field == "port_stat.eth0.speed")
    assert speed.verdict is Verdict.ERROR and speed.actual is None


def test_failed_collection_is_api_error_for_its_checks():
    err = CollectError("GET /api/v1/sites/x/stats/devices: 403 authentication failed")
    evs = run([], start={"site_device_stats": err}, end={"site_device_stats": err})
    assert {(e.result.verdict, e.result.reason) for e in evs} == {(Verdict.ERROR, Reason.API_ERROR)}
    assert all("403" in e.result.message and e.result.device is None for e in evs)


def test_failed_end_sample_is_sample_missing_only_for_ap05():
    err = CollectError("rate limited (429) after 6 attempts")
    evs = run(stats_items(), end={"site_device_stats": err})
    (ap05,) = by(evs, "AP-05")
    assert (ap05.verdict, ap05.reason) == (Verdict.ERROR, Reason.SAMPLE_MISSING)
    assert counts(evs, "AP-02") == {"PASS": 89, "FAIL": 1, "SKIP": 1}


def test_one_bad_check_does_not_abort_the_run(monkeypatch):
    real = assert_engine.evaluate_criterion

    def explode_on_ap02(criterion, samples, profile):
        if any(
            getattr(a, "field", "") == "power_constrained" and a.op == "equals"
            for a in criterion.all
        ):
            raise RuntimeError("boom")
        return real(criterion, samples, profile)

    monkeypatch.setattr(assert_engine, "evaluate_criterion", explode_on_ap02)
    evs = run(stats_items())
    ap02 = by(evs, "AP-02")
    assert {(r.verdict, r.reason) for r in ap02 if r.verdict is not Verdict.SKIP} == {
        (Verdict.ERROR, Reason.ENGINE_ERROR)
    }
    assert "RuntimeError: boom" in ap02[0].message or "RuntimeError: boom" in ap02[1].message
    # every other check still produced its normal results
    assert counts(evs, "AP-04") == {"PASS": 88, "FAIL": 2, "SKIP": 1}
    assert len(evs) == 6 * 91


def test_check_level_exception_becomes_site_wide_error(monkeypatch):
    real = assert_engine._index_by_id

    def broken(payload):
        raise KeyError("corrupt")

    monkeypatch.setattr(assert_engine, "_index_by_id", broken)
    evs = run(stats_items())
    (ap05,) = by(evs, "AP-05")
    assert (ap05.verdict, ap05.reason, ap05.device) == (Verdict.ERROR, Reason.ENGINE_ERROR, None)
    assert len(by(evs, "AP-04")) == 91
    monkeypatch.setattr(assert_engine, "_index_by_id", real)


@pytest.mark.parametrize("test_id", ["AP-01", "AP-02", "AP-03", "AP-04"])
def test_raw_refs_point_at_the_device_item(test_id):
    items = stats_items()
    evs = run(items)
    for r in by(evs, test_id)[:5]:
        idx = int(r.raw_ref[0].pointer.lstrip("/"))
        assert items[idx]["id"] == r.device.id
