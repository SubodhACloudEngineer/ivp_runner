"""Throwaway: capture raw Mist API responses into samples/ for field discovery.

READ-ONLY. Every request below is a GET. The token is read from MIST_API_TOKEN,
sent only in the Authorization header, and never printed or written to disk
(it is scrubbed from any response body that happens to echo it).

Usage:
    MIST_API_TOKEN=... python scripts/capture_samples.py \
        --org-id <org_uuid> --site-id <site_uuid> [--api-host api.mist.com]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"
TIMEOUT = 30
MAX_PER_DEVICE = 3  # per-AP detail calls; enough to see shape, not a full sweep


def site_endpoints(org_id: str, site_id: str) -> list[tuple[str, str, dict]]:
    """(slug, path, params) for every GET we capture. Rationale per check in comments."""
    return [
        # AP-01..05: primary source -- runtime stats for every AP on the site.
        (
            "site_stats_devices_ap",
            f"/api/v1/sites/{site_id}/stats/devices",
            {"type": "ap", "limit": 1000},
        ),
        # AP-03: configured IP / VLAN intent lives on the device config, not stats.
        ("site_devices_ap", f"/api/v1/sites/{site_id}/devices", {"type": "ap", "limit": 1000}),
        # AP-01: the "expected SSIDs" -- site WLANs plus those inherited from
        # org templates. derived = effective set for this site.
        ("site_wlans_derived", f"/api/v1/sites/{site_id}/wlans/derived", {}),
        ("site_wlans", f"/api/v1/sites/{site_id}/wlans", {}),
        ("org_wlans", f"/api/v1/orgs/{org_id}/wlans", {}),
        # Timestamps convention: site timezone for local rendering.
        ("site", f"/api/v1/sites/{site_id}", {}),
        # Effective site settings (may carry AP port / power related config).
        ("site_setting_derived", f"/api/v1/sites/{site_id}/setting/derived", {}),
    ]


def get(session: requests.Session, base: str, path: str, params: dict) -> requests.Response:
    return session.get(base + path, params=params or None, timeout=TIMEOUT)


HEADERS: dict[str, dict] = {}  # slug -> response headers (cookies dropped)


def save(slug: str, resp: requests.Response, token: str) -> None:
    HEADERS[slug] = {
        k: v.replace(token, "<REDACTED_TOKEN>")
        for k, v in resp.headers.items()
        if k.lower() not in ("set-cookie", "cookie")
    }
    try:
        body = json.dumps(resp.json(), indent=2, sort_keys=False, ensure_ascii=False)
    except ValueError:
        body = resp.text
    body = body.replace(token, "<REDACTED_TOKEN>")
    out = SAMPLES_DIR / f"{slug}.json"
    out.write_text(body + "\n", encoding="utf-8")
    print(f"  {resp.status_code}  {slug}.json  ({len(body)} bytes)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--org-id", required=True)
    ap.add_argument("--site-id", required=True)
    ap.add_argument("--api-host", default="api.mist.com")
    args = ap.parse_args()

    token = os.environ.get("MIST_API_TOKEN")
    if not token:
        print("MIST_API_TOKEN is not set", file=sys.stderr)
        return 2

    SAMPLES_DIR.mkdir(exist_ok=True)
    base = f"https://{args.api_host}"
    session = requests.Session()
    session.headers.update({"Authorization": f"Token {token}", "Accept": "application/json"})

    failures = 0
    stats_resp = None
    for slug, path, params in site_endpoints(args.org_id, args.site_id):
        resp = get(session, base, path, params)
        save(slug, resp, token)
        failures += not resp.ok
        if slug == "site_stats_devices_ap" and resp.ok:
            stats_resp = resp

    # Per-AP stats detail: may include fields the list view omits.
    if stats_resp is not None:
        aps = stats_resp.json()
        if isinstance(aps, list):
            for dev in aps[:MAX_PER_DEVICE]:
                dev_id = dev.get("id")
                if not dev_id:
                    continue
                resp = get(
                    session, base, f"/api/v1/sites/{args.site_id}/stats/devices/{dev_id}", {}
                )
                save(f"site_stats_device_{dev_id}", resp, token)
                failures += not resp.ok

    # Pagination probe: does `page` page through results? Compare the two pages'
    # ids and the response headers. Two small GETs, read-only.
    for page in (1, 2):
        resp = get(
            session,
            base,
            f"/api/v1/sites/{args.site_id}/stats/devices",
            {"type": "ap", "limit": 10, "page": page},
        )
        save(f"page_probe_stats_limit10_page{page}", resp, token)
        failures += not resp.ok

    headers_out = SAMPLES_DIR / "_response_headers.json"
    headers_out.write_text(json.dumps(HEADERS, indent=2) + "\n", encoding="utf-8")
    print(f"done: {failures} non-2xx response(s); files in {SAMPLES_DIR}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
