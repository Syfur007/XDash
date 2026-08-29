"""bridge_scripts/list_models.py — every model family currently registered
via models/registry.py's @MODEL_REGISTRY.register(...) decorator, so the
Create Config model picker (IMPLEMENTATION_PLAN.md Phase 2) always reflects
the real registry instead of a hand-maintained list that drifts the moment
a new family (e.g. Phase 6's mamba_unet) is added.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _common import run_main  # noqa: E402


def main(argv):
    from models.registry import MODEL_REGISTRY

    return {"names": sorted(MODEL_REGISTRY.keys())}


if __name__ == "__main__":
    run_main(main)
