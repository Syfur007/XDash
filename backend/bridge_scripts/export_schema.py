"""bridge_scripts/export_schema.py — the orchestration config schema
(orchestration/schema.py's pydantic `Config` model) as JSON Schema, so the
dashboard can build schema-aware form fields (dropdowns for Literal types,
required-vs-optional, real defaults) instead of inferring field type from
whatever value a template config happens to already have.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _common import run_main  # noqa: E402


def main(argv):
    from orchestration.schema import Config

    return Config.model_json_schema()


if __name__ == "__main__":
    run_main(main)
