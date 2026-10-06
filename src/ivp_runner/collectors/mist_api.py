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

from ivp_runner.collectors import CollectError, Collection, Payload, normalize_token

TIMEOUT = 30
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
        self._host = host
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
        for source in self.plan(catalogue, site_class, phase):
            try:
                out[source] = self.fetch(source, site_id, run_dir, phase)
            except CollectError as e:
                out[source] = e
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
            body, data = self._get_json(path, dict(spec.params), raw_dir / f"{source}.{phase}.json")
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
        # Paging with `page` is NOT verified against a captured response: only
        # `limit` is. The duplicate check below makes a server that ignores
        # `page` fail loudly instead of looping or double-counting.
        for page in range(1, MAX_PAGES + 1):
            params = {**spec.params, "limit": str(self._page_limit), "page": str(page)}
            _, data = self._get_json(path, params, raw_dir / f"{source}.{phase}.p{page}.json")
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
            if len(data) < self._page_limit:
                break
        else:
            raise CollectError(
                f"GET {spec.path}: more than {MAX_PAGES} pages; refusing to continue"
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

    def _get_json(self, path: str, params: dict, save_to: Path) -> tuple[bytes, Any]:
        """GET with retries; save the body verbatim (token scrubbed); return (bytes, parsed)."""
        url = f"https://{self._host}{path}"
        headers = {"Authorization": f"Token {self._token}", "Accept": "application/json"}
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
                if attempt > self._max_retries:
                    raise CollectError(
                        f"GET {path}: {type(e).__name__} after {attempt} attempts"
                    ) from None
                self._sleep(self._backoff(attempt, None))
                continue

            status = resp.status_code
            entry["status"] = status
            entry["response_headers"] = {
                k: v for k, v in resp.headers.items() if k.lower() not in _DROP_HEADERS
            }
            if status == 429 or status >= 500:
                if attempt > self._max_retries:
                    what = "rate limited (429)" if status == 429 else f"server error {status}"
                    raise CollectError(f"GET {path}: {what} after {attempt} attempts")
                self._sleep(self._backoff(attempt, resp.headers.get("Retry-After")))
                continue
            if status in (401, 403):
                raise CollectError(
                    f"GET {path}: {status} authentication failed or the token lacks "
                    "access to this site"
                )
            if status == 404:
                raise CollectError(
                    f"GET {path}: 404 not found; check the site id and --api-host "
                    "(EU orgs use api.eu.mist.com)"
                )
            if status >= 400:
                raise CollectError(f"GET {path}: HTTP {status}")

            body = resp.content.replace(self._token.encode(), REDACTED)
            save_to.write_bytes(body)
            entry["saved_as"] = save_to.name
            entry["sha256"] = hashlib.sha256(body).hexdigest()
            try:
                return body, json.loads(body)
            except ValueError:
                raise CollectError(f"GET {path}: response is not JSON") from None
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
