"""The framework-facing half of dispatch: what XDash asks the host repo, and
how it builds each stage's arguments (XDASH_PLAN.md §4, the Phase 0 slice).

- `locate_run()` — the planned run dir and run ids, asked of the profile's
  `hooks.locate_run` before launch and stored on the attempt (§4.4, X2).
  Every reader (classification, collection, canonicalization) reads that
  record; nothing reconstructs a run path from an experiment id any more.
- `code_state()` — {commit, dirty, pushed} of the host repo, recorded on
  every attempt at dispatch (§4.5, X6) and gating Kaggle (`code-not-pushed`).
- `stage_args()` — the train and eval halves' extra arguments, built
  separately: the seed flags go to both, the experiment's own
  `extra_args.train` only to train, `extra_args.eval` only to eval. Since
  Phase 1 these are the escape hatch for non-config flags only.
- The **overlay** (§4.3, Phase 1; closes X12): an experiment's config
  overrides, as dotted keys, written at dispatch as a config file that
  composes the experiment's own config. Train and eval both get `--config`
  that file (`cli_config()`), and so does `locate_run()`, so all three
  resolve the same config and the same config_hash — which a config-changing
  CLI flag never could (the Phase 0 `run-mismatch` gap).
- `parse_run_ids()` / `run_mismatch()` — the `run_id=` lines the framework
  prints, checked against the plan after the fact (§4.4's `run-mismatch`).

Phase 2 moves the flat profile keys this reads (seed_arg, hooks,
overlay_compose_key) into the sectioned profile. That changes what's behind
these functions, not their callers.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from . import bridge
from . import configs as cfg
from .config import settings
from .store import atomic_write_text


# --------------------------------------------------------------------------- run location
def locate_run(config_path: str, seed: Any, cli_config: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The planned run for (*config_path*, *seed*) via the profile's
    `locate_run` hook: {config, config_hash, experiment_name, run_dir,
    run_ids}, or {"error": ...} when it can't be answered. None when the
    profile declares no hook at all — a missing hook degrades classification
    and canonical collection, never dispatch (§4's "If missing" column).

    *cli_config* is the repo-relative path train and eval get as `--config`
    (an experiment's overlay file, see cli_config()); the hook is asked about
    exactly that file, so the plan is computed on the same config the run
    resolves. Defaults to *config_path*'s own repo-relative path."""
    hook = settings.hooks.get("locate_run")
    if not hook:
        return None
    if seed in (None, ""):
        # A seed-less launch runs the framework's own default multi-seed
        # sweep: several run dirs, none of which is "the" run.
        return {"error": "No seed: a seed-less launch runs several runs, so there is no single run dir to plan"}
    try:
        config = cli_config or cfg.repo_relative_path(config_path)
        result = bridge.call_hook(hook, {"config": config, "seed": int(seed)})
    except (bridge.BridgeError, bridge.BridgeUnavailable, ValueError, TypeError) as e:
        return {"error": str(e)[:500]}
    if not isinstance(result, dict) or not result.get("run_dir"):
        return {"error": "locate_run hook %r returned no run_dir: %r" % (hook, result)}
    run_ids = result.get("run_ids") or []
    return {
        "config": config,
        "config_hash": result.get("config_hash"),
        "experiment_name": result.get("experiment_name"),
        "run_dir": str(result["run_dir"]),
        "run_ids": [str(r) for r in run_ids] if isinstance(run_ids, list) else [],
    }


# --------------------------------------------------------------------------- overlay (§4.3)
# Repo-relative, on every runtime: the file's `compose` entry is relative to
# it, so the same text works wherever the repo is checked out. `.xdash/` is
# the one path XDash writes inside the host repo's source tree (the host
# repo's .gitignore should list it).
XDASH_DIR = ".xdash"
OVERLAY_DIR = XDASH_DIR + "/overlays"
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]")


class OverlayError(ValueError):
    """An overlay that is malformed, or that the framework's own loader
    rejects (a typo'd key, a wrong type). Expected; routes map it to 4xx."""


