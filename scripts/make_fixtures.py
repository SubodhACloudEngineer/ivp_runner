"""Turn the untracked captures in samples/ into sanitised, committable test fixtures.

    python scripts/make_fixtures.py

Reads:   samples/site_stats_devices_ap.json, samples/site.json, samples/site_wlans_derived.json
Writes:  tests/fixtures/mist/{site_stats_devices_ap.json, site.json, site_profile.yaml}

Every identifier is replaced consistently (the same input always maps to the
same output): names, UUIDs, MACs in any notation, serials, switch names, and
IPv4/IPv6 addresses. Signed download URLs, notes and the site address are
dropped. Structure and every number, status and boolean stay as captured.

IPv4: each real /16 maps to its own /16 inside 100.64.0.0/10, keeping the low
16 bits, so subnet membership (e.g. a /22) is preserved.

The script refuses to write anything if an original identifier survives.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
OUT = ROOT / "tests" / "fixtures" / "mist"

DROP_KEYS = {"image1_url", "image2_url", "image3_url", "address"}
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
MAC_SEP_RE = re.compile(r"\b[0-9a-f]{2}(?:[:-][0-9a-f]{2}){5}\b", re.I)
HEX_RE = re.compile(r"\b[0-9a-f]{12}(?:[0-9a-f]{8})?\b", re.I)  # bare MAC or 20-hex namespace
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
IPV6_LL_RE = re.compile(r"\bfe80(?::[0-9a-f]{0,4}){2,7}\b", re.I)
TOKEN_RE = re.compile(
    "|".join(
        f"(?P<{name}>{rx.pattern})"
        for name, rx in (
            ("dev_uuid", re.compile(r"\b00000000-0000-0000-1000-[0-9a-f]{12}\b")),
            ("uuid", UUID_RE),
            ("sep_mac", MAC_SEP_RE),
            ("ipv6", IPV6_LL_RE),
            ("ipv4", IPV4_RE),
            ("hex", HEX_RE),
        )
    ),
    re.I,
)


class Scrubber:
    def __init__(self) -> None:
        self.maps: dict[str, dict[str, str]] = {}
        self.originals: set[str] = set()

    def _map(self, kind: str, value: str, make) -> str:
        table = self.maps.setdefault(kind, {})
        if value not in table:
            table[value] = make(len(table) + 1, value)
            self.originals.add(value)
        return table[value]

    @staticmethod
    def _digest(value: str, n: int) -> str:
        return hashlib.sha256(value.encode()).hexdigest()[:n]

    def mac(self, hex12: str) -> str:
        """Bare 12-hex MAC -> locally administered fake (02:xx...)."""
        low = hex12.lower()
        return self._map("mac", low, lambda i, v: "02" + f"{i:010x}")

    def uuid(self, u: str) -> str:
        low = u.lower()
        return self._map("uuid", low, lambda i, v: f"{i:08x}-1111-4111-8111-{i:012x}")

    def ipv4(self, ip: str) -> str:
        addr = ipaddress.ip_address(ip)
        if addr.is_loopback or ip.startswith(("255.", "0.")):
            return ip  # netmasks, loopback and unspecified are not identifying

        def make(i, v):
            if i > 64:
                raise SystemExit("more than 64 distinct /16 networks; extend the mapping")
            return f"100.{63 + i}"

        net16 = ".".join(ip.split(".")[:2])
        new16 = self._map("net16", net16, make)
        self.originals.discard(net16)  # a /16 prefix alone is too short to leak-check
        self.originals.add(ip)
        return new16 + "." + ".".join(ip.split(".")[2:])

    def ipv6_ll(self, ip: str) -> str:
        return self._map("ipv6", ip.lower(), lambda i, v: f"fe80:0:0:0:0:0:0:{i:x}")

    def text(self, s: str) -> str:
        """Replace every identifier in one pass, so replacements are never re-scrubbed."""

        def one(m: re.Match) -> str:
            kind, tok = m.lastgroup, m.group(0)
            if kind == "dev_uuid":
                return tok[:24] + self.mac(tok[24:])
            if kind == "uuid":
                return self.uuid(tok)
            if kind == "sep_mac":
                return self._sep_mac(tok, tok[2])
            if kind == "ipv6":
                return self.ipv6_ll(tok)
            if kind == "ipv4":
                return self.ipv4(tok)
            return self._hex(tok)

        return TOKEN_RE.sub(one, s)

    def _sep_mac(self, mac: str, sep: str) -> str:
        fake = self.mac(re.sub(r"[:-]", "", mac))
        self.originals.add(mac.lower())
        return sep.join(fake[i : i + 2] for i in range(0, 12, 2))

    def _hex(self, h: str) -> str:
        if len(h) == 12:
            return self.mac(h)
        return self._map("hex20", h.lower(), lambda i, v: f"{i:020x}")


def scrub(obj, sc: Scrubber, names: dict[str, dict[str, str]], key: str = ""):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in DROP_KEYS:
                continue
            out[k] = scrub(v, sc, names, k)
        return out
    if isinstance(obj, list):
        return [scrub(v, sc, names, key) for v in obj]
    if isinstance(obj, str):
        if key in names and obj in names[key]:
            sc.originals.add(obj)
            return names[key][obj]
        if key == "notes":
            return ""
        return sc.text(obj)
    return obj


def build_name_maps(stats: list[dict], site: dict) -> dict[str, dict[str, str]]:
    ap_names = sorted({it["name"] for it in stats if "name" in it})
    switches = sorted({it.get("lldp_stat", {}).get("system_name") for it in stats} - {None, ""})
    profiles = sorted({it["deviceprofile_name"] for it in stats if it.get("deviceprofile_name")})
    serials = sorted({it["serial"] for it in stats if "serial" in it})
    return {
        "name": {n: f"AP-{i:03d}" for i, n in enumerate(ap_names, 1)} | {site["name"]: "Test Site"},
        "system_name": {n: f"SW-{i:02d}" for i, n in enumerate(switches, 1)},
        "deviceprofile_name": {n: f"Profile {chr(64 + i)}" for i, n in enumerate(profiles, 1)},
        "serial": {n: f"FAKESERIAL{i:04d}" for i, n in enumerate(serials, 1)},
    }


def main() -> int:
    stats = json.loads((SAMPLES / "site_stats_devices_ap.json").read_text(encoding="utf-8"))
    site = json.loads((SAMPLES / "site.json").read_text(encoding="utf-8"))
    wlans = json.loads((SAMPLES / "site_wlans_derived.json").read_text(encoding="utf-8"))

    sc = Scrubber()
    names = build_name_maps(stats, site)
    for table in names.values():
        sc.originals.update(table)
    out_stats = scrub(stats, sc, names)
    out_site = scrub(site, sc, names)
    out_site["latlng"] = {"lat": 0.0, "lng": 0.0}

    # Site profile with the same design values the verification run used,
    # mapped through the same scrubber so subnet membership still holds.
    mgmt = ipaddress.ip_network(
        f"{stats[0]['ip_stat']['ip']}/{stats[0]['ip_stat']['netmask']}", strict=False
    )
    profile = {
        "schema_version": 1,
        "site_id": sc.uuid(site["id"]),
        "site_class": "large",
        "expectations": {
            "uplink_port": "eth0",
            "ssids": [
                {"ssid": f"SSID {chr(65 + i)}", "bands": w["bands"]} for i, w in enumerate(wlans)
            ],
            "mgmt_subnet": f"{sc.ipv4(str(mgmt.network_address))}/{mgmt.prefixlen}",
            "dns_servers": [sc.ipv4(d) for d in stats[0]["ip_stat"]["dns"]],
            "min_uplink_speed_mbps": 1000,
        },
    }
    sc.originals.update(w["ssid"] for w in wlans)

    texts = {
        "site_stats_devices_ap.json": json.dumps(out_stats, indent=2, ensure_ascii=False) + "\n",
        "site.json": json.dumps(out_site, indent=2, ensure_ascii=False) + "\n",
        "site_profile.yaml": _yaml(profile),
    }
    leaks = _find_leaks(texts, sc.originals | {site["name"], site.get("address", "")})
    if leaks:
        print("REFUSING TO WRITE: original values survive:", file=sys.stderr)
        for f, vals in leaks.items():
            print(f"  {f}: {len(vals)} value(s), e.g. {sorted(vals)[:3]}", file=sys.stderr)
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in texts.items():
        (OUT / name).write_text(text, encoding="utf-8")
        print(f"wrote {(OUT / name).relative_to(ROOT)}")
    print(f"replaced {len(sc.originals)} distinct original values")
    return 0


def _find_leaks(texts: dict[str, str], originals: set[str]) -> dict[str, set[str]]:
    leaks: dict[str, set[str]] = {}
    for name, text in texts.items():
        low = text.lower()
        found = {o for o in originals if o and len(o) >= 4 and o.lower() in low}
        if "jwt" in low:
            found.add("jwt")
        if found:
            leaks[name] = found
    return leaks


def _yaml(profile: dict) -> str:
    import yaml

    header = (
        "# Sanitised site profile for tests, generated by scripts/make_fixtures.py.\n"
        "# Values mirror current Mist state of the captured site (mapped), NOT design intent.\n"
    )
    return header + yaml.safe_dump(profile, sort_keys=False)


if __name__ == "__main__":
    sys.exit(main())
