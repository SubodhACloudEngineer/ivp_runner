"""mist_api backend: read-only collection from the Mist cloud API.

Only GET requests are ever built: there is no code path that issues anything
else. The token is read from ``MIST_API_TOKEN``, sent only in the
Authorization header, and never logged or written. If a response body echoes
it, it is scrubbed before the raw payload is saved.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import requests

from ivp_runner.methods import CollectError, Payload

TIMEOUT = 30


@dataclass(frozen=True)
class Source:
    path: str  # formatted with site_id / org_id
    params: dict[str, str]
    identity: dict[str, str]


# Named sources a catalogue may reference. Each one's response shape is
# documented in docs/field_inventory.md.
SOURCES: dict[str, Source] = {
    "site_device_stats": Source(
        path="/api/v1/sites/{site_id}/stats/devices",
        params={"type": "ap", "limit": "1000"},
        identity={"id": "id", "name": "name", "mac": "mac", "model": "model"},
    ),
}


class Transport(Protocol):
    def get(self, url: str, *, headers: dict, params: dict, timeout: float) -> Any: ...


class MistApi:
    def __init__(self, host: str, transport: Transport | None = None, token: str | None = None):
        token = token or os.environ.get("MIST_API_TOKEN")
        if not token:
            raise CollectError("MIST_API_TOKEN is not set")
        self._host = host
        self._token = token
        self._transport = transport or requests.Session()

    def __repr__(self) -> str:  # never expose the token
        return f"MistApi(host={self._host!r})"

    def collect(
        self, source: str, site_id: str, out_dir: str | Path, sample: str = "once"
    ) -> Payload:
        if source not in SOURCES:
            raise CollectError(f"unknown mist_api source {source!r}")
        spec = SOURCES[source]
        url = f"https://{self._host}{spec.path.format(site_id=site_id)}"
        collected_at = datetime.now(UTC)
        try:
            resp = self._transport.get(
                url,
                headers={"Authorization": f"Token {self._token}", "Accept": "application/json"},
                params=dict(spec.params),
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            raise CollectError(f"GET {spec.path} failed: {type(e).__name__}") from None

        raw = resp.content.replace(self._token.encode(), b"<REDACTED_TOKEN>")
        try:
            items = json.loads(raw)
        except ValueError:
            raise CollectError(f"GET {spec.path}: response is not JSON") from None
        if not isinstance(items, list):
            raise CollectError(f"GET {spec.path}: expected a list, got {type(items).__name__}")

        raw_dir = Path(out_dir) / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / f"{source}.{sample}.json"
        raw_path.write_bytes(raw)
        return Payload(
            method="mist_api",
            source=source,
            sample=sample,
            items=items,
            collected_at=collected_at,
            raw_path=str(raw_path),
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            identity=dict(spec.identity),
        )