def normalize_overlay(value: Any) -> Dict[str, Any]:
    """An overlay as XDash stores it: a flat mapping of dotted keys to
    values, keys sorted — `{"training.epochs": 200}`. Accepts that form, the
    nested form (`{"training": {"epochs": 200}}`), or a mix; None/{} is the
    empty overlay. Refuses a key that is a prefix of another
    (`training` and `training.epochs`: which one wins would depend on merge
    order), an empty key segment, the loader's own include key, and values
    that aren't plain YAML (JSON-serializable) data."""
    if value is None or value == "" or value == {}:
        return {}
    if not isinstance(value, dict):
        raise OverlayError("overlay must be a mapping of dotted keys to values, got %s" % type(value).__name__)
    flat: Dict[str, Any] = {}

    def walk(prefix: str, node: Dict[Any, Any]) -> None:
        for raw_key, v in node.items():
            k = str(raw_key).strip()
            key = "%s.%s" % (prefix, k) if prefix else k
            if isinstance(v, dict) and v:
                walk(key, v)
            elif key in flat:
                raise OverlayError("overlay key %r is given twice" % key)
            else:
                flat[key] = v

    walk("", value)
    reserved = {settings.overlay_compose_key or "compose"}
    for key, v in flat.items():
        parts = key.split(".")
        if any(not p for p in parts):
            raise OverlayError("overlay key %r has an empty segment" % key)
        if parts[0] in reserved:
            raise OverlayError("overlay key %r would replace the overlay's own include" % key)
        try:
            json.dumps(v, allow_nan=False)
        except (TypeError, ValueError):
            raise OverlayError("overlay value for %r is not plain YAML data: %r" % (key, v))
    keys = sorted(flat)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if b.startswith(a + "."):
                raise OverlayError("overlay keys %r and %r conflict (one is inside the other)" % (a, b))
    return {k: flat[k] for k in keys}


def overlay_digest(overlay: Any) -> str:
    """sha1 of the canonical overlay, first 6 hex chars; "" for the empty
    overlay. The identity suffix of an experiment id (§3.3.1): two
    different overlays can never share a record (X8)."""
    flat = normalize_overlay(overlay)
    if not flat:
        return ""
    canonical = json.dumps(flat, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:6]


def overlay_supported() -> bool:
    return bool(settings.overlay_compose_key)


def overlay_rel_path(experiment_id: str) -> str:
    return "%s/%s.yaml" % (OVERLAY_DIR, _UNSAFE_FILENAME.sub("_", experiment_id))


def _nest(flat: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in flat.items():
        node = out
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return out


def render_overlay(config_path: str, overlay: Any, include: Optional[str] = None) -> str:
    """The overlay file's text: one include (*include*, default the
    experiment's config relative to OVERLAY_DIR — `../../configs/...`),
    then the overrides as nested YAML. Deterministic for a given input."""
    if not overlay_supported():
        raise OverlayError("Profile '%s' declares no overlay_compose_key, so it can't compose an overlay" % settings.profile_name)
    flat = normalize_overlay(overlay)
    if include is None:
        include = posixpath.relpath(cfg.repo_relative_path(config_path), OVERLAY_DIR)
    doc: Dict[str, Any] = {settings.overlay_compose_key: [include]}
    doc.update(_nest(flat))
    header = (
        "# Written by XDash at dispatch (XDASH_PLAN.md §4.3); regenerated every time.\n"
        "# train and eval both run with --config this file.\n"
    )
    return header + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, allow_unicode=True)


def cli_config(experiment: Dict[str, Any]) -> str:
    """What train and eval (and locate_run) get as `--config`: the overlay
    file when the experiment has an overlay, else its own config."""
    if normalize_overlay(experiment.get("overlay")):
        return overlay_rel_path(experiment["experiment_id"])
    return cfg.repo_relative_path(experiment["config_path"])


