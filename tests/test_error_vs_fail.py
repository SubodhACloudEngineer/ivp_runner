"""The ERROR-vs-FAIL contract, operator by operator.

FAIL means the device was judged and did not meet the criterion. ERROR means
it could not be judged: a field, expectation or sample is missing, or the data
has the wrong shape. A FAIL is a finding about the network; an ERROR is a gap
in the evidence, and it must never be reported as a finding.

One table, one row per case, so the contract can be read top to bottom.
"""

import pytest

from ivp_runner.assert_engine import evaluate
from ivp_runner.criteria import Criterion, Samples
from ivp_runner.criteria import evaluate as evaluate_criterion
from ivp_runner.results import Reason, Verdict, rollup
from ivp_runner.site_profile import SiteProfile

from .factories import ap, catalogue, context, payloads, profile

PASS, FAIL, ERROR = Verdict.PASS, Verdict.FAIL, Verdict.ERROR
R = Reason
NOT_MET = Reason.CRITERIA_NOT_MET

PROFILE = SiteProfile.model_validate(
    {
        "schema_version": 1,
        "site_id": "s",
        "site_class": "large",
        "expectations": {
            "uplink_port": "eth0",
            "mgmt_subnet": "10.0.0.0/24",
            "dns_servers": ["10.0.0.53", "10.0.0.54"],
            "min_uplink_speed_mbps": 1000,
        },
    }
)
NO_EXPECTATIONS = SiteProfile.model_validate(
    {"schema_version": 1, "site_id": "s", "site_class": "large", "expectations": {}}
)


def run(assertion: dict, start: dict, end: dict | None = None, prof=PROFILE, vars_=None):
    crit = Criterion.model_validate({"all": [assertion]})
    (outcome,) = evaluate_criterion(crit, Samples(start, end, vars_ or {}), prof)
    return outcome


EQ = {"field": "up", "equals": True}
AT_LEAST = {"field": "speed", "at_least": {"expect": "min_uplink_speed_mbps"}}
SUBNET = {"field": "ip", "in_subnet": {"expect": "mgmt_subnet"}}
SAME_SET = {"field": "dns", "same_set_as": {"expect": "dns_servers"}}
PRESENT = {"field": "uptime", "present": True}
UNCHANGED = {"field": "rx_errors", "unchanged_during_run": True}
GUARDED = {**UNCHANGED, "reset_guard": "uptime"}

CASES = [
    # equals
    ("equals: matches", EQ, {"up": True}, None, PASS, None),
    ("equals: differs", EQ, {"up": False}, None, FAIL, NOT_MET),
    ("equals: field absent", EQ, {}, None, ERROR, R.FIELD_MISSING),
    ("equals: string where bool expected", EQ, {"up": "true"}, None, ERROR, R.BAD_DATA),
    (
        "equals: string where number expected",
        {"field": "speed", "equals": 1000},
        {"speed": "1000"},
        None,
        ERROR,
        R.BAD_DATA,
    ),
    # at_least
    ("at_least: equal", AT_LEAST, {"speed": 1000}, None, PASS, None),
    ("at_least: below", AT_LEAST, {"speed": 100}, None, FAIL, NOT_MET),
    ("at_least: not a number", AT_LEAST, {"speed": "1G"}, None, ERROR, R.BAD_DATA),
    ("at_least: field absent", AT_LEAST, {}, None, ERROR, R.FIELD_MISSING),
    # in_subnet
    ("in_subnet: inside", SUBNET, {"ip": "10.0.0.5"}, None, PASS, None),
    ("in_subnet: outside", SUBNET, {"ip": "10.0.1.5"}, None, FAIL, NOT_MET),
    ("in_subnet: not an address", SUBNET, {"ip": "dhcp"}, None, ERROR, R.BAD_DATA),
    ("in_subnet: wrong type", SUBNET, {"ip": 5}, None, ERROR, R.BAD_DATA),
    # same_set_as
    ("same_set_as: any order", SAME_SET, {"dns": ["10.0.0.54", "10.0.0.53"]}, None, PASS, None),
    ("same_set_as: one wrong", SAME_SET, {"dns": ["10.0.0.53", "8.8.8.8"]}, None, FAIL, NOT_MET),
    ("same_set_as: not a list", SAME_SET, {"dns": "10.0.0.53"}, None, ERROR, R.BAD_DATA),
    # present: absence IS the finding here, so it is FAIL, not ERROR
    ("present: reported", PRESENT, {"uptime": 1}, None, PASS, None),
    ("present: not reported", PRESENT, {}, None, FAIL, NOT_MET),
    # unchanged_during_run
    ("unchanged: same", UNCHANGED, {"rx_errors": 7}, {"rx_errors": 7}, PASS, None),
    ("unchanged: grew", UNCHANGED, {"rx_errors": 7}, {"rx_errors": 9}, FAIL, NOT_MET),
    ("unchanged: backwards", UNCHANGED, {"rx_errors": 7}, {"rx_errors": 0}, ERROR, R.COUNTER_RESET),
    (
        "unchanged: rebooted (uptime fell)",
        GUARDED,
        {"rx_errors": 7, "uptime": 900},
        {"rx_errors": 9, "uptime": 5},
        ERROR,
        R.COUNTER_RESET,
    ),
    ("unchanged: no end sample", UNCHANGED, {"rx_errors": 7}, None, ERROR, R.SAMPLE_MISSING),
    ("unchanged: absent at end", UNCHANGED, {"rx_errors": 7}, {}, ERROR, R.FIELD_MISSING),
    ("unchanged: not a number", UNCHANGED, {"rx_errors": "7"}, {"rx_errors": 9}, ERROR, R.BAD_DATA),
]  # fmt: skip


