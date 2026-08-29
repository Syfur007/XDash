"""Shared helpers for bridge_scripts/*.py.

Each script here is invoked as `bridge_python_executable <this_file>
[args]` with cwd=<host repo root> and PYTHONPATH=<host repo root> (see
../bridge.py), so the host repo's own packages (orchestration, models,
...) import exactly as they would from any script sitting at the repo
root — these files just happen to live inside dashboard/ instead, keeping
the whole dashboard folder self-contained (the host repo needs zero
dashboard-aware code of its own).
"""
from __future__ import annotations

import json
import sys
from typing import Any, Callable, List


def emit(result: Any) -> None:
    json.dump(result, sys.stdout, default=str)
    sys.stdout.flush()


def emit_error(message: str) -> None:
    emit({"__bridge_error__": True, "message": message})


def run_main(fn: Callable[[List[str]], Any]) -> None:
    """Call fn(argv[1:]) and print its return value as JSON. Any exception
    fn raises becomes a clean {"__bridge_error__": ...} object instead of
    a Python traceback on stdout, which would otherwise fail JSON parsing
    on the dashboard side and get reported as a generic "not valid JSON"
    BridgeUnavailable instead of the actual, more useful error message.
    """
    try:
        emit(fn(sys.argv[1:]))
    except Exception as exc:  # noqa: BLE001 - deliberately broad: this is the subprocess boundary
        emit_error(f"{type(exc).__name__}: {exc}")