def overlay_text(experiment: Dict[str, Any]) -> str:
    """The overlay file's content for *experiment*, "" when it has none."""
    if not normalize_overlay(experiment.get("overlay")):
        return ""
    return render_overlay(experiment["config_path"], experiment["overlay"])


def write_overlay(experiment: Dict[str, Any], repo_root: Optional[Path] = None) -> Optional[str]:
    """Writes *experiment*'s overlay file under *repo_root* (default: the
    local host repo) and returns its repo-relative path, or None when there
    is no overlay. Every dispatch writes the local copy, whatever the
    runtime: locate_run reads it here. Runners that execute elsewhere carry
    it over themselves (MachineRunner: the transport; Kaggle: OVERLAY_YAML
    in the launch spec)."""
    text = overlay_text(experiment)
    if not text:
        return None
    rel = overlay_rel_path(experiment["experiment_id"])
    atomic_write_text(Path(repo_root or settings.repo_root) / rel, text)
    return rel


def validate_overlay(config_path: str, overlay: Any) -> Optional[str]:
    """Checks *overlay* on *config_path* through the profile's
    `resolve_config` hook (the framework's own loader and schema) at
    composition time, so a typo'd key fails when the experiment is created,
    not hours into a run (§4.3). Raises OverlayError when the framework
    rejects it; returns a warning string when it couldn't be checked (no
    hook, bridge unavailable); None when it resolved.

    The probe file lives in a throwaway directory and names the config by
    absolute path: the loader resolves either spelling to the same file, so
    the merged config is the one the real overlay will produce, and nothing
    is written into the host repo for an experiment that may never run."""
    flat = normalize_overlay(overlay)
    if not flat:
        return None
    if not overlay_supported():
        raise OverlayError("Profile '%s' declares no overlay_compose_key, so experiments can't carry an overlay" % settings.profile_name)
    hook = settings.hooks.get("resolve_config")
    if not hook:
        return "overlay not validated: the profile declares no hooks.resolve_config"
    include = str((settings.repo_root / cfg.repo_relative_path(config_path)).resolve())
    tmpdir = Path(tempfile.mkdtemp(prefix="xdash_overlay_"))
    try:
        probe = tmpdir / "overlay.yaml"
        probe.write_text(render_overlay(config_path, flat, include=include))
        bridge.call_hook(hook, [str(probe)])
    except bridge.BridgeError as e:
        raise OverlayError("Overlay %s doesn't resolve on %s: %s" % (json.dumps(flat), config_path, str(e)[:800]))
    except bridge.BridgeUnavailable as e:
        return "overlay not validated: %s" % str(e)[:300]
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return None


def parse_run_ids(text: str) -> List[str]:
    """Every run id the framework printed, in order, via the profile's
    run_id_pattern (dissert: `run_id=R-f76c81e-s42-f- status=done ...`)."""
    try:
        pattern = re.compile(settings.run_id_pattern)
    except re.error:
        return []
    return [m.group(1) for m in pattern.finditer(text or "")]


def run_mismatch(planned: List[str], log_texts: List[str]) -> Optional[Dict[str, Any]]:
    """The `run-mismatch` flag (§4.4) when the logs name run ids and none of
    them is one XDash planned — e.g. a config-changing train extra arg like
    `--epochs 50` (what the overlay is for) changed the config hash, so the
    run landed in a different dir than locate_run predicted. XDash flags
    this instead of guessing at the other dir. None when there's nothing to
    compare, or they agree."""
    if not planned:
        return None
    seen: List[str] = []
    for text in log_texts:
        for rid in parse_run_ids(text):
            if rid not in seen:
                seen.append(rid)
    if not seen or any(rid in planned for rid in seen):
        return None
    return {
        "code": "run-mismatch",
        "detail": "Framework reported run id(s) %s, planned %s" % (", ".join(seen[:4]), ", ".join(planned[:4])),
        "reported": seen, "planned": list(planned),
    }


