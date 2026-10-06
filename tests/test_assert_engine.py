import json

import pytest

from ivp_runner.assert_engine import evaluate, write_evidence, write_results
from ivp_runner.catalogue import Catalogue
from ivp_runner.results import Reason, TestResult, Verdict

from .factories import DELETE, ap, catalogue, context, disconnected, payloads, profile


def run(start, end=None, prof=None, cat=None):
    evs = evaluate(cat or catalogue(), prof or profile(), *payloads(start, end), context())
    return {(e.result.test_id, e.result.device.id if e.result.device else None): e for e in evs}


def verdicts(results, device_id):
    return {t: e.result.verdict for (t, d), e in results.items() if d == device_id}


DEV = ap(1)["id"]


def test_healthy_ap_passes_every_check():
    res = run([ap(1)])
    assert verdicts(res, DEV) == {f"AP-0{i}": Verdict.PASS for i in range(6)}


def test_disconnected_ap_gives_one_fail_and_five_skips():
    d = disconnected(9)
    res = run([ap(1), d])
    got = verdicts(res, d["id"])
    assert got.pop("AP-00") is Verdict.FAIL
    assert set(got.values()) == {Verdict.SKIP} and len(got) == 5
    skip = res[("AP-02", d["id"])].result
    assert skip.reason is Reason.PRECONDITION_FAILED and "AP-00" in skip.message
    # missing runtime fields under `present` are a FAIL of AP-00, not an ERROR
    ap00 = res[("AP-00", d["id"])].result
    assert ap00.reason is Reason.CRITERIA_NOT_MET


@pytest.mark.parametrize(
    "test_id, override, value",
    [
        ("AP-01", "radio_stat.band_5.num_wlans", 0),
        ("AP-02", "power_constrained", True),
        ("AP-03", "ip_stat.ip", "203.0.113.5"),
        ("AP-03", "ip_stat.dns", ["192.0.2.53"]),
        ("AP-04", "port_stat.eth0.speed", 100),
        ("AP-04", "port_stat.eth0.full_duplex", False),
    ],
)
def test_each_check_fails_on_its_own_condition(test_id, override, value):
    res = run([ap(1, **{override: value})])
    r = res[(test_id, DEV)].result
    assert r.verdict is Verdict.FAIL and r.reason is Reason.CRITERIA_NOT_MET
    others = {t: v for t, v in verdicts(res, DEV).items() if t != test_id}
    assert set(others.values()) == {Verdict.PASS}


def test_ap01_ignores_disabled_band_and_fails_when_all_disabled():
    one_off = ap(1, **{"radio_stat.band_24": {"num_wlans": 0, "power": 0, "disabled": True}})
    assert run([one_off])[("AP-01", DEV)].result.verdict is Verdict.PASS

    radios = {b: {"num_wlans": 0, "disabled": True} for b in ("band_24", "band_5")}
    all_off = ap(1, radio_stat=radios)
    r = run([all_off])[("AP-01", DEV)].result
    assert r.verdict is Verdict.FAIL
    assert "only 0 band(s)" in r.message


def test_ap01_ap_broadcasting_nothing_fails():
    radios = {b: {"num_wlans": 0, "power": 12} for b in ("band_24", "band_5")}
    assert run([ap(1, radio_stat=radios)])[("AP-01", DEV)].result.verdict is Verdict.FAIL


@pytest.mark.parametrize(
    "end_overrides, verdict, reason",
    [
        ({}, Verdict.PASS, None),
        ({"port_stat.eth0.rx_errors": 9}, Verdict.FAIL, Reason.CRITERIA_NOT_MET),
        ({"port_stat.eth0.rx_errors": 0, "uptime": 50}, Verdict.ERROR, Reason.COUNTER_RESET),
        ({"port_stat.eth0.rx_errors": 3}, Verdict.ERROR, Reason.COUNTER_RESET),
        # rebooted and counted past the start value: uptime still exposes it
        ({"port_stat.eth0.rx_errors": 20, "uptime": 10}, Verdict.ERROR, Reason.COUNTER_RESET),
    ],
)
def test_ap05_delta_rules(end_overrides, verdict, reason):
    r = run([ap(1)], [ap(1, **end_overrides)])[("AP-05", DEV)].result
    assert (r.verdict, r.reason) == (verdict, reason)
    assert r.actual["port_stat.eth0.rx_errors"]["start"] == 7
    assert [ref.sample for ref in r.raw_ref] == ["start", "end"]


