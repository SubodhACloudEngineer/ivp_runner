"""Site insights: a short summary of the site, shown before the test menu.

Built only from fields listed in docs/field_inventory.md (``FIELDS`` below;
a test enforces this). Mist SLE scores are not shown yet: their responses
have not been captured, so their field names are unknown. Run
``scripts/capture_samples.py --insights-only`` and share the files to add them.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from ivp_runner.criteria import MISSING, get_path

# (inventory section, field path) for every field read here.
FIELDS = (
    ("Endpoint: site", "name"),
    ("Endpoint: site", "timezone"),
    ("Endpoint: AP stats", "name"),
    ("Endpoint: AP stats", "status"),
    ("Endpoint: AP stats", "model"),
    ("Endpoint: AP stats", "power_constrained"),
    ("Endpoint: AP stats", "port_stat.<port>.speed"),
)


def site_insights(
    site: dict, aps: list[dict], uplink_port: str, now: datetime
) -> list[tuple[str, str]]:
    """(label, value) lines. Pure; ``now`` must be timezone-aware."""
    lines: list[tuple[str, str]] = []
    tz = str(site.get("timezone") or "UTC")
    try:
        local = now.astimezone(ZoneInfo(tz))
        when = f"{tz}, local time {local:%Y-%m-%d %H:%M %Z}"
    except (KeyError, ValueError):
        when = f"{tz} (unknown time zone)"
    lines.append(("Site", f"{site.get('name', '?')}  ({when})"))

    status = Counter(str(a.get("status", "unknown")) for a in aps)
    down = sorted(str(a.get("name", "?")) for a in aps if a.get("status") != "connected")
    text = f"{len(aps)} total: {status.get('connected', 0)} connected"
    if down:
        shown = ", ".join(down[:5]) + (f" +{len(down) - 5} more" if len(down) > 5 else "")
        text += f", {len(down)} not connected ({shown})"
    lines.append(("Access points", text))

    models = Counter(str(a.get("model", "?")) for a in aps)
    lines.append(("Models", ", ".join(f"{m} x{n}" for m, n in models.most_common())))

    constrained = sorted(str(a.get("name", "?")) for a in aps if a.get("power_constrained") is True)
    lines.append(
        (
            "PoE",
            f"{len(constrained)} power-constrained"
            + (f" ({', '.join(constrained[:5])})" if constrained else ""),
        )
    )

    speeds = Counter()
    for a in aps:
        v = get_path(a, f"port_stat.{uplink_port}.speed")
        if v is not MISSING and isinstance(v, int) and not isinstance(v, bool):
            speeds[v] += 1
    if speeds:
        lines.append(
            (
                f"Uplinks ({uplink_port})",
                ", ".join(f"{s} Mbps x{n}" for s, n in sorted(speeds.items(), reverse=True)),
            )
        )
    lines.append(("SLE insights", "not captured yet (see README, 'Site insights')"))
    return lines