# --------------------------------------------------------------------------- code provenance
_CODE_TTL_SECONDS = 10.0
_code_lock = threading.Lock()
_code_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    # --no-optional-locks: `git status` otherwise refreshes the host repo's
    # index (taking .git/index.lock) — XDash only ever reads the host repo.
    return subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args],
                          capture_output=True, text=True, timeout=20)


def code_state(repo_root: Optional[Path] = None, fresh: bool = False) -> Dict[str, Any]:
    """{commit, dirty, pushed} for the host repo (X6).

    - `dirty`: tracked files differ from `commit` (untracked files don't
      count — outputs/, scratch notes and the like would otherwise block
      every Kaggle dispatch; a new *tracked* source file does count).
    - `pushed`: `commit` is on some remote-tracking branch. Local refs only,
      no network: a push made from another clone shows as unpushed until
      the next `git fetch`.

    Cached for a few seconds — KaggleRunner.can_accept() asks once per
    pending experiment per dispatch tick. `fresh=True` (dispatch) bypasses
    the cache."""
    root = Path(repo_root or settings.repo_root)
    key = str(root)
    now = time.monotonic()
    if not fresh:
        with _code_lock:
            cached = _code_cache.get(key)
            if cached is not None and now - cached[0] < _CODE_TTL_SECONDS:
                return dict(cached[1])
    try:
        head = _git(root, "rev-parse", "HEAD")
        if head.returncode != 0:
            state = {"commit": None, "dirty": None, "pushed": None, "error": (head.stderr or "not a git repo").strip()[-200:]}
        else:
            commit = head.stdout.strip()
            status = _git(root, "status", "--porcelain", "--untracked-files=no")
            remote = _git(root, "branch", "-r", "--contains", commit)
            state = {
                "commit": commit,
                "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
                "pushed": bool(remote.stdout.strip()) if remote.returncode == 0 else None,
            }
    except (OSError, subprocess.TimeoutExpired) as e:
        state = {"commit": None, "dirty": None, "pushed": None, "error": str(e)[:200]}
    with _code_lock:
        _code_cache[key] = (now, state)
    return dict(state)


def code_is_pinnable(code: Dict[str, Any]) -> bool:
    """Would a fresh clone from GitHub reproduce exactly this code? Only a
    pushed commit with a clean tree."""
    return bool(code.get("commit")) and code.get("pushed") is True and code.get("dirty") is False


# --------------------------------------------------------------------------- stage arguments
_SEED_FLAGS = ("--seed", "--seeds")


