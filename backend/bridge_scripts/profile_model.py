"""bridge_scripts/profile_model.py '<json-encoded model kwargs>' — builds
one registered model with the given kwargs (the same kwargs a config's
`model:` block supplies, including `name`) and reports its trainable/total
parameter count, so the Create Config model picker can show "this
configuration is N params" before the config is ever saved or launched.

Building an arbitrary model is the one bridge script that can be slow or
memory-heavy (a large model family on a CPU-only bridge interpreter) —
callers should apply their own timeout via backend/bridge.py's `timeout`
argument rather than assuming this always returns quickly.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _common import run_main  # noqa: E402


def main(argv):
    if not argv:
        raise ValueError("usage: profile_model.py '<json-encoded model kwargs, including name>'")
    kwargs = json.loads(argv[0])
    if not isinstance(kwargs, dict) or "name" not in kwargs:
        raise ValueError("kwargs must be a JSON object including at least 'name'")

    from models.registry import get_model
    from utils.metrics import count_parameters

    model = get_model(**kwargs)
    trainable = count_parameters(model)
    total = sum(p.numel() for p in model.parameters())
    return {"params_trainable": trainable, "params_total": total}


if __name__ == "__main__":
    run_main(main)
