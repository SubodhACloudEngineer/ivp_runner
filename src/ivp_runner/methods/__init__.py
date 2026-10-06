"""Collection backends. A check's ``collect.method`` selects one.

Only ``mist_api`` is implemented. A check naming ``ssh`` or ``probe`` evaluates
to ERROR (method_unavailable), never to a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

IMPLEMENTED_METHODS = frozenset({"mist_api"})


@dataclass(frozen=True)
class Payload:
    """One collected response: its items plus where the raw bytes were saved."""

    method: str
    source: str
    sample: str  # "once", "start" or "end"
    items: list[dict]
    collected_at: datetime
    raw_path: str
    raw_sha256: str
    # How to read a device's identity from an item: DeviceRef field -> item key.
    identity: dict[str, str] = field(default_factory=dict)


class CollectError(RuntimeError):
    """Collection failed; the checks depending on it report ERROR (api_error)."""
