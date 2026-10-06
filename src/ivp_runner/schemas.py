"""JSON Schema files for the three data contracts, generated from the models.

    python -m ivp_runner.schemas          # rewrite schemas/*.schema.json

Tests fail if the committed files drift from the models.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import BaseModel

from ivp_runner.catalogue import Catalogue
from ivp_runner.results import TestResult
from ivp_runner.site_profile import SiteProfile

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"
MODELS: dict[str, type[BaseModel]] = {
    "catalogue": Catalogue,
    "result": TestResult,
    "site_profile": SiteProfile,
}


def render(model: type[BaseModel]) -> str:
    schema = model.model_json_schema(by_alias=True)
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> int:
    SCHEMA_DIR.mkdir(exist_ok=True)
    for name, model in MODELS.items():
        path = SCHEMA_DIR / f"{name}.schema.json"
        path.write_text(render(model), encoding="utf-8")
        print(f"wrote {path.relative_to(SCHEMA_DIR.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
