"""bridge_scripts/resolve_config.py <repo-relative-config-path> — the
fully compose-merged + schema-validated config `utils.config.load_config()`
would produce, as JSON: {"valid": true, "resolved": {...}} normally, or
{"valid": false, "errors": [...]} on a pydantic ValidationError (treated as
a legitimate result, not a bridge-transport failure — a config *should* be
able to fail validation and have the dashboard show why).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _common import run_main  # noqa: E402


def main(argv):
    if not argv:
        raise ValueError("usage: resolve_config.py <repo-relative-config-path>")
    config_path = argv[0]

    from utils.config import load_config

    try:
        resolved = load_config(config_path)
    except Exception as exc:
        if type(exc).__name__ == "ValidationError" and hasattr(exc, "errors"):
            return {
                "valid": False,
                "errors": [
                    {
                        "loc": list(e.get("loc", [])),
                        "msg": e.get("msg", ""),
                        "type": e.get("type", ""),
                    }
                    for e in exc.errors()
                ],
            }
        raise
    return {"valid": True, "resolved": resolved}


if __name__ == "__main__":
    run_main(main)
