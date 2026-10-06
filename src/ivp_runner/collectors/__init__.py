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


def normalize_token(raw: str | None) -> str:
    """Return the bare API key, or raise CollectorConfigError without echoing it.

    Surrounding whitespace is stripped and a leading ``Token `` prefix (the
    header scheme, often pasted by mistake) is removed. Whitespace left inside
    the key is reported by position only.
    """
    if raw is None or not raw.strip():
        raise CollectorConfigError(
            "MIST_API_TOKEN is not set. Export it in your shell "
            "(read -rs MIST_API_TOKEN && export MIST_API_TOKEN); never put it in a file."
        )
    token = raw.strip()
    if token[:6].lower() == "token ":
        token = token[6:].lstrip()
    for i, ch in enumerate(token):
        if ch.isspace():
            kind = {"\n": "a line break", "\r": "a line break", "\t": "a tab"}.get(ch, "a space")
            raise CollectorConfigError(
                f"MIST_API_TOKEN contains {kind} at character {i + 1} of {len(token)}; "
                "paste only the key itself (no 'Token ' prefix, no quotes, one line)"
            )
    return token


# Per phase: source name -> Payload, or the CollectError that stopped it.
Collection = dict[str, Payload | CollectError]