def normalize_extra_args(value: Any, seed: Any = None) -> Dict[str, str]:
    """`extra_args` as {"train": str, "eval": str} (Phase 0's X12 split;
    since Phase 1 the escape hatch for non-config flags such as
    `--max-hours` — config changes belong in the overlay).

    A legacy single string becomes train-only: eval.py rejects any flag it
    doesn't know with exit 2, so the only split that can never crash eval is
    to give it nothing it didn't ask for. XDash used to bake the seed flag
    into that string at creation (`--seed 42`); a seed flag naming the
    experiment's own seed is dropped, since stage_args() now adds the
    profile's current seed_arg to both halves at dispatch."""
    if isinstance(value, dict):
        return {"train": str(value.get("train") or "").strip(), "eval": str(value.get("eval") or "").strip()}
    text = str(value or "").strip()
    if not text:
        return {"train": "", "eval": ""}
    try:
        tokens = shlex.split(text)
    except ValueError:
        return {"train": text, "eval": ""}
    kept: List[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if seed not in (None, "") and tok in _SEED_FLAGS and nxt == str(seed):
            i += 2
            if i + 1 < len(tokens) and tokens[i] == "--repeats" and tokens[i + 1] == "1":
                i += 2
            continue
        kept.append(tok)
        i += 1
    return {"train": " ".join(shlex.quote(t) for t in kept), "eval": ""}


def seed_args(seed: Any) -> str:
    if seed in (None, "") or not settings.seed_arg:
        return ""
    return settings.seed_arg.format(seed=seed)


def stage_args(experiment: Dict[str, Any]) -> Tuple[str, str]:
    """(train_extra_args, eval_extra_args) for *experiment*: the seed flags
    on both (dissert: `--seeds 42 --repeats 1`, X1), then each stage's own
    extra args on that stage only. Resume/budget flags are the runner's
    business and are added by it (train only).

    Legacy: this is what fed a runtime's command line before Phase 2's
    `commands.*` templates (§4.1) folded the seed flag's own spelling into
    the template itself (`--seeds {seed} --repeats 1`, not a profile-wide
    string baked into extra_args). Still used for Kaggle's
    TRAIN_EXTRA_ARGS/EVAL_EXTRA_ARGS placeholders, which an older
    template-predates-the-split notebook (e.g. data/segpriors/
    kaggle_worker_template.ipynb) still substitutes directly. `extra_only()`
    below is the seed-free equivalent `render_command()` wants."""
    extra = normalize_extra_args(experiment.get("extra_args"), experiment.get("seed"))
    seeds = seed_args(experiment.get("seed"))
    join = lambda *parts: " ".join(p for p in parts if p)  # noqa: E731
    return join(seeds, extra["train"]), join(seeds, extra["eval"])


def extra_only(experiment: Dict[str, Any]) -> Tuple[str, str]:
    """(train_extra, eval_extra) for *experiment* with **no** seed flag baked
    in — what render_command()'s `{extra}` placeholder wants, since the
    `commands.train`/`commands.eval` template already spells the seed flag
    itself (e.g. `--seeds {seed} --repeats 1`). Unlike stage_args(), nothing
    here is profile-specific string-building; it's just each stage's own
    escape-hatch extra_args."""
    extra = normalize_extra_args(experiment.get("extra_args"), experiment.get("seed"))
    return extra["train"], extra["eval"]


_WHITESPACE_RUN = re.compile(r" {2,}")


def render_command(
    config: str, seed: Any, python: str, stage: str, *,
    budget_hours: Optional[float] = None, resume: bool = False, extra: str = "",
) -> str:
    """The full command line for *stage* ("train"/"eval") against *config*
    (repo-relative — the experiment's overlay path when it has one, from
    cli_config(), for both stages) and *seed*, rendered from the profile's
    `commands.<stage>` template (XDASH_PLAN.md §4.1/§4.5) — the single
    source every runtime kind (local, ssh, colab, kaggle) builds its launch
    command from, so the same experiment runs the exact same command line
    everywhere (modulo *python*, which is per-runtime: a host's own
    interpreter, or Kaggle's venv).

    Placeholders: {python} {config} {seed} {budget} {resume} {extra}. *extra*
    is each stage's own escape-hatch args (framework.extra_only()); eval
    additionally gets the profile's `eval_default_args` prepended (mirrors
    the pre-template-era tmux_runner.build_launch_command() ordering: default
    flags before the caller's own extra args). Raises ValueError when the
    profile declares no template for *stage* (a hand-emptied `commands.*`
    key — the default synthesized in backend/config.py is never blank)."""
    template = settings.commands.get(stage)
    if not template:
        raise ValueError("Profile '%s' declares no commands.%s template" % (settings.profile_name, stage))
    if stage == "eval" and settings.eval_default_args:
        flags = " ".join(shlex.quote(a) for a in settings.eval_default_args)
        extra = ("%s %s" % (flags, extra)).strip() if extra else flags
    rendered = template.format(
        python=python,
        config=config,
        seed="" if seed in (None, "") else str(seed),
        budget=settings.commands["budget"].format(hours=budget_hours) if budget_hours else "",
        resume=settings.commands["resume"] if resume else "",
        extra=extra or "",
    )
    # Collapses the double spaces an empty placeholder leaves (e.g.
    # "{budget} {resume}" both blank) without touching any other whitespace
    # — a narrower normalization than str.split()/" ".join(), which would
    # also collapse a meaningful run of spaces inside a quoted extra arg.
    return _WHITESPACE_RUN.sub(" ", rendered).strip()
