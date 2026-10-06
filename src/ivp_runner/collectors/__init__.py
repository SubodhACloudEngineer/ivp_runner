"""Collection backends. A check's ``collect.method`` selects one.

Collectors fetch and persist data; they never judge it. Only ``mist_api`` is
implemented. A check naming ``ssh`` or ``probe`` evaluates to ERROR
(method_unavailable), never to a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

IMPLEMENTED_METHODS = frozenset({"mist_api"})


@dataclass(frozen=True)
class Payload:
    """One collected source: parsed data plus where the raw bytes were saved."""

    method: str
    source: str
    sample: str  # "once", "start" or "end"
    data: Any  # list for list sources, dict for object sources
    collected_at: datetime
    raw_path: str
    raw_sha256: str
    # How to read a device's identity from an item: DeviceRef field -> item key.
    identity: dict[str, str] = field(default_factory=dict)

    @property
    def items(self) -> list[dict]:
        return self.data if isinstance(self.data, list) else [self.data]


class CollectError(RuntimeError):
    """Collecting one source failed; checks using it report ERROR (api_error)."""


class CollectorConfigError(RuntimeError):
    """The collector cannot start at all (e.g. no API token)."""


# Per phase: source name -> Payload, or the CollectError that stopped it.
Collection = dict[str, Payload | CollectError]
