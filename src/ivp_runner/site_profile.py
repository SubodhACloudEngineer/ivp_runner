"""Site profile: per-site design intent that checks compare against.

Values here come from the LLD/MOP, never from Mist site settings, which only
describe current state. The catalogue refers to them by name, e.g.
``{expect: dns_servers}``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress, IPvAnyNetwork, PositiveInt

SiteClass = Literal["small", "medium", "large"]
Band = Literal["24", "5", "6"]


class MissingExpectation(LookupError):
    """The profile does not define a value a check needs."""


class ExpectedSsid(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ssid: str = Field(min_length=1)
    bands: list[Band] = Field(min_length=1)


class Expectations(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uplink_port: str | None = None
    ssids: list[ExpectedSsid] | None = Field(default=None, min_length=1)
    mgmt_subnet: IPvAnyNetwork | None = None
    dns_servers: list[IPvAnyAddress] | None = Field(default=None, min_length=1)
    min_uplink_speed_mbps: PositiveInt | None = None


class SiteProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    site_id: str = Field(min_length=1)
    site_class: SiteClass
    expectations: Expectations

    def lookup(self, name: str) -> Any:
        """Plain JSON-able value for an expectation name. Raises MissingExpectation.

        Besides the declared names, ``ssid_count.band_<b>`` is derived from
        ``ssids``: the number of expected SSIDs that list band ``<b>``.
        """
        exp = self.expectations
        if name.startswith("ssid_count."):
            if exp.ssids is None:
                raise MissingExpectation("ssids")
            band = name.removeprefix("ssid_count.").removeprefix("band_")
            return sum(1 for s in exp.ssids if band in s.bands)
        if name not in Expectations.model_fields:
            raise MissingExpectation(name)
        value = getattr(exp, name)
        if value is None:
            raise MissingExpectation(name)
        if name == "mgmt_subnet":
            return str(value)
        if name == "dns_servers":
            return [str(v) for v in value]
        if name == "ssids":
            return [s.model_dump() for s in value]
        return value


def load_site_profile(path: str | Path) -> SiteProfile:
    with open(path, encoding="utf-8") as fh:
        return SiteProfile.model_validate(yaml.safe_load(fh))
