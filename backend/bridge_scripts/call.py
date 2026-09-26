"""bridge_scripts/call.py <module:function> <json-args> — the one generic
entry point for framework hooks (XDASH_PLAN.md §4.2), replacing a new
per-feature script for every hook.

*module:function* is either

- a function in the host repo itself, e.g. `utils.config:load_config`
  (imported exactly as a script at the repo root would import it — cwd and
  PYTHONPATH are the host repo root, see ../bridge.py), or
- `xdash:<framework>.<function>`, e.g. `xdash:dissert.locate_run`: an
  XDash-shipped adapter function in bridge_scripts/adapters/<framework>.py.
  Adapters import host modules, but they live here, which keeps the rule
  that the host repo needs zero XDash-aware code.

*json-args* is a JSON object (passed as keyword arguments), a JSON list
(positional), or omitted (no arguments). The function's return value is
printed as JSON; any exception becomes {"__bridge_error__": ...} via
_common.run_main, which bridge.py turns into a BridgeError.

Runs under the host repo's interpreter (bridge_python_executable), so this
file and everything under adapters/ must stay Python 3.8-compatible.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _common import run_main  # noqa: E402

XDASH_PREFIX = "xdash:"
ADAPTERS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adapters")
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _load_adapter(framework):
    """Loads adapters/<framework>.py under a private module name, so it can
    never shadow (or be shadowed by) a host-repo package that happens to be
    called `adapters`."""
    if not _NAME_RE.match(framework):
        raise ValueError("Invalid adapter name %r" % framework)
    path = os.path.join(ADAPTERS_DIR, framework + ".py")
    if not os.path.isfile(path):
        raise ValueError("No XDash adapter for framework %r (expected %s)" % (framework, path))
    spec = importlib.util.spec_from_file_location("xdash_adapter_" + framework, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolve(spec):
    if spec.startswith(XDASH_PREFIX):
        framework, _, func = spec[len(XDASH_PREFIX):].rpartition(".")
        if not framework or not func:
            raise ValueError("Adapter hooks look like 'xdash:<framework>.<function>', got %r" % spec)
        module = _load_adapter(framework)
    else:
        module_name, sep, func = spec.partition(":")
        if not sep or not module_name or not func:
            raise ValueError("Hooks look like '<module>:<function>', got %r" % spec)
        module = importlib.import_module(module_name)
    fn = getattr(module, func, None)
    if not callable(fn):
        raise ValueError("%r does not name a callable" % spec)
    return fn


def main(argv):
    if not argv:
        raise ValueError("usage: call.py <module:function> [<json-args>]")
    fn = resolve(argv[0])
    args = json.loads(argv[1]) if len(argv) > 1 and argv[1].strip() else {}
    if isinstance(args, dict):
        return fn(**args)
    if isinstance(args, list):
        return fn(*args)
    return fn(args)


if __name__ == "__main__":
    run_main(main)
