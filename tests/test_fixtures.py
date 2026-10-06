"""Guards on the committed fixtures: they must stay sanitised."""

import ipaddress
import re

from .fixtures_mist import FIXTURES, stats_items


def test_fixtures_contain_no_signed_urls_or_real_addresses():
    for f in FIXTURES.iterdir():
        text = f.read_text(encoding="utf-8")
        assert "jwt" not in text.lower() and "mist.com" not in text, f.name
        for ip in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text):
            if ip.startswith(("255.", "127.", "0.")):
                continue
            assert ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10"), (f.name, ip)


def test_fixture_identities_are_pseudonyms():
    items = stats_items()
    assert len(items) == 91
    assert all(re.fullmatch(r"AP-\d{3}", it["name"]) for it in items)
    assert all(re.fullmatch(r"FAKESERIAL\d{4}", it["serial"]) for it in items)
    assert all(it["mac"].startswith("02") for it in items)  # locally administered fakes
    names = {it.get("lldp_stat", {}).get("system_name") for it in items} - {None}
    assert all(re.fullmatch(r"SW-\d{2}", n) for n in names)
