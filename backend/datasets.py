"""The dataset registry + resolver (XDASH_PLAN.md §5; fixes X4).

Replaces `dataset_map.json`'s narrow "dataset name -> Kaggle slug" map with a
per-dataset record that also carries per-runtime **bindings** — how that
dataset's files get to a runtime's filesystem before a launch: `path` (it's
already there), `push` (rsync a local copy up), `fetch` (download it on the
runtime), or `attach` (Kaggle's declarative dataset_sources). `dataset_map.json`
keeps working underneath (backend/dataset_map.py is untouched — Kaggle's own
`attach` mode and its `resolve_kaggle_dataset()` precedence rules are still
exactly what they were); this module is the layer *above* it that also
answers "how does ssh/colab get this dataset", not just "which Kaggle slug".

**`root` is never typed here.** A dataset's declared filesystem root
(`dataset.root` in a resolved config) is read fresh from its identity
fragment every time it's needed (`dataset_identity_for_config()`), because
`dataset.root` is part of dissert's `config_hash` (§5.1) — caching a stale
copy here would risk placement acting on a path the config itself no longer
declares.

Storage: `data/<profile>/datasets.json`, keyed by dataset name (case-folded,
same convention as dataset_map.py). Seeded in memory from dataset_map.json
the first time it's read and the file doesn't exist yet — same "migrate in
memory, persist on the first real write" pattern as backend/experiments.py's
JsonStore migrations (XDASH_PLAN.md §3.8): a bare GET/import never writes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from . import configs as cfg
from .config import settings
from .store import JsonStore
from .transport import Transport, TransportError

_store = JsonStore(lambda: settings.datasets_file, dict, sort_keys=True)

# §5.2's four placement modes.
MODES = ("path", "push", "fetch", "attach")


class DatasetError(ValueError):
    """Expected failure (unknown dataset, bad binding) — routes map to 4xx."""


class PlacementError(Exception):
    """A binding couldn't be resolved or executed — carries the same
    {code, detail} shape as any other `can_accept()` block (XDASH_PLAN.md
    §5.2's `no-dataset-binding`, `no-dataset-mapping` kept as an alias)."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(detail)


# --------------------------------------------------------------------------- registry storage
def _seed_from_dataset_map() -> Dict[str, Any]:
    """Migration source: dataset_map.json's existing name->slug entries, one
    record each, sources only (no bindings yet — every runtime falls back to
    its kind default until someone sets one explicitly). Read-only; never
    writes dataset_map.json itself."""
    from . import dataset_map
    seeded: Dict[str, Any] = {}
    for name, slug in dataset_map.load_dataset_map().items():
        seeded[name] = {"name": name, "sources": {"kaggle": {"slug": slug}}, "bindings": {}, "checks": {}}
    return seeded


def _load() -> Dict[str, Any]:
    if not _store.exists():
        return _seed_from_dataset_map()
    data = _store.load()
    return data if isinstance(data, dict) else {}


def _save(data: Dict[str, Any]) -> None:
    _store.save(data)


def _key(name: str) -> str:
    return (name or "").strip().casefold()


# A reserved key inside the same flat {dataset_key: record} dict — no real
# dataset name can collide with it (_key() only ever produces casefolded
# dataset names). Keeps the registry to one simple {name: record} shape
# instead of a second top-level envelope just for one setting.
_META_KEY = "__meta__"


def data_account() -> Optional[str]:
    """The Kaggle account whose credentials a `fetch` placement uses
    (XDASH_PLAN.md §5.2) — a property of the fleet, not of any one dataset,
    so it's one setting rather than repeated per binding."""
    return (_load().get(_META_KEY) or {}).get("data_account") or None


def set_data_account(name: Optional[str]) -> None:
    data = _load()
    meta = dict(data.get(_META_KEY) or {})
    meta["data_account"] = (name or "").strip() or None
    data[_META_KEY] = meta
    _save(data)


def data_account_creds() -> Optional[Dict[str, str]]:
    """`{KAGGLE_USERNAME, KAGGLE_KEY}` for the designated data account, read
    straight off its stored credentials (backend/kaggle.py's own
    `kaggle.json` file) — None when no account is designated or it has no
    legacy username/key pair stored (an access-token-only account can't
    authenticate `kagglehub` this way; register a username/key pair for the
    data account specifically)."""
    name = data_account()
    if not name:
        return None
    creds_path = settings.kaggle_creds_dir / name / "kaggle.json"
    if not creds_path.is_file():
        return None
    import json as _json
    try:
        pair = _json.loads(creds_path.read_text())
        return {"KAGGLE_USERNAME": str(pair["username"]), "KAGGLE_KEY": str(pair["key"])}
    except (OSError, ValueError, KeyError):
        return None


# --------------------------------------------------------------------------- identity (never typed)
def _dotted_get(raw: Any, dotted: str) -> Optional[Any]:
    node = raw
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _fragment_paths() -> List[Path]:
    try:
        return sorted(settings.configs_dir.glob(settings.dataset_fragment_glob))
    except (OSError, ValueError):
        return []


def _read_yaml(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = yaml.safe_load(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return None


def known_dataset_names() -> List[str]:
    """Every dataset name any identity fragment declares — configs_dir/
    `framework.fragments.dataset` glob (default `dataset/*.yaml`), read
    directly (no compose-walk needed: a dataset fragment is a leaf)."""
    names = []
    for path in _fragment_paths():
        raw = _read_yaml(path)
        name = _dotted_get(raw, settings.dataset_name_key)
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def dataset_identity_for_config(config_path: str) -> Tuple[Optional[str], Optional[str]]:
    """(dataset name, root) declared by *config_path* (configs_dir-relative),
    walking its `compose` fragments the same way the framework's own loader
    merges them — mirrors dataset_map.config_dataset_identity()'s walk, but
    also pulls `root` (§5.1: read from the fragment/resolved config, never
    typed). Both keys are configurable per profile (dataset_name_key/
    dataset_root_key); dissert's are `dataset.name`/`dataset.root`."""
    try:
        record = cfg.read_config(config_path)
    except (FileNotFoundError, ValueError):
        return None, None
    raw = record.get("parsed")
    if not isinstance(raw, dict):
        return None, None
    name = _dotted_get(raw, settings.dataset_name_key)
    root = _dotted_get(raw, settings.dataset_root_key)
    name = name.strip() if isinstance(name, str) and name.strip() else None
    root = root.strip() if isinstance(root, str) and root.strip() else None
    if name and root:
        return name, root
    config_dir = settings.configs_dir.resolve()
    config_abs = (config_dir / config_path).resolve()
    for fragment in raw.get("compose") or []:
        fragment_path = (config_abs.parent / str(fragment)).resolve()
        if config_dir not in fragment_path.parents or not fragment_path.is_file():
            continue
        frag_raw = _read_yaml(fragment_path)
        if frag_raw is None:
            continue
        name = name or _dotted_get(frag_raw, settings.dataset_name_key)
        root = root or _dotted_get(frag_raw, settings.dataset_root_key)
    name = name.strip() if isinstance(name, str) and name.strip() else None
    root = root.strip() if isinstance(root, str) and root.strip() else None
    return name, root


def configs_using_dataset(name: str) -> List[str]:
    """Configs (configs_dir-relative paths) whose resolved dataset identity
    matches *name* — best-effort, for the dataset detail page's "which
    configs use it" (XDASH_PLAN.md §8.5: "search configs for the dataset
    name key"). Walks every config's own compose chain via
    `dataset_identity_for_config()` (never a raw text grep, so a config
    that inherits its dataset from a fragment still matches). Excludes the
    dataset's own identity fragment(s) — those match themselves trivially,
    which isn't a real "usage"."""
    key = _key(name)
    fragment_paths = {p.resolve().as_posix() for p in _fragment_paths()}
    out: List[str] = []
    for group in cfg.list_configs():
        for item in group.get("configs") or []:
            path = item.get("path")
            if not path:
                continue
            abs_path = (settings.configs_dir / path).resolve()
            if abs_path.as_posix() in fragment_paths:
                continue
            found_name, _root = dataset_identity_for_config(path)
            if found_name and _key(found_name) == key:
                out.append(path)
    return sorted(out)


def root_for_dataset(name: str) -> Optional[str]:
    """The declared root for dataset *name*, resolved fresh from whichever
    identity fragment declares it (never the registry's own JSON — §5.1).
    None when no fragment claims this name."""
    key = _key(name)
    for path in _fragment_paths():
        raw = _read_yaml(path)
        frag_name = _dotted_get(raw, settings.dataset_name_key)
        if isinstance(frag_name, str) and _key(frag_name) == key:
            root = _dotted_get(raw, settings.dataset_root_key)
            return root.strip() if isinstance(root, str) and root.strip() else None
    return None


# --------------------------------------------------------------------------- CRUD (GET/PUT /api/datasets*)
def list_datasets() -> List[Dict[str, Any]]:
    data = _load()
    names = (set(data) | {_key(n) for n in known_dataset_names()}) - {_META_KEY}
    out = []
    for key in sorted(names):
        record = data.get(key) or {"name": key, "sources": {}, "bindings": {}, "checks": {}}
        out.append({
            "name": record.get("name") or key,
            "root": root_for_dataset(key),
            "sources": record.get("sources") or {},
            "bindings": record.get("bindings") or {},
            "checks": record.get("checks") or {},
        })
    return out


def get_dataset(name: str) -> Dict[str, Any]:
    for d in list_datasets():
        if _key(d["name"]) == _key(name):
            return d
    raise DatasetError("Unknown dataset %r" % name)


def upsert_dataset(name: str, sources: Optional[Dict[str, Any]] = None,
                    bindings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise DatasetError("A dataset needs a name")
    key = _key(name)
    data = _load()
    record = data.get(key) or {"name": name, "sources": {}, "bindings": {}, "checks": {}}
    record["name"] = name
    if sources is not None:
        if not isinstance(sources, dict):
            raise DatasetError("sources must be an object")
        record["sources"] = sources
    if bindings is not None:
        if not isinstance(bindings, dict):
            raise DatasetError("bindings must be an object")
        for mode in bindings.values():
            if isinstance(mode, dict) and mode.get("mode") not in (None, *MODES):
                raise DatasetError("Unknown binding mode %r (expected one of %s)" % (mode.get("mode"), MODES))
        record["bindings"] = bindings
    data[key] = record
    _save(data)
    return get_dataset(name)


def set_binding(name: str, runtime: str, binding: Dict[str, Any]) -> Dict[str, Any]:
    """`PUT /api/datasets/<name>/bindings/<runtime>` — *runtime* is an exact
    slot id ("ssh:mclab-gpu2") or a kind wildcard ("ssh:*")."""
    mode = (binding or {}).get("mode")
    if mode not in MODES:
        raise DatasetError("binding.mode must be one of %s, got %r" % (MODES, mode))
    key = _key(name)
    data = _load()
    record = data.get(key) or {"name": name, "sources": {}, "bindings": {}, "checks": {}}
    record.setdefault("bindings", {})[runtime] = dict(binding)
    data[key] = record
    _save(data)
    return get_dataset(name)


def record_check(name: str, runtime: str, ok: bool, detail: str) -> None:
    from datetime import datetime, timezone
    key = _key(name)
    data = _load()
    record = data.get(key) or {"name": name, "sources": {}, "bindings": {}, "checks": {}}
    record.setdefault("checks", {})[runtime] = {
        "ok": bool(ok), "detail": detail, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    data[key] = record
    _save(data)


# --------------------------------------------------------------------------- resolution (§5.2)
def _default_source(record: Dict[str, Any]) -> Optional[str]:
    sources = record.get("sources") or {}
    kaggle = sources.get("kaggle") or {}
    return kaggle.get("slug") or None


def resolve_binding(name: str, runtime_id: str, runtime_kind: str) -> Dict[str, Any]:
    """§5.2's resolution order: exact runtime id -> `kind:*` -> the kind
    default (local/ssh: `path`; colab: `fetch` from a Kaggle source when one
    is declared; kaggle: `attach`). Returns `{"mode": None}` when nothing
    resolves — the caller turns that into `no-dataset-binding`. A `fetch`/
    `attach` binding that doesn't spell its own `source` falls back to the
    dataset's default Kaggle source, so a user only has to set the mode."""
    data = _load()
    record = data.get(_key(name)) or {}
    bindings = record.get("bindings") or {}
    resolved: Optional[Dict[str, Any]] = None
    if runtime_id in bindings:
        resolved = dict(bindings[runtime_id])
    else:
        wildcard = "%s:*" % runtime_kind
        if wildcard in bindings:
            resolved = dict(bindings[wildcard])
    if resolved is None:
        if runtime_kind in ("local", "ssh"):
            resolved = {"mode": "path"}
        elif runtime_kind == "colab":
            resolved = {"mode": "fetch"}
        elif runtime_kind == "kaggle":
            resolved = {"mode": "attach"}
        else:
            return {"mode": None}
    if resolved.get("mode") in ("fetch", "attach") and not resolved.get("source"):
        default = _default_source(record)
        if default:
            resolved["source"] = default
        else:
            # No source anywhere: a fetch/attach binding with nothing to
            # fetch/attach is the same as no binding — surface it that way
            # rather than failing loudly at dispatch time.
            return {"mode": None}
    return resolved


def _resolve_binding_for_config(config_path: str, name: str, runtime_id: str, runtime_kind: str) -> Dict[str, Any]:
    """`resolve_binding()`, plus the one config-specific fallback it can't do
    on its own: a `fetch`/`attach` binding with no source of its own falls
    back to `dataset_map.resolve_kaggle_dataset(config_path)` — the
    pre-existing precedence (§3.4): a config's own `dataset.kaggle_dataset`
    wins, then `dataset_map.json`, then the profile yaml's legacy map — so
    this registry's own `sources.kaggle.slug` is one more override underneath
    those, not a second, competing source of truth. Used by both
    `data_mode_for_experiment()` (a pure read) and `place_dataset()` (which
    acts on the same answer), so the two can never disagree."""
    binding = resolve_binding(name, runtime_id, runtime_kind)
    if binding.get("mode") in ("fetch", "attach") and not binding.get("source"):
        from . import dataset_map
        slug = dataset_map.resolve_kaggle_dataset(config_path)
        binding = {**binding, "source": slug} if slug else {"mode": None}
    return binding


def data_mode_for_experiment(config_path: str, runtime_id: str, runtime_kind: str) -> Dict[str, Any]:
    """What `POST /api/experiments/preflight` and every runner's
    `can_accept()` show as the data column: the resolved binding for this
    config's dataset on this runtime, or `{"mode": None, "code":
    "no-dataset-binding", ...}` when it can't be answered (no dataset
    declared, or nothing resolves for this runtime)."""
    name, _root = dataset_identity_for_config(config_path)
    if not name:
        return {"mode": None, "code": "no-dataset-binding", "detail": "Config declares no dataset"}
    binding = _resolve_binding_for_config(config_path, name, runtime_id, runtime_kind)
    if not binding.get("mode"):
        return {
            "mode": None, "code": "no-dataset-binding", "dataset": name,
            "detail": "'%s' has no binding for %s and no default source to fall back to" % (name, runtime_id),
        }
    return {**binding, "dataset": name}


# --------------------------------------------------------------------------- placement (dispatch-time)
def _repo_relative(path: str) -> str:
    return path[1:] if path.startswith("/") else path


def place_dataset(
    config_path: str, runtime_id: str, runtime_kind: str, transport: Transport,
    remote_repo_root: Path, local_repo_root: Optional[Path] = None,
    data_account_creds: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Makes `<remote_repo_root>/<dataset.root>` resolve to real data on the
    given runtime, over *transport*, before that runtime's launch
    (XDASH_PLAN.md §5.1) — never `--dataset_dir`, because `dataset.root` is
    part of the framework's `config_hash`. Returns `{"mode": ...,
    "detail": ...}` on success; raises PlacementError (`no-dataset-binding`)
    when nothing resolves. `local` is always a no-op (`mode: "path"`,
    unchecked: a machine with a filesystem already has whatever's there).

    *local_repo_root* (push mode only) is where the local copy is pushed
    from — defaults to `settings.repo_root`, the dashboard's own checkout.
    *data_account_creds* (fetch mode only, §5.2): `{KAGGLE_USERNAME:...,
    KAGGLE_KEY:...}`-shaped env for that one step, never persisted on the
    runtime and never used for anything but the download command."""
    name, root = dataset_identity_for_config(config_path)
    if not name:
        raise PlacementError("no-dataset-binding", "Config declares no dataset")
    if not root:
        raise PlacementError("no-dataset-binding", "Dataset '%s' has no declared root" % name)
    binding = _resolve_binding_for_config(config_path, name, runtime_id, runtime_kind)
    mode = binding.get("mode")
    if not mode:
        raise PlacementError(
            "no-dataset-binding",
            "'%s' has no binding for %s and no default source to fall back to" % (name, runtime_id),
        )
    target = _repo_relative(root)  # repo-relative — where the config expects to find it

    if runtime_kind == "local":
        return {"mode": "path", "detail": "local filesystem, unchecked"}

    if mode == "path":
        source_path = binding.get("path") or str(remote_repo_root / target)
        if not transport.exists(source_path, "d"):
            raise PlacementError("no-dataset-binding", "'%s' not found on %s at %s" % (name, runtime_id, source_path))
        transport.run(["sh", "-c", "ln -sfn %s %s" % (_sh(source_path), _sh(str(remote_repo_root / target)))])
        return {"mode": "path", "detail": "symlinked to %s" % source_path}

    if mode == "push":
        cache = binding.get("path") or str(remote_repo_root / "data" / ".cache" / target)
        local_root = local_repo_root or settings.repo_root
        local_dir = Path(root) if Path(root).is_absolute() else (local_root / root)
        transport.push(local_dir, cache)
        transport.run(["sh", "-c", "ln -sfn %s %s" % (_sh(cache), _sh(str(remote_repo_root / target)))])
        return {"mode": "push", "detail": "pushed to %s" % cache}

    if mode == "fetch":
        source = binding.get("source")
        if not source:
            raise PlacementError("no-dataset-binding", "'%s' has no fetch source configured for %s" % (name, runtime_id))
        cache = str(remote_repo_root / "data" / ".cache" / target)
        env_prefix = ""
        if data_account_creds:
            env_prefix = " ".join("%s=%s" % (k, _sh(v)) for k, v in data_account_creds.items()) + " "
        fetch_cmd = (
            "%smkdir -p %s && python3 -c "
            "\"import kagglehub,shutil,sys; p=kagglehub.dataset_download(%r); "
            "shutil.copytree(p, %r, dirs_exist_ok=True)\""
        ) % (env_prefix, _sh(cache), source, cache)
        transport.run(["sh", "-c", fetch_cmd])
        transport.run(["sh", "-c", "ln -sfn %s %s" % (_sh(cache), _sh(str(remote_repo_root / target)))])
        return {"mode": "fetch", "source": source, "detail": "fetched to %s" % cache}

    if mode == "attach":
        # Kaggle only, and handled entirely by backend/kaggle.py's existing
        # dataset_sources + the template's own symlink cell — nothing to do
        # over a Transport (a Kaggle kernel has none).
        return {"mode": "attach", "source": binding.get("source"), "detail": "declarative attach (kernel-metadata.json)"}

    raise PlacementError("no-dataset-binding", "Unknown binding mode %r" % mode)


def _sh(value: str) -> str:
    import shlex
    return shlex.quote(str(value))


# --------------------------------------------------------------------------- checks (POST /api/datasets/<name>/check)
def check_binding(name: str, runtime_id: str, runtime_kind: str, transport: Transport, repo_root: Path) -> Dict[str, Any]:
    """Runs the binding's check now (`POST /api/datasets/<name>/check?runtime=`):
    `path`/`push` test the resolved directory exists over *transport*; `fetch`/
    `attach` just report the resolved source, since actually fetching/attaching
    is dispatch's job, not a dry check. Records the result (GET /api/datasets
    shows the last one) and returns it."""
    binding = resolve_binding(name, runtime_id, runtime_kind)
    mode = binding.get("mode")
    if not mode:
        detail = "No binding for %s and no default source" % runtime_id
        record_check(name, runtime_id, False, detail)
        return {"ok": False, "mode": None, "detail": detail}
    root = root_for_dataset(name)
    if mode in ("path", "push") and root:
        target = binding.get("path") or str(repo_root / _repo_relative(root))
        try:
            ok = transport.exists(target, "d")
            detail = ("found" if ok else "not found") + " at %s" % target
        except TransportError as e:
            ok, detail = False, str(e)
    else:
        ok = bool(binding.get("source")) or mode == "attach"
        detail = "source: %s" % binding.get("source") if binding.get("source") else "mode %s, no dry check" % mode
    record_check(name, runtime_id, ok, detail)
    return {"ok": ok, "mode": mode, "detail": detail}
