import copy
from pathlib import Path

import pytest
import yaml

from ivp_runner.catalogue import CatalogueError, load_catalogue, parse_catalogue

CATALOGUE = Path(__file__).resolve().parent.parent / "catalogue" / "ap.yaml"


@pytest.fixture
def raw():
    return yaml.safe_load(CATALOGUE.read_text(encoding="utf-8"))


def test_shipped_catalogue_loads():
    cat = load_catalogue(CATALOGUE)
    assert [c.id for c in cat.checks] == ["AP-00", "AP-01", "AP-02", "AP-03", "AP-04", "AP-05"]
    assert cat.uplink_port == "eth0"


def test_ap00_gates_every_other_ap_check():
    cat = load_catalogue(CATALOGUE)
    for test_id in ("AP-01", "AP-02", "AP-03", "AP-04", "AP-05"):
        assert cat.preconditions_of(test_id) == ("AP-00",)
    assert cat.preconditions_of("AP-00") == ()


def test_partial_checks_carry_a_limitation():
    cat = load_catalogue(CATALOGUE)
    partial = {c.id for c in cat.checks if c.coverage == "partial"}
    assert partial == {"AP-01", "AP-03", "AP-05"}
    assert all(cat.get(i).limitation for i in partial)


def test_ap03_names_say_subnet_and_dns_configuration():
    ap03 = load_catalogue(CATALOGUE).get("AP-03")
    names = [a.name for a in ap03.assertions]
    assert names == ["mgmt_ip_in_expected_subnet", "dns_servers_match_expected_configuration"]
    assert "vlan" not in ap03.title.lower()
    assert "client probe" in ap03.limitation


def test_unset_expectations_are_reported_per_check():
    cat = load_catalogue(CATALOGUE)
    assert cat.missing_expectations("AP-02") == ()
    assert cat.missing_expectations("AP-03") == ("mgmt_subnet", "dns_servers")


def test_expectations_parse(raw):
    raw["expectations"] = {
        "ssids": [{"ssid": "A", "bands": ["24", "5"]}, {"ssid": "B", "bands": ["5", "6"]}],
        "mgmt_subnet": "192.0.2.0/24",
        "dns_servers": ["192.0.2.53", "198.51.100.53"],
        "min_uplink_speed_mbps": 1000,
    }
    exp = parse_catalogue(raw).expectations
    assert exp.ssid_count_per_band() == {"band_24": 1, "band_5": 2, "band_6": 1}
    assert str(exp.mgmt_subnet) == "192.0.2.0/24"
    assert exp.dns_servers == {"198.51.100.53", "192.0.2.53"}


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda r: r["checks"].append(copy.deepcopy(r["checks"][1])), "duplicate test IDs"),
        (lambda r: r["checks"][1].update(id="AP1"), "does not match"),
        (lambda r: r["checks"][1].pop("limitation"), "partial coverage requires a limitation"),
        (lambda r: r["checks"][0].update(precondition_for=["AP-99"]), "unknown test IDs"),
        (lambda r: r["checks"][1].update(surprise=1), "unknown keys"),
        (lambda r: r["checks"][2].update(uses_expectations=["vlan"]), "unknown keys"),
        (lambda r: r["expectations"].update(mgmt_subnet="10.0.0.1/33"), "mgmt_subnet"),
        (lambda r: r["expectations"].update(dns_servers=["dns.example"]), "not an IP"),
        (lambda r: r["expectations"].update(ssids=[{"ssid": "A", "bands": ["7"]}]), "bands"),
        (lambda r: r["expectations"].update(min_uplink_speed_mbps=True), "positive int"),
        (lambda r: r.update(schema_version=2), "schema_version"),
    ],
)
def test_malformed_catalogue_is_rejected(raw, mutate, message):
    mutate(raw)
    with pytest.raises(CatalogueError, match=message):
        parse_catalogue(raw)
