"""mist_api collector: read-only collection from the Mist cloud API.

* GET only. There is no code path that issues any other HTTP method.
* The token comes from ``MIST_API_TOKEN``. It is sent only in the
  Authorization header and never logged, persisted or put in an error. If a
  response body echoes it, it is scrubbed before saving.
* Each source the catalogue needs is fetched once per phase, never per
  device or per check.
* Every HTTP response is saved under ``<run_dir>/raw/``, with a manifest, so
  any result can be traced to the bytes it was judged on.
* No judgements: failures come back as ``CollectError`` values, and the
  assertion engine decides what they mean.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import requests

from ivp_runner.collectors import (
    CollectError,
    Collection,
    CollectorConfigError,
    Payload,
    normalize_token,
)

TIMEOUT = (10, 30)  # (connect, read) seconds
MAX_PAGES = 100
BACKOFF_S = (1, 2, 4, 8, 16)
REDACTED = b"<REDACTED_TOKEN>"
# Response headers worth keeping as evidence; cookies are never recorded.
_DROP_HEADERS = {"set-cookie", "cookie"}


@dataclass(frozen=True)
class Source:
    path: str  # formatted with site_id
    kind: Literal["list", "object"]
    params: dict[str, str] = field(default_factory=dict)
    identity: dict[str, str] = field(default_factory=dict)
    # Heading of this source's table in docs/field_inventory.md
    inventory_section: str = ""


SOURCES: dict[str, Source] = {
    "site_device_stats": Source(
        path="/api/v1/sites/{site_id}/stats/devices",
        kind="list",
        params={"type": "ap"},
        identity={"id": "id", "name": "name", "mac": "mac", "model": "model"},
        inventory_section="Endpoint: AP stats",
    ),
    "site": Source(
        path="/api/v1/sites/{site_id}",
        kind="object",
        inventory_section="Endpoint: site",
    ),
}
# Fetched at run start whatever the catalogue says: name and timezone for the results.
ALWAYS_AT_START = ("site",)


def normalize_host(raw: str) -> str:
    """``api.eu.mist.com`` from what people paste: a URL, a trailing slash."""
    host = raw.strip()
    for scheme in ("https://", "http://"):
        if host.lower().startswith(scheme):
            host = host[len(scheme) :]
    host = host.rstrip("/")
    if not host or "/" in host or any(c.isspace() for c in host):
        raise CollectorConfigError(
            f"--api-host {raw!r} is not a host name. Give the API host only, e.g. "
            "api.mist.com (global) or api.eu.mist.com (EU)."
        )
    return host


def _proxy_note() -> str:
    # Never print the proxy URL itself: it may carry credentials.
    set_ = any(os.environ.get(v) for v in ("HTTPS_PROXY", "https_proxy"))
    return "HTTPS_PROXY is set in this shell" if set_ else "HTTPS_PROXY is not set in this shell"


_DNS_MARKERS = (
    "NameResolutionError",
    "Failed to resolve",
    "Name or service not known",
    "getaddrinfo failed",
    "nodename nor servname",
    "Temporary failure in name resolution",
)
_REFUSED_MARKERS = ("Connection refused", "ConnectionRefusedError", "WinError 10061")


def _connection_error(e: Exception, host: str, attempts: int) -> CollectError:
    """Turn a requests exception into something a network engineer can act on."""
    text = f"{type(e).__name__}: {e}"
    if isinstance(e, requests.exceptions.SSLError):
        return CollectError(
            f"TLS certificate verification failed for {host}",
            kind="tls",
            fix="If this network inspects TLS (a corporate proxy or firewall), set "
            "REQUESTS_CA_BUNDLE to your corporate CA bundle (.pem). Do not disable "
            "certificate verification.",
        )
    if isinstance(e, requests.exceptions.ProxyError):
        return CollectError(
            f"the HTTPS proxy failed to connect to {host}",
            kind="proxy",
            fix=f"{_proxy_note()}. Check --api-host is spelled right (a host that does not "
            f"exist fails at the proxy too), then the proxy address and credentials, and "
            f"that the proxy allows {host}.",
        )
    if any(m in text for m in _DNS_MARKERS):
        return CollectError(
            f"cannot resolve {host} (DNS lookup failed)",
            kind="dns",
            fix=f"Check --api-host is spelled right (api.mist.com global, api.eu.mist.com EU) "
            f"and that this machine can resolve it: nslookup {host}",
        )
    if any(m in text for m in _REFUSED_MARKERS):
        port = host.rsplit(":", 1)[1] if ":" in host else "443"
        name = host.rsplit(":", 1)[0]
        return CollectError(
            f"connection to {name}:{port} was refused",
            kind="refused",
            fix=f"Something on the path rejects HTTPS to {name}. Check firewall rules for "
            f"outbound tcp/{port}; {_proxy_note()}.",
        )
    return CollectError(
        f"no usable response from {host} after {attempts} attempts ({type(e).__name__})",
        kind="unreachable",
        fix=f"Check this machine has outbound HTTPS (tcp/443) to {host}: "
        f"curl -sI https://{host}/api/v1/self should answer (401 is fine). {_proxy_note()}; "
        "set it if your network needs a proxy.",
    )


def _int_header(headers: dict, name: str, default: int | None) -> int | None:
    """Case-insensitive integer header, or ``default`` if absent or not a number."""
    for k, v in headers.items():
        if k.lower() == name.lower():
            try:
                return int(v)
            except (TypeError, ValueError):
                return default
    return default


class MistApiCollector:
    def __init__(
        self,
        host: str,
        *,
        token: str | None = None,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 5,
        page_limit: int = 1000,
    ):
        token = normalize_token(token or os.environ.get("MIST_API_TOKEN"))
        self._host = normalize_host(host)
        self._token = token
        self._session = session or requests.Session()
        self._sleep = sleep
        self._max_retries = max_retries
        self._page_limit = page_limit
        self._manifest: list[dict] = []

    def __repr__(self) -> str:  # never expose the token
        return f"MistApiCollector(host={self._host!r})"

    # ------------------------------------------------------------ planning

    @staticmethod
    def plan(catalogue, site_class: str, phase: Literal["start", "end"]) -> list[str]:
        """Sources to fetch in ``phase``: once each, however many checks/devices use them."""
        needed: list[str] = list(ALWAYS_AT_START) if phase == "start" else []
        for check in catalogue.checks:
            if check.collect.method != "mist_api" or not check.applies_to(site_class):
                continue
            if phase == "end" and check.collect.sampling != "run_start_and_end":
                continue
            if check.collect.source not in needed:
                needed.append(check.collect.source)
        return needed

    def collect(
        self,
        catalogue,
        site_class: str,
        site_id: str,
        run_dir: str | Path,
        phase: Literal["start", "end"],
    ) -> Collection:
        out: Collection = {}
        fatal: CollectError | None = None
        for source in self.plan(catalogue, site_class, phase):
            if fatal is not None:  # same token, host and site: it would fail the same way
                out[source] = fatal
                continue
            try:
                out[source] = self.fetch(source, site_id, run_dir, phase)
            except CollectError as e:
                out[source] = e
                fatal = e if e.fatal else None
        self._write_manifest(run_dir)
        return out

    # ------------------------------------------------------------ fetching

    def fetch(self, source: str, site_id: str, run_dir: str | Path, phase: str) -> Payload:
        if source not in SOURCES:
            raise CollectError(f"unknown mist_api source {source!r}")
        spec = SOURCES[source]
        raw_dir = Path(run_dir) / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        path = spec.path.format(site_id=site_id)

        if spec.kind == "object":
            body, data, _ = self._get_json(
                path, dict(spec.params), raw_dir / f"{source}.{phase}.json", site_id
            )
            if not isinstance(data, dict):
                raise CollectError(
                    f"GET {spec.path}: expected an object, got {type(data).__name__}"
                )
            return self._payload(
                source, phase, data, raw_dir / f"{source}.{phase}.json", body, spec
            )

        items: list = []
        seen: set[str] = set()
        id_key = spec.identity.get("id", "id")
        # Paging verified against a live capture (limit=10, pages 1 and 2 returned
        # disjoint items; headers X-Page-Total/-Limit/-Page present). We stop on a
        # short page or once X-Page-Total items are in hand, and cross-check the
        # count. The duplicate check still guards against a server ignoring `page`.
        total: int | None = None
        for page in range(1, MAX_PAGES + 1):
            params = {**spec.params, "limit": str(self._page_limit), "page": str(page)}
            _, data, headers = self._get_json(
                path, params, raw_dir / f"{source}.{phase}.p{page}.json", site_id
            )
            total = _int_header(headers, "X-Page-Total", total)
            if not isinstance(data, list):
                raise CollectError(f"GET {spec.path}: expected a list, got {type(data).__name__}")
            ids = {str(it.get(id_key)) for it in data if isinstance(it, dict) and id_key in it}
            if page > 1 and ids & seen:
                raise CollectError(
                    f"GET {spec.path}: page {page} repeats items from earlier pages; "
                    "the API may not support `page`. Stopping to avoid duplicates."
                )
            seen |= ids
            items.extend(data)
            if len(data) < self._page_limit or (total is not None and len(items) >= total):
                break
        else:
            raise CollectError(
                f"GET {spec.path}: more than {MAX_PAGES} pages; refusing to continue"
            )

        if total is not None and len(items) != total:
            raise CollectError(
                f"GET {spec.path}: X-Page-Total says {total} items but {len(items)} were "
                "returned; the list changed during collection or paging is broken"
            )
        combined = (json.dumps(items, indent=2, ensure_ascii=False) + "\n").encode()
        combined_path = raw_dir / f"{source}.{phase}.json"
        combined_path.write_bytes(combined)
        return self._payload(source, phase, items, combined_path, combined, spec)

    def _payload(self, source, phase, data, raw_path: Path, body: bytes, spec: Source) -> Payload:
        return Payload(
            method="mist_api",
            source=source,
            sample=phase,
            data=data,
            collected_at=datetime.now(UTC),
            raw_path=str(raw_path),
            raw_sha256=hashlib.sha256(body).hexdigest(),
            identity=dict(spec.identity),
        )

    def _get_json(
        self, path: str, params: dict, save_to: Path, site_id: str = ""
    ) -> tuple[bytes, Any, dict]:
        """GET with retries; save the body verbatim (token scrubbed).

        Returns (raw bytes, parsed JSON, response headers).
        """
        url = f"https://{self._host}{path}"
        headers = {"Authorization": f"Token {self._token}", "Accept": "application/json"}
        host = self._host
        for attempt in range(1, self._max_retries + 2):
            entry = {
                "path": path,
                "params": params,
                "attempt": attempt,
                "at_utc": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            }
            self._manifest.append(entry)
            try:
                resp = self._session.get(url, headers=headers, params=params, timeout=TIMEOUT)
            except (requests.ConnectionError, requests.Timeout) as e:
                entry["error"] = type(e).__name__
                err = _connection_error(e, host, attempt)
                # DNS, TLS, proxy and refused fail the same way every time: no retry.
                # Timeouts and resets may be transient; retry those, but not for long.
                if err.kind != "unreachable" or attempt > min(self._max_retries, 2):
                    raise err from None
                self._sleep(self._backoff(attempt, None))
                continue

            status = resp.status_code
            entry["status"] = status
            entry["response_headers"] = {
                k: v for k, v in resp.headers.items() if k.lower() not in _DROP_HEADERS
            }
            if status == 429 or status >= 500:
                if attempt > self._max_retries:
                    if status == 429:
                        raise CollectError(
                            f"GET {path}: rate limited (429) after {attempt} attempts",
                            kind="rate_limited",
                            fix="Another tool may be using the same token heavily. Wait a few "
                            "minutes and run again.",
                        )
                    raise CollectError(
                        f"GET {path}: server error {status} after {attempt} attempts",
                        kind="server",
                        fix="This is on the Mist side. Check the Mist status page and run "
                        "again later.",
                    )
                self._sleep(self._backoff(attempt, resp.headers.get("Retry-After")))
                continue
            if status == 401:
                raise CollectError(
                    f"Mist rejected the API token (HTTP 401 from {host})",
                    kind="auth",
                    fix="The token is wrong, expired or revoked, or it was created on a "
                    "different Mist cloud: tokens only work on their own cloud (an EU org's "
                    "token works only with --api-host api.eu.mist.com). Create a new token "
                    "in the Mist portal and export MIST_API_TOKEN again.",
                )
            if status == 403:
                raise CollectError(
                    f"the API token cannot read site {site_id} (HTTP 403 from {host})",
                    kind="forbidden",
                    fix="The token is valid but has no access to this site. Check --site "
                    "belongs to the org you expect, and that the token's account or org "
                    "token has at least read-only (Observer) access to it.",
                )
            if status == 404:
                raise CollectError(
                    f"site {site_id} was not found on {host} (HTTP 404)",
                    kind="not_found",
                    fix="Check --site is the site's UUID as shown in the Mist portal's site "
                    "settings. If the ID is right, the org may live on another Mist cloud: "
                    "check --api-host (api.mist.com global, api.eu.mist.com EU).",
                )
            if status == 400:
                raise CollectError(
                    f"Mist rejected the request for site {site_id} as malformed (HTTP 400)",
                    kind="bad_request",
                    fix="--site is usually the cause: it must be the site's UUID, "
                    "e.g. 0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0.",
                )
            if status >= 400:
                raise CollectError(f"GET {path}: HTTP {status}")

            body = resp.content.replace(self._token.encode(), REDACTED)
            save_to.write_bytes(body)
            entry["saved_as"] = save_to.name
            entry["sha256"] = hashlib.sha256(body).hexdigest()
            try:
                return body, json.loads(body), dict(resp.headers)
            except ValueError:
                raise CollectError(
                    f"{host} answered with something that is not JSON",
                    kind="not_api",
                    fix="--api-host must be the API host (api.mist.com / api.eu.mist.com), "
                    "not the portal (manage.mist.com). A proxy login page can also cause "
                    f"this. The response was saved to raw/{save_to.name}.",
                ) from None
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass  # HTTP-date form: fall back to exponential backoff
        return float(BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)])

    def _write_manifest(self, run_dir: str | Path) -> None:
        path = Path(run_dir) / "raw" / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self._manifest, indent=2) + "\n"
        path.write_text(text.replace(self._token, REDACTED.decode()), encoding="utf-8")
