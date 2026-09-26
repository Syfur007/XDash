"""XDash's adapter for the dissert framework (XDASH_PLAN.md §4.2/§4.4).

Loaded by bridge_scripts/call.py for hooks spelled `xdash:dissert.<fn>`, in
dissert's own interpreter with the dissert repo root as cwd and on
PYTHONPATH. It imports dissert's modules, but lives in XDash: dissert needs
no XDash-aware code.

Python 3.8-compatible (dissert's `thesis` env), and import-light at module
scope — nothing here may pull in torch.
"""
from __future__ import annotations

import os


def _load_config():
    """dissert's config loader — new src/dissert/ package path first, old
    flat layout as a fallback (XDASH_PROGRESS.md's Phase 2 "dissert-reorg
    switch-over checklist"). Both expose the same `load_config(path,
    validate=True) -> dict` signature, so nothing else here needs to change
    when the reorg lands."""
    try:
        from dissert.config.loader import load_config
    except ImportError:
        from utils.config import load_config
    return load_config


def _runid_module():
    try:
        from dissert.orchestration import runid
    except ImportError:
        from orchestration import runid
    return runid


def resolve_config(path):
    """`hooks.resolve_config` (§4.3): the framework's own compose-merge +
    schema validation, used to validate an experiment's overlay at
    composition time. A thin wrapper (rather than pointing the profile at
    `dissert.config.loader:load_config`/`utils.config:load_config` directly)
    so the same profile key works before and after the reorg."""
    return _load_config()(path)


def locate_run(config, seed, repeat=None):
    """Where dissert will write the run for (*config*, *seed*), known before
    launch (§4.4). *config* is repo-relative, exactly as it goes on the
    train.py command line.

    Mirrors what `train.py --config <config> --seeds <seed> --repeats 1`
    does through orchestration.runner.run_sweep: `load_config()` →
    `config_hash()` (which strips training.seed, logging, output_dir,
    checkpoint bookkeeping and stats) → `experiment_paths()`. `repeat` stays
    None for `--repeats 1` (run_sweep's "no repeat axis"), which is the
    unsuffixed `<hash7>-s<seed>/` dir and `R-<hash7>-s<seed>-f<fold|->` ids.

    Returns {config_hash, experiment_name, run_dir, run_ids, fold_splits}:
    run_dir and fold_splits relative to the repo root when the config's
    output_dir is, run_ids one per fold (one `-f-` id for a non-CV run).
    """
    load_config = _load_config()
    runid = _runid_module()
    config_hash, experiment_paths, run_id = runid.config_hash, runid.experiment_paths, runid.run_id

    seed = int(seed)
    resolved = load_config(config)
    h = config_hash(resolved)
    name = (resolved.get("logging") or {}).get("experiment_name") or "experiment"
    output_dir = resolved.get("output_dir", "outputs/experiments")
    paths = experiment_paths(output_dir, name, h, seed, None, repeat)

    # Same fold selection as train.py's sweep path when no --fold is given.
    kfold = resolved.get("k_fold") or {}
    if kfold.get("enabled"):
        folds = list(kfold.get("run_folds") or range(int(kfold.get("n_splits", 5))))
    else:
        folds = [None]

    return {
        "config_hash": h,
        "experiment_name": name,
        "run_dir": os.path.normpath(paths["root"]),
        "run_ids": [run_id(h, seed=seed, fold=f, repeat=repeat) for f in folds],
        "fold_splits": os.path.normpath(paths["fold_splits"]) if kfold.get("enabled") else None,
    }
