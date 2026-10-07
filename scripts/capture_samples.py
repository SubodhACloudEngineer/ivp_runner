"""Throwaway: capture raw Mist API responses into samples/ for field discovery.

READ-ONLY. Every request below is a GET. The token is read from MIST_API_TOKEN,
sent only in the Authorization header, and never printed or written to disk
(it is scrubbed from any response body that happens to echo it).

Usage:
    read -rs MIST_API_TOKEN && export MIST_API_TOKEN
    python scripts/capture_samples.py \
        --org-id <org_uuid> --site-id <site_uuid> [--api-host api.mist.com]

    # Only the site-insight / SLE candidates (for the interactive launcher):
    python scripts/capture_samples.py --insights-only \
        --org-id <org_uuid> --site-id <site_uuid> --api-host api.eu.mist.com
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


def insight_endpoints(org_id: str, site_id: str) -> list[tuple[str, str, dict]]:
    """Candidates for "site insights" (SLEs, site-level counters).

    These are guesses at where the data lives, which is the point of capturing
    them: the launcher reads only fields seen in a successful (200) response.
    A 404 is saved as *.error.json and simply means "not here". Client lists
    are deliberately not captured: they hold personal data (hostnames, users).
    """
    sle = f"/api/v1/sites/{site_id}/sle/site/{site_id}"
    return [
        ("insight_site_stats", f"/api/v1/sites/{site_id}/stats", {}),
        ("insight_sle_metrics", f"{sle}/metrics", {}),
        ("insight_sle_coverage_summary", f"{sle}/metric/coverage/summary", {"duration": "1d"}),
        ("insight_sle_ap_health_summary", f"{sle}/metric/ap-health/summary", {"duration": "1d"}),
        ("insight_sle_throughput_summary", f"{sle}/metric/throughput/summary", {"duration": "1d"}),
        ("insight_org_sites_sle", f"/api/v1/orgs/{org_id}/insights/sites-sle", {}),
    ]


def get(session: requests.Session, base: str, path: str, params: dict) -> requests.Response:
    return session.get(base + path, params=params or None, timeout=TIMEOUT)


def clean_token(raw: str | None) -> str:
    """Bare key from MIST_API_TOKEN; exits with a reason (never the token) if malformed.

    Same rules as ivp_runner.collectors.normalize_token, duplicated so this
    throwaway script runs without the package installed.
    """
    if raw is None or not raw.strip():
        sys.exit("MIST_API_TOKEN is not set (read -rs MIST_API_TOKEN && export MIST_API_TOKEN)")
    token = raw.strip()
    if token[:6].lower() == "token ":
        print("note: removed a leading 'Token ' from MIST_API_TOKEN; paste only the key next time")
        token = token[6:].lstrip()
    for i, ch in enumerate(token):
        if ch.isspace():
            kind = "a line break" if ch in "\r\n" else "a tab" if ch == "\t" else "a space"
            sys.exit(
                f"MIST_API_TOKEN contains {kind} at character {i + 1} of {len(token)}; "
                "paste only the key itself (no 'Token ' prefix, no quotes, one line)"
            )
    return token


HEADERS: dict[str, dict] = {}  # slug -> response headers (cookies dropped)


class AuthFailed(Exception):
    pass


def save(slug: str, resp: requests.Response, token: str) -> None:
    """Save the body. A failed response never overwrites a good earlier capture."""
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
    name = f"{slug}.json" if resp.ok else f"{slug}.error.json"
    (SAMPLES_DIR / name).write_text(body + "\n", encoding="utf-8")
    print(f"  {resp.status_code}  {name}  ({len(body)} bytes)")
    if resp.status_code in (401, 403):
        raise AuthFailed(body.strip()[:200])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--org-id", required=True)
    ap.add_argument("--site-id", required=True)
    ap.add_argument("--api-host", default="api.mist.com")
    ap.add_argument(
        "--insights-only", action="store_true", help="capture only the site-insight candidates"
    )
    args = ap.parse_args()

    token = clean_token(os.environ.get("MIST_API_TOKEN"))

    SAMPLES_DIR.mkdir(exist_ok=True)
    base = f"https://{args.api_host}"
    session = requests.Session()
    session.headers.update({"Authorization": f"Token {token}", "Accept": "application/json"})

    try:
        failures = capture(session, base, args, token)
    except AuthFailed as e:
        print(f"\nSTOPPED: Mist rejected the token: {e}", file=sys.stderr)
        print("Existing good samples were left untouched.", file=sys.stderr)
        return 1
    finally:
        headers_out = SAMPLES_DIR / "_response_headers.json"
        headers_out.write_text(json.dumps(HEADERS, indent=2) + "\n", encoding="utf-8")

    print(f"done: {failures} non-2xx response(s); files in {SAMPLES_DIR}")
    return 1 if failures else 0


def capture(session: requests.Session, base: str, args, token: str) -> int:
    failures = 0
    print("site insight candidates (a 404 just means the data is not there):")
    for slug, path, params in insight_endpoints(args.org_id, args.site_id):
        resp = get(session, base, path, params)
        save(slug, resp, token)
        failures += not resp.ok
    if args.insights_only:
        return failures

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
    # ids and the X-Page-* headers. Two small GETs, read-only.
    print("pagination probe (limit=10):")
    for page in (1, 2):
        resp = get(
            session,
            base,
            f"/api/v1/sites/{args.site_id}/stats/devices",
            {"type": "ap", "limit": 10, "page": page},
        )
        save(f"page_probe_stats_limit10_page{page}", resp, token)
        failures += not resp.ok
        paging = {k: v for k, v in resp.headers.items() if k.lower().startswith("x-page-")}
        ids = [d.get("id") for d in resp.json()] if resp.ok else []
        print(f"    page {page}: {len(ids)} items, headers {paging or 'none'}")
    return failures


if __name__ == "__main__":
    sys.exit(main())