@pytest.mark.parametrize(
    "assertion,start,end,verdict,reason", [c[1:] for c in CASES], ids=[c[0] for c in CASES]
)
def test_operator_verdicts(assertion, start, end, verdict, reason):
    o = run(assertion, start, end)
    assert (o.verdict, o.reason) == (verdict, reason)
    if verdict is FAIL:
        assert o.actual is not None or assertion is PRESENT  # a FAIL shows what was seen
    if verdict is ERROR:
        assert o.message  # an ERROR always says why it could not judge


@pytest.mark.parametrize("assertion", [AT_LEAST, SUBNET, SAME_SET], ids=lambda a: a["field"])
def test_blank_expectation_is_error_whatever_the_device_reports(assertion):
    """A blank site profile must never turn into a FAIL against the AP."""
    for start in ({"speed": 1, "ip": "1.1.1.1", "dns": []}, {}):
        o = run(assertion, start, prof=NO_EXPECTATIONS)
        assert (o.verdict, o.reason) == (ERROR, Reason.EXPECTATION_MISSING)


def test_unfilled_placeholder_is_expectation_missing():
    o = run({"field": "port_stat.<port>.up", "equals": True}, {"port_stat": {}}, vars_={})
    assert (o.verdict, o.reason) == (ERROR, Reason.EXPECTATION_MISSING)


def _for_each(start):
    crit = Criterion.model_validate(
        {
            "all": [
                {
                    "for_each": "radio_stat.<band>",
                    "skip_if": {"field": "disabled", "equals": True},
                    "all": [{"field": "num_wlans", "at_least": 1}],
                    "min_evaluated": 1,
                }
            ]
        }
    )
    return evaluate_criterion(crit, Samples(start), PROFILE)


@pytest.mark.parametrize(
    "start,verdict,reason",
    [
        ({"radio_stat": {"band_5": {"num_wlans": 2}}}, PASS, None),
        ({"radio_stat": {"band_5": {"num_wlans": 0}}}, FAIL, Reason.CRITERIA_NOT_MET),
        ({"radio_stat": {"band_5": {"disabled": True}}}, FAIL, Reason.CRITERIA_NOT_MET),
        ({}, ERROR, Reason.FIELD_MISSING),
        ({"radio_stat": ["band_5"]}, ERROR, Reason.BAD_DATA),
    ],
    ids=["one band ok", "band broadcasts nothing", "all bands disabled", "no radios", "bad shape"],
)
def test_for_each_verdicts(start, verdict, reason):
    outcomes = _for_each(start)
    got = rollup(o.verdict for o in outcomes)
    assert got is verdict
    assert next((o.reason for o in outcomes if o.verdict is got), None) == reason


# ---------------------------------------------------------------- whole-check level


def _one(test_id, start, end=None, prof=None):
    evs = evaluate(catalogue(), prof or profile(), *payloads(start, end), context())
    (r,) = [e.result for e in evs if e.result.test_id == test_id]
    return r


def test_check_with_a_proven_defect_and_an_unjudgeable_field_is_fail_and_says_both():
    """Within one check FAIL outranks ERROR (a proven defect is reported), and the
    message still names the assertion that could not be judged."""
    item = ap(1, **{"port_stat.eth0.up": False})
    del item["port_stat"]["eth0"]["speed"]
    r = _one("AP-04", [item])
    assert (r.verdict, r.reason) == (FAIL, Reason.CRITERIA_NOT_MET)
    assert "speed" in r.message and "not reported" in r.message


def test_fail_reason_is_always_criteria_not_met_and_error_never_is():
    disconnected = ap(9, status="disconnected")
    items = [
        ap(1),
        ap(2, power_constrained=True),
        ap(3, **{"port_stat.eth0.speed": "x"}),
        disconnected,
    ]
    evs = evaluate(catalogue(), profile(mgmt_subnet=None), *payloads(items), context())
    seen = set()
    for e in evs:
        r = e.result
        seen.add(r.verdict)
        if r.verdict is FAIL:
            assert r.reason is Reason.CRITERIA_NOT_MET
        elif r.verdict is ERROR:
            assert r.reason not in (None, Reason.CRITERIA_NOT_MET) and r.message
        elif r.verdict is PASS:
            assert r.reason is None
    assert {PASS, FAIL, ERROR, Verdict.SKIP} <= seen