def test_ap05_device_missing_from_end_sample_is_error():
    r = run([ap(1)], [])[("AP-05", DEV)].result
    assert (r.verdict, r.reason) == (Verdict.ERROR, Reason.SAMPLE_MISSING)


def test_missing_expectation_is_error_not_fail():
    res = run([ap(1)], prof=profile(mgmt_subnet=None))
    r = res[("AP-03", DEV)].result
    assert (r.verdict, r.reason) == (Verdict.ERROR, Reason.EXPECTATION_MISSING)
    # the DNS half was still evaluated and is visible
    assert [a.verdict for a in r.assertions] == [Verdict.ERROR, Verdict.PASS]


def test_missing_vars_expectation_is_site_wide_error():
    res = run([ap(1)], prof=profile(uplink_port=None))
    r = res[("AP-04", None)].result
    assert (r.verdict, r.reason, r.device) == (Verdict.ERROR, Reason.EXPECTATION_MISSING, None)


def test_missing_field_is_error_not_fail():
    r = run([ap(1, power_constrained=DELETE)])
    # AP-00 requires the field (present -> FAIL), so AP-02 is SKIP.
    assert r[("AP-00", DEV)].result.verdict is Verdict.FAIL
    assert r[("AP-02", DEV)].result.verdict is Verdict.SKIP


def test_wrong_type_is_bad_data_error():
    r = run([ap(1, power_constrained="no")])[("AP-02", DEV)].result
    assert (r.verdict, r.reason) == (Verdict.ERROR, Reason.BAD_DATA)


def test_unimplemented_method_is_error():
    cat = catalogue().model_dump(exclude_unset=True)
    cat["checks"][2]["collect"]["method"] = "ssh"
    res = run([ap(1)], cat=Catalogue.model_validate(cat))
    r = res[("AP-02", None)].result
    assert (r.verdict, r.reason) == (Verdict.ERROR, Reason.METHOD_UNAVAILABLE)


def test_missing_payload_is_api_error():
    evs = evaluate(catalogue(), profile(), {}, None, context())
    assert {(e.result.verdict, e.result.reason) for e in evs} == {(Verdict.ERROR, Reason.API_ERROR)}
    assert len(evs) == 6


def test_site_class_filter_emits_no_result():
    cat = catalogue().model_dump(exclude_unset=True)
    cat["checks"][2]["site_classes"] = ["small"]
    res = run([ap(1)], cat=Catalogue.model_validate(cat))
    assert ("AP-02", DEV) not in res


def test_non_targets_are_ignored():
    switch = ap(2, type="switch")
    res = run([ap(1), switch])
    assert all(d != switch["id"] for (_, d) in res)


def test_result_record_traces_to_raw_payload_and_evidence(tmp_path):
    evs = evaluate(catalogue(), profile(), *payloads([ap(1), ap(2)]), context(str(tmp_path)))
    r = next(
        e.result for e in evs if e.result.test_id == "AP-02" and e.result.device.name == "AP-TEST-2"
    )
    assert r.raw_ref[0].pointer == "/1"
    assert r.timestamp_utc == "2026-07-01T10:00:00Z"
    assert r.timestamp_local == "2026-07-01T12:00:00+02:00"
    assert r.expected == {"power_constrained": False}
    assert r.actual == {"power_constrained": False}

    assert write_evidence(evs) == 12
    evidence = json.loads(open(r.evidence_path).read())
    assert evidence["fields"]["power_src"] == "LLDP"
    ap05 = next(e for e in evs if e.result.test_id == "AP-05")
    assert json.loads(open(ap05.result.evidence_path).read())["fields"][
        "port_stat.eth0.rx_errors"
    ] == {
        "start": 7,
        "end": 7,
    }

    out = tmp_path / "results.json"
    write_results(evs, out)
    records = json.loads(out.read_text())
    assert len(records) == 12
    assert all(TestResult.model_validate(rec) for rec in records)
