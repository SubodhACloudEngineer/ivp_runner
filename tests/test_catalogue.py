import copy

import pytest
import yaml
from pydantic import ValidationError

from ivp_runner.catalogue import (
    Catalogue,
    explain,
    inventory_paths,
    load_catalogue,
    unknown_field_paths,
)
from ivp_runner.criteria import describe
from ivp_runner.site_profile import MissingExpectation, load_site_profile

from .factories import CATALOGUE, ROOT, profile


@pytest.fixture
def raw():
    return yaml.safe_load(CATALOGUE.read_text(encoding="utf-8"))


def test_shipped_catalogue_loads_in_dependency_order():
    cat = load_catalogue(CATALOGUE)
    assert [c.test_id for c in cat.ordered()] == [f"AP-0{i}" for i in range(6)]
    assert all(c.requires == ["AP-00"] for c in cat.checks[1:])


def test_every_field_path_is_in_the_inventory():
    inventory = inventory_paths((ROOT / "docs" / "field_inventory.md").read_text(encoding="utf-8"))
    assert len(inventory) > 30
    assert unknown_field_paths(load_catalogue(CATALOGUE), inventory) == {}


def test_partial_checks_and_mop_descriptions():
    cat = load_catalogue(CATALOGUE)
    assert {c.test_id for c in cat.checks if c.coverage == "partial"} == {"AP-01", "AP-03", "AP-05"}
    assert all(c.mop.section == "Verify AP" for c in cat.checks)
    assert cat.get("AP-02").mop.description == "Check the Power mode:"


# The sentences a reviewer signs off. Changing a criterion must change these.
EXPECTED_RULES = {
    "AP-00": [
        'status equals "connected"',
        "radio_stat is reported",
        "power_constrained is reported",
        "ip_stat.ip is reported",
        "ip_stat.netmask is reported",
        "ip_stat.dns is reported",
        "port_stat.<port>.up is reported",
        "port_stat.<port>.rx_errors is reported",
        "uptime is reported",
    ],
    "AP-01": [
        "For each band in radio_stat (ignoring any where disabled equals true):",
        "  radio_stat.<band>.num_wlans equals the site's expected ssid_count.<band>",
        "  at least 1 band(s) must be checked",
    ],
    "AP-02": ["power_constrained equals false"],
    "AP-03": [
        "ip_stat.ip is an address inside the site's expected mgmt_subnet",
        "ip_stat.dns contains exactly the same entries as the site's expected dns_servers"
        " (any order)",
    ],
    "AP-04": [
        "port_stat.<port>.up equals true",
        "port_stat.<port>.speed is at least the site's expected min_uplink_speed_mbps",
        "port_stat.<port>.full_duplex equals true",
    ],
    "AP-05": [
        "port_stat.<port>.rx_errors is the same at run start and run end "
        "(if uptime went down, the device rebooted: ERROR, not PASS)"
    ],
}


@pytest.mark.parametrize("test_id", sorted(EXPECTED_RULES))
def test_criteria_render_as_reviewed_sentences(test_id):
    assert describe(load_catalogue(CATALOGUE).get(test_id).pass_when) == EXPECTED_RULES[test_id]


def test_explain_mentions_limitations_and_preconditions():
    text = explain(load_catalogue(CATALOGUE))
    assert "Only if PASS: AP-00 (otherwise SKIP)" in text
    assert "not proof that DNS works" in text


def test_adding_a_check_is_yaml_only(raw):
    raw["checks"].append(
        {
            "test_id": "AP-06",
            "title": "AP uplink has an LLDP neighbour",
            "mop": {"section": "Verify AP", "description": "new row"},
            "site_classes": ["large"],
            "coverage": "full",
            "requires": ["AP-00"],
            "collect": {"method": "mist_api", "source": "site_device_stats"},
            "target": {"kind": "ap", "select": {"field": "type", "equals": "ap"}},
            "pass_when": {"all": [{"field": "lldp_stat.system_name", "present": True}]},
        }
    )
    cat = Catalogue.model_validate(raw)
    assert cat.ordered()[-1].test_id == "AP-06"


def _mutations():
    def check(i):
        return lambda r: r["checks"][i]

    return [
        (lambda r: r["checks"].append(copy.deepcopy(r["checks"][1])), "duplicate test IDs"),
        (lambda r: check(1)(r).update(test_id="AP1"), "test_id"),
        (lambda r: check(1)(r).pop("limitation"), "partial coverage requires a limitation"),
        (lambda r: check(1)(r).update(requires=["AP-99"]), "unknown test IDs"),
        (lambda r: check(0)(r).update(requires=["AP-02"]), "requires cycle"),
        (lambda r: check(1)(r).update(surprise=1), "Extra inputs"),
        (lambda r: check(2)(r).update(site_classes=["huge"]), "site_classes"),
        (lambda r: check(2)(r).update(site_classes=["all", "large"]), "'all' cannot be combined"),
        (lambda r: check(2)(r).update(collect={"method": "snmp", "source": "x"}), "method"),
        (
            lambda r: check(2)(r)["pass_when"]["all"].__setitem__(0, {"field": "x", "greater": 1}),
            "pass_when",
        ),
        (
            lambda r: check(2)(r)["pass_when"]["all"].__setitem__(
                0, {"field": "x", "equals": 1, "at_least": 1}
            ),
            "exactly one operator",
        ),
        (lambda r: check(5)(r)["collect"].update(sampling="once"), "unchanged_during_run requires"),
        (lambda r: check(4)(r).pop("vars"), "placeholders without a var"),
        (lambda r: r.update(schema_version=1), "schema_version"),
    ]


@pytest.mark.parametrize("mutate, message", _mutations())
def test_malformed_catalogue_is_rejected(raw, mutate, message):
    mutate(raw)
    with pytest.raises(ValidationError, match=message):
        Catalogue.model_validate(raw)


def test_example_site_profile_and_derived_counts():
    p = load_site_profile(ROOT / "sites" / "example.yaml")
    assert [p.lookup(f"ssid_count.band_{b}") for b in ("24", "5", "6")] == [1, 2, 1]
    assert p.lookup("dns_servers") == ["192.0.2.53", "198.51.100.53"]


def test_site_profile_missing_and_invalid_values():
    with pytest.raises(MissingExpectation):
        profile(mgmt_subnet=None).lookup("mgmt_subnet")
    with pytest.raises(MissingExpectation):
        profile(ssids=None).lookup("ssid_count.band_5")
    with pytest.raises(ValidationError):
        profile(dns_servers=["dns.example"])
    with pytest.raises(ValidationError):
        profile(min_uplink_speed_mbps=0)
