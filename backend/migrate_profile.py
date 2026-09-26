"""One-shot script: restructures `repos/<profile>.yaml`'s flat keys into the
sectioned profile shape XDASH_PLAN.md §4.1 describes (`framework:`,
`commands:`, `metrics:`), using ruamel.yaml's round-trip mode so comments and
key order survive the restructuring. Run once per profile:

    conda run -n xdash python -m backend.migrate_profile dissert
    conda run -n xdash python -m backend.migrate_profile segpriors

Idempotent: a key that's already been moved (or a profile with no legacy
flat keys left to move) is a no-op — safe to run again.

**Not required for the profile to keep working.** `backend/config.py`'s
`_get()` helper reads the sectioned key first, the legacy flat key second —
an unmigrated profile is exactly as valid as a migrated one. This script
only exists so a profile's *file* documents itself in the shape §4.1
describes, for Settings (Phase 4) to build a form from.

Comment preservation here is best-effort, not byte-exact: a key that moves
into a new section carries its own end-of-line/preceding comment along
(ruamel's `.ca.items`), but a section header's own comment can end up
re-anchored slightly differently once several keys move under it. The
migration's real acceptance bar (XDASH_PLAN.md §10 Phase 2) is *functional*
equivalence — the legacy-read settings of the pre-migration text must equal
the sectioned-read settings of the post-migration text — proved by
tests/test_profile_migration.py, not a byte-diff of this script's own
output. Byte-exact comment preservation *is* required, and tested, for the
`/api/profile` PATCH endpoint (backend/profile_ops.py), which only ever
changes one already-sectioned key's value, never restructures the file.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import List, Tuple

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

REPOS_DIR = Path(__file__).resolve().parent.parent / "repos"

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096  # never re-wrap a long comment/string line

# (legacy flat key, new dotted path under framework:/outputs:) — moved, not
# duplicated. A profile that doesn't declare a legacy key is left alone.
_MOVES: List[Tuple[str, str]] = [
    ("repo_root", "framework.repo_root"),
    ("configs_dir", "framework.configs_dir"),
    ("dataset_name_key", "framework.dataset_name_key"),
    ("dataset_root_key", "framework.dataset_root_key"),
    ("overlay_compose_key", "framework.overlay_compose_key"),
    ("run_id_pattern", "outputs.run_id_pattern"),
]

_DEFAULT_LOWER_IS_BETTER = ["hd95", "asd", "mean_ms", "median_ms", "std_ms", "p95_ms", "eval_duration_s", "ece"]


def _ensure_section(doc: CommentedMap, name: str) -> CommentedMap:
    if name not in doc or not isinstance(doc[name], CommentedMap):
        doc[name] = CommentedMap()
    return doc[name]


_ABSENT = object()


def _pop_key(doc: CommentedMap, key: str):
    """Removes doc[key], returning (value, comment) — (_ABSENT, None) if
    *key* isn't present. The building block both _move_key() and
    _build_commands() use.

    **Known comment-fidelity limitation** (documented, not fixed — see the
    module docstring): ruamel attaches a key's own same-line comment *and*
    every blank line/comment that follows it up to the next key's line as
    ONE token on *this* key's `ca.items` entry. So the paragraph a human
    reads as documenting the *next* key travels here too, and ends up wherever
    this key's value lands post-migration — usually still readable and
    adjacent to related settings, but not always the same key it originally
    explained. An attempt at splitting/reattaching that remainder onto the
    correct neighbor made real files measurably harder to read (glued onto an
    unrelated key's own line) rather than better, so it was reverted in favor
    of this simpler, honest behavior."""
    if key not in doc:
        return _ABSENT, None
    value = doc.pop(key)
    comment = doc.ca.items.pop(key, None)
    return value, comment


def _move_key(doc: CommentedMap, old_key: str, dotted_new: str) -> bool:
    """Moves doc[old_key] (and its own same-line comment, if any) to the
    nested *dotted_new* path. False when *old_key* isn't present."""
    value, comment = _pop_key(doc, old_key)
    if value is _ABSENT:
        return False
    *section_parts, leaf = dotted_new.split(".")
    node = doc
    for part in section_parts:
        node = _ensure_section(node, part)
    node[leaf] = value
    if comment is not None:
        node.ca.items[leaf] = comment
    return True


def _build_commands(doc: CommentedMap) -> bool:
    """Synthesizes `commands.train`/`eval`/`budget`/`resume` from the
    profile's existing train_script/eval_script/seed_arg/eval_default_args —
    the exact template backend/config.py's `_default_command_template()`
    would synthesize in memory for an unmigrated profile, just written out
    literally now. `python_executable`/`env_activate_cmd` become
    `commands.python`/`commands.env_activate`. False when there was nothing
    to build (already sectioned, or a from-scratch profile with no legacy
    keys at all — commands.train/eval have to come from *somewhere*)."""
    if "commands" in doc and isinstance(doc["commands"], CommentedMap) and "train" in doc["commands"]:
        return False  # already has its own commands section
    if "train_script" not in doc and "python_executable" not in doc:
        return False
    commands = _ensure_section(doc, "commands")
    train_script, _c = _pop_key(doc, "train_script")
    train_script = "train.py" if train_script is _ABSENT else train_script
    eval_script, _c = _pop_key(doc, "eval_script")
    eval_script = "eval.py" if eval_script is _ABSENT else eval_script
    seed_arg, _c = _pop_key(doc, "seed_arg")
    seed_arg = str(seed_arg or "").strip() if seed_arg is not _ABSENT else ""

    # Mirrors backend/config.py's _default_command_template() exactly —
    # `eval_default_args` deliberately stays a separate top-level key rather
    # than getting baked into commands.eval's own text: framework.
    # render_command() prepends it at render time for *every* profile,
    # migrated or not (XDASH_PLAN.md §4.1), so baking it in here too would
    # double it up for a migrated one.
    seed_part = ("%s " % seed_arg) if seed_arg else ""
    python_exe, python_comment = _pop_key(doc, "python_executable")
    commands["python"] = "python" if python_exe is _ABSENT else python_exe
    if python_comment is not None:
        commands.ca.items["python"] = python_comment
    env_activate, env_comment = _pop_key(doc, "env_activate_cmd")
    if env_activate is not _ABSENT:
        commands["env_activate"] = env_activate
        if env_comment is not None:
            commands.ca.items["env_activate"] = env_comment
    commands["train"] = "{python} %s --config {config} %s{budget} {resume} {extra}" % (train_script, seed_part)
    commands["eval"] = "{python} %s --config {config} %s{extra}" % (eval_script, seed_part)
    commands["budget"] = "--max-hours {hours}"
    commands["resume"] = "--resume"
    return True


def _build_metrics(doc: CommentedMap) -> bool:
    """`metrics.lower_is_better` (XDASH_PLAN.md §4.1) — moves the list out of
    static/app.js's own hardcoded constant. False when a profile already has
    its own `metrics:` section."""
    if "metrics" in doc:
        return False
    metrics = _ensure_section(doc, "metrics")
    metrics["lower_is_better"] = list(_DEFAULT_LOWER_IS_BETTER)
    return True


def migrate(path: Path) -> bool:
    """Rewrites *path* in place. Returns True if anything changed."""
    doc = _yaml.load(path.read_text())
    changed = False
    for old_key, new_dotted in _MOVES:
        changed = _move_key(doc, old_key, new_dotted) or changed
    changed = _build_commands(doc) or changed
    changed = _build_metrics(doc) or changed
    if not changed:
        return False
    buf = io.StringIO()
    _yaml.dump(doc, buf)
    path.write_text(buf.getvalue())
    return True


def main(argv: List[str]) -> int:
    if not argv:
        print("usage: python -m backend.migrate_profile <profile-name> [<profile-name> ...]", file=sys.stderr)
        return 1
    status = 0
    for name in argv:
        path = REPOS_DIR / ("%s.yaml" % name)
        if not path.is_file():
            print("skip %s: not found" % path, file=sys.stderr)
            status = 1
            continue
        print("migrated %s" % path if migrate(path) else "%s: already sectioned, nothing to do" % path)
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
