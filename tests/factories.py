"""Synthetic test data in the real Mist response shape. No customer values."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from pathlib import Path

from ivp_runner.catalogue import load_catalogue
from ivp_runner.evaluator import RunContext
from ivp_runner.methods import Payload
from ivp_runner.results import SiteRef
from ivp_runner.site_profile import SiteProfile

ROOT = Path(__file__).resolve().parent.parent
CATALOGUE = ROOT / "catalogue" / "ap.yaml"
T0 = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
T1 = datetime(2026, 7, 1, 10, 5, tzinfo=UTC)


def ap(n: int = 1, **overrides) -> dict:
    """A healthy, connected AP stats item. Overrides use dotted paths."""
    item = {
        "id": f"00000000-0000-0000-1000-{n:012x}",
        "name": f"AP-TEST-{n}",
        "mac": f"{n:012x}",
        "model": "AP-MODEL",
        "type": "ap",
        "status": "connected",
        "uptime": 100_000,
        "last_seen": 1_780_000_000,
        "num_wlans": 6,
        "power_constrained": False,
        "power_src": "LLDP",
        "power_needed": 20000,
        "power_avail": 25000,
        "power_budget": 5000,
        "ip_stat": {
            "ip": f"192.0.2.{n + 10}",
            "netmask": "255.255.255.0",
            "gateway": "192.0.2.1",
            "dns": ["192.0.2.53", "198.51.100.53"],
            "ips": {"vlan1": f"192.0.2.{n + 10}/24"},
        },
        "port_stat": {
            "eth0": {
                "up": True,
                "speed": 2500,
                "full_duplex": True,
                "rx_errors": 7,
                "rx_pkts": 1000,
                "tx_pkts": 900,
            },
        },
        "lldp_stat": {"ap_port_name": "eth0", "system_name": "SW-TEST", "port_id": "ge-0/0/1"},
        "radio_stat": {
            "band_24": {"num_wlans": 1, "power": 10},
            "band_5": {"num_wlans": 2, "power": 14},
            "band_6": {"num_wlans": 1, "power": 14},
        },
    }
    for path, value in overrides.items():
        set_path(item, path, value)
    return item


def disconnected(n: int = 9) -> dict:
    """What Mist returns for a disconnected AP: identity only, no runtime fields."""
    return {
        "id": f"00000000-0000-0000-1000-{n:012x}",
        "name": f"AP-TEST-{n}",
        "mac": f"{n:012x}",
        "model": "AP-MODEL",
        "type": "ap",
        "status": "disconnected",
    }


_DELETE = object()
DELETE = _DELETE


def set_path(item: dict, path: str, value) -> None:
    *parents, last = path.split("__") if "__" in path else path.split(".")
    for p in parents:
        item = item.setdefault(p, {})
    if value is _DELETE:
        item.pop(last, None)
    else:
        item[last] = value


def profile(**expectations) -> SiteProfile:
    base = {
        "uplink_port": "eth0",
        "ssids": [{"ssid": "Corp", "bands": ["5", "6"]}, {"ssid": "Guest", "bands": ["24", "5"]}],
        "mgmt_subnet": "192.0.2.0/24",
        "dns_servers": ["198.51.100.53", "192.0.2.53"],
        "min_uplink_speed_mbps": 1000,
    }
    base.update(expectations)
    base = {k: v for k, v in base.items() if v is not None}
    return SiteProfile.model_validate(
        {"schema_version": 1, "site_id": "site-1", "site_class": "large", "expectations": base}
    )


def payload(items: list[dict], sample: str = "start", at: datetime = T0) -> Payload:
    return Payload(
        method="mist_api",
        source="site_device_stats",
        sample=sample,
        items=copy.deepcopy(items),
        collected_at=at,
        raw_path=f"out/run-1/raw/site_device_stats.{sample}.json",
        raw_sha256="a" * 64,
        identity={"id": "id", "name": "name", "mac": "mac", "model": "model"},
    )


def payloads(start: list[dict], end: list[dict] | None = None) -> dict:
    samples = {"start": payload(start)}
    samples["end"] = payload(end if end is not None else start, "end", T1)
    return {("mist_api", "site_device_stats"): samples}


def context(out_dir: str = "out/run-1") -> RunContext:
    return RunContext(
        run_id="run-1",
        site=SiteRef(id="site-1", name="Test Site", **{"class": "large"}),
        timezone="Europe/Madrid",
        out_dir=out_dir,
        started_at=T0,
    )


def catalogue():
    return load_catalogue(CATALOGUE)
