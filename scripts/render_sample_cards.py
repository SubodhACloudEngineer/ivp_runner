"""Render sample evidence cards from the sanitised test fixtures.

    python scripts/render_sample_cards.py [out_dir]     (default: out/evidence_samples)

Runs the real assertion engine over tests/fixtures/mist (91 captured APs,
sanitised). Two small changes produce failures the capture lacks: one AP
takes new RX errors during the run (AP-05), and one AP has a DNS server
that isn't expected (AP-03). Then it renders a PASS and a FAIL card for each
check, plus a SKIP. No network, no customer data.
"""

from __future__ import annotations

import copy
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ivp_runner.assert_engine import RunContext, evaluate  # noqa: E402
from ivp_runner.catalogue import load_catalogue  # noqa: E402
from ivp_runner.collectors import Payload  # noqa: E402
from ivp_runner.collectors.mist_api import SOURCES  # noqa: E402
from ivp_runner.evidence import render_card  # noqa: E402
from ivp_runner.results import SiteRef, Verdict  # noqa: E402
from ivp_runner.site_profile import load_site_profile  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "mist"
T0 = datetime(2026, 9, 29, 13, 55, 44, tzinfo=UTC)
T1 = datetime(2026, 9, 29, 14, 0, 12, tzinfo=UTC)


def payload(items: list, sample: str, at: datetime, run_dir: Path) -> Payload:
    return Payload(
        method="mist_api",
        source="site_device_stats",
        sample=sample,
        data=items,
        collected_at=at,
        raw_path=str(run_dir / "raw" / f"site_device_stats.{sample}.json"),
        raw_sha256="0" * 64,
        identity=dict(SOURCES["site_device_stats"].identity),
    )


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "out" / "evidence_samples"
    out.mkdir(parents=True, exist_ok=True)
    items = json.loads((FIX / "site_stats_devices_ap.json").read_text(encoding="utf-8"))
    site = json.loads((FIX / "site.json").read_text(encoding="utf-8"))
    profile = load_site_profile(FIX / "site_profile.yaml")
    catalogue = load_catalogue(ROOT / "catalogue" / "ap.yaml")

    connected = [it for it in items if it.get("status") == "connected"]
    errs_ap, dns_ap = connected[3], connected[7]
    dns_ap["ip_stat"]["dns"] = [*dns_ap["ip_stat"]["dns"][:2], "100.99.1.1"]
    errs_ap["port_stat"]["eth0"]["rx_errors"] = 0
    end_items = copy.deepcopy(items)
    next(it for it in end_items if it["id"] == errs_ap["id"])["port_stat"]["eth0"]["rx_errors"] = 4

    run_dir = Path("out/run-sample")
    ctx = RunContext(
        run_id="run-sample",
        site=SiteRef(id=site["id"], name=site["name"], **{"class": profile.site_class}),
        timezone=site["timezone"],
        out_dir=str(run_dir),
        started_at=T0,
    )
    evs = evaluate(
        catalogue,
        profile,
        {"site_device_stats": payload(items, "start", T0, run_dir)},
        {"site_device_stats": payload(end_items, "end", T1, run_dir)},
        ctx,
    )
    by_id = {c.test_id: c for c in catalogue.checks}

    wanted: list[tuple[str, Verdict]] = [("AP-00", Verdict.FAIL), ("AP-02", Verdict.SKIP)]
    for t in ("AP-01", "AP-02", "AP-03", "AP-04", "AP-05"):
        wanted += [(t, Verdict.PASS), (t, Verdict.FAIL)]

    for test_id, verdict in wanted:
        r = next(
            e.result for e in evs if e.result.test_id == test_id and e.result.verdict is verdict
        )
        png = render_card(r, by_id[test_id])
        path = out / f"{test_id}_{verdict.value}_{r.device.name if r.device else 'site'}.png"
        path.write_bytes(png)
        shown = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
        print(f"{shown}  {len(png) / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
