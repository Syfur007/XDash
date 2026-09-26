"""`GET/PATCH /api/profile` + `PUT /api/profile/raw` (XDASH_PLAN.md §7,
§4.1, §8.6): a dotted-path patch applied to `repos/<profile>.yaml` with
ruamel.yaml's round-trip mode (comments and order kept for everything but
the changed key), validated by constructing a throwaway Settings from the
patched text *before* anything real is touched, written atomically, then
applied live with `settings.reload()` — so a bad patch never corrupts the
profile it was about to replace, and a good one takes effect without a
restart (except the keys that can't, §4.5/§8.6's `restart_required`).

Per-key help text (`comments`) and type/enum hints (`profile_hints.py`,
optional) are read-only decoration for GET — nothing here needs them to
patch or validate correctly.
"""
from __future__ import annotations

import io
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from . import config as config_mod
from .config import settings
from .store import atomic_write_text

try:
    from . import profile_hints as _profile_hints
except ImportError:  # optional (XDASH_PLAN.md §4.1's "If missing" column)
    _profile_hints = None

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096  # never re-wrap a long comment/string line — see migrate_profile.py


class ProfileError(ValueError):
    """Expected failure (bad patch, a resulting profile Settings can't load) —
    routes map this to a 4xx, never a 500."""


# Keys bound once at process start and never touched by settings.reload()
# (backend/config.py's own Settings.reload() docstring) — PATCHing one still
# writes the file (so it takes effect on the next restart), it just can't be
# applied live, and GET marks it for the UI (XDASH_PLAN.md §8.6's ⟳).
_RESTART_REQUIRED = ("server_host", "server_port", "api_token", "server.host", "server.port", "server.api_token")


def _profile_path(profile_name: Optional[str] = None) -> Path:
    # Module-qualified (`config_mod.REPOS_DIR`, not `from .config import
    # REPOS_DIR`): the test harness (and anything else) that monkeypatches
    # config.REPOS_DIR must be seen here too, not just by config.py's own
    # other callers — a `from module import name` binding would freeze the
    # value this module saw at import time instead.
    return config_mod.REPOS_DIR / ("%s.yaml" % (profile_name or settings.profile_name))


def restart_required(dotted: str) -> bool:
    return dotted in _RESTART_REQUIRED or any(dotted.startswith(p + ".") for p in _RESTART_REQUIRED)


def _to_plain(value: Any) -> Any:
    """CommentedMap/CommentedSeq -> plain dict/list/scalars, JSON-safe."""
    if isinstance(value, dict):
        return {str(k): _to_plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_plain(v) for v in value]
    return value


def _first_paragraph(text: str) -> str:
    return text.split("\n\n")[0].lstrip("#").replace("\n#", " ").replace("\n", " ").strip()


def _extract_comments(doc: Any, prefix: str = "") -> Dict[str, str]:
    """{dotted_key: help text}, best-effort — ruamel attaches a key's own
    same-line comment *and* everything up to the next key's line as one
    token (see backend/migrate_profile.py's docstring for the same quirk),
    so this only ever takes the first paragraph, and a key can end up
    showing text that actually documents its successor. Good enough for
    "hover to see what this does"; not a source of truth for anything else."""
    out: Dict[str, str] = {}
    if not isinstance(doc, CommentedMap):
        return out
    for key in doc:
        dotted = "%s.%s" % (prefix, key) if prefix else str(key)
        item = doc.ca.items.get(key)
        if item and item[2] is not None and item[2].value.strip():
            out[dotted] = _first_paragraph(item[2].value)
        if isinstance(doc[key], CommentedMap):
            out.update(_extract_comments(doc[key], dotted))
    return out


def _dotted_keys(doc: Any, prefix: str = "") -> list:
    keys = []
    if not isinstance(doc, CommentedMap):
        return keys
    for key in doc:
        dotted = "%s.%s" % (prefix, key) if prefix else str(key)
        keys.append(dotted)
        if isinstance(doc[key], CommentedMap):
            keys.extend(_dotted_keys(doc[key], dotted))
    return keys


def get_profile(profile_name: Optional[str] = None) -> Dict[str, Any]:
    path = _profile_path(profile_name)
    if not path.is_file():
        raise ProfileError("Unknown profile %r (%s not found)" % (profile_name, path))
    text = path.read_text()
    doc = _yaml.load(text) or CommentedMap()
    keys = _dotted_keys(doc)
    return {
        "profile_name": profile_name or settings.profile_name,
        "text": text,
        "parsed": _to_plain(doc),
        "comments": _extract_comments(doc),
        "hints": {k: getattr(_profile_hints, "HINTS", {}).get(k) for k in keys if _profile_hints} if _profile_hints else {},
        "restart_required": [k for k in keys if restart_required(k)],
        "mtime": path.stat().st_mtime,
    }


def _apply_patch(doc: CommentedMap, patch: Dict[str, Any]) -> None:
    if not isinstance(patch, dict) or not patch:
        raise ProfileError("PATCH body must be a non-empty object of {dotted.key: value}")
    for dotted, value in patch.items():
        if not isinstance(dotted, str) or not dotted.strip():
            raise ProfileError("Bad key %r" % (dotted,))
        *parts, leaf = dotted.split(".")
        if not leaf or any(not p for p in parts):
            raise ProfileError("Bad dotted key %r" % dotted)
        node = doc
        for part in parts:
            if part not in node or not isinstance(node[part], CommentedMap):
                node[part] = CommentedMap()
            node = node[part]
        node[leaf] = value


def _validate(text: str, profile_name: str) -> None:
    """Constructs a throwaway Settings from *text*, written to a private
    temp profile file first — so a bad patch is refused with the real
    loader's own error, and nothing about the real profile is touched
    until this succeeds. The throwaway's own per-profile state dir (created
    by Settings.__init__ as a side effect) is removed again either way."""
    tmp_name = ".validate_%s_%d_%d" % (profile_name, os.getpid(), id(text) & 0xFFFFFF)
    tmp_path = config_mod.REPOS_DIR / (tmp_name + ".yaml")
    tmp_path.write_text(text)
    tmp_settings = None
    try:
        tmp_settings = config_mod.Settings(tmp_name)
    except Exception as e:
        raise ProfileError("Patched profile fails to load: %s" % e)
    finally:
        tmp_path.unlink(missing_ok=True)
        if tmp_settings is not None:
            shutil.rmtree(tmp_settings.state_dir, ignore_errors=True)


def patch_profile(patch: Dict[str, Any], profile_name: Optional[str] = None) -> Dict[str, Any]:
    name = profile_name or settings.profile_name
    path = _profile_path(name)
    if not path.is_file():
        raise ProfileError("Unknown profile %r (%s not found)" % (name, path))
    doc = _yaml.load(path.read_text()) or CommentedMap()
    _apply_patch(doc, patch)
    buf = io.StringIO()
    _yaml.dump(doc, buf)
    new_text = buf.getvalue()
    _validate(new_text, name)
    atomic_write_text(path, new_text)
    if name == settings.profile_name:
        settings.reload(name)
    return get_profile(name)


def put_raw(text: str, profile_name: Optional[str] = None) -> Dict[str, Any]:
    """Replaces the whole file with *text* verbatim — the Settings "Raw
    YAML" escape hatch (XDASH_PLAN.md §8.6). Comments/order are whatever the
    caller's text says; nothing here reformats it."""
    name = profile_name or settings.profile_name
    if not (text or "").strip():
        raise ProfileError("Raw profile text can't be empty")
    _validate(text, name)
    atomic_write_text(_profile_path(name), text)
    if name == settings.profile_name:
        settings.reload(name)
    return get_profile(name)
