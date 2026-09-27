"""The dataset registry (DATASETS_PLAN.md, replaces XDASH_PLAN.md §3.7/§5.2/§8.5).

One store, one slug, delivery computed instead of configured (§0). A
dataset combines up to four layers (§3.1): **identity** (`name`, `root`,
read fresh from `configs/dataset/*.yaml` every time — never cached, never
typed here, because `dataset.root` is part of dissert's `config_hash`),
**sources** (a local folder and/or a Kaggle slug), **host overrides** (a
persistent SSH host that already has the data at a known path), and
**metadata** (tags, checks, fingerprints) — all XDash-owned.

`plan_delivery()` (§4) is the one function every caller uses to answer "how
does this runtime get this dataset's files" — preflight, every runner's
`can_accept()`, `GET /api/datasets`, and the UI all get the same answer, so
they can't disagree the way the old `dataset_map`/bindings/registry split
did (DS1-DS3). It's a pure read over cached checks, never a network call
(§9: "never calls out"). `stage()` is what actually executes a plan at
dispatch time (§4.4, §4.7), over a real Transport, recomputing the plan
fresh rather than trusting whatever preflight cached.

Storage: `data/<profile>/datasets.json`, keyed by dataset name (case-folded).
Migrated in memory from the old bindings-registry, `dataset_map.json`, and
the profile yaml's `kaggle_dataset_map` the first time it's read; persisted
(with a permanent `datasets.v1.json.bak`) on the first real write — same
"migrate in memory, persist on the first write" pattern as every other
JsonStore migration (XDASH_PLAN.md §3.8, §10).

Relay (the Kaggle-to-local cache, §4.3) and staging jobs (§4.7's "Stage
now") are D3 — not built here. Every place this module would have used the
relay instead blocks with `dataset-unavailable` and a `# DATASETS_PLAN.md
D3: relay` marker.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import yaml

from . import config as config_mod
from . import configs as cfg
from .config import settings
from .store import JsonStore, atomic_write_text
from .transport import Transport, TransportError

_store = JsonStore(lambda: settings.datasets_file, dict, sort_keys=True)

_META_KEY = "__meta__"  # a reserved key in the flat {dataset_key: record} dict — see backend/config.py's old comment

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}

_KAGGLE_URL_RE = re.compile(r"^https?://(?:www\.)?kaggle\.com/datasets/([^/\s?#]+)/([^/\s?#]+)", re.IGNORECASE)
_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_TAG_RE = re.compile(r"^[a-z0-9 ._-]{1,32}$")

# Codes plan_delivery()/stage() emit (§4.2). "no-dataset-binding" and
# "no-dataset-mapping" are kept as legacy aliases in experiments.py's block
# priority table — nothing here emits them any more.
CODE_DATASET_DRAFT = "dataset-draft"
CODE_DATASET_UNAVAILABLE = "dataset-unavailable"
CODE_NO_KAGGLE_SOURCE = "no-kaggle-source"
CODE_KAGGLE_NO_ACCESS = "kaggle-no-access"
CODE_NO_DATA_ACCOUNT = "no-data-account"
CODE_TARGET_OCCUPIED = "dataset-target-occupied"
CODE_LAYOUT_AMBIGUOUS = "dataset-layout-ambiguous"


class DatasetError(ValueError):
    """Expected failure (unknown dataset, bad slug, duplicate name) — routes
    map this to *status* (400 by default; 404/409 where the plan calls for
    it)."""

    def __init__(self, detail: str, status: int = 400):
        super().__init__(detail)
        self.status = status


class PlacementError(Exception):
    """Raised by `stage()` when a plan can't actually be executed — carries
    the same {code, detail} shape `plan_delivery()` returns, so a runner's
    `dispatch()` can turn it straight into a `RunnerBlocked`."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(detail)


# --------------------------------------------------------------------------- registry storage
def _key(name: str) -> str:
    return (name or "").strip().casefold()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _legacy_kaggle_slug_map() -> Dict[str, str]:
    """dataset_map.json (if present) else the profile yaml's own
    `kaggle_dataset_map` — read directly here, once, only to migrate old
    state (§10 item 2). backend/dataset_map.py itself is gone (§11); this is
    not a second, ongoing copy of it."""
    legacy_path = settings.state_dir / "dataset_map.json"
    if legacy_path.is_file():
        try:
            raw = json.loads(legacy_path.read_text())
        except (OSError, ValueError):
            raw = None
        if isinstance(raw, dict):
            return {str(k).strip().casefold(): str(v).strip() for k, v in raw.items() if str(k).strip() and str(v).strip()}
    try:
        profile_path = config_mod.REPOS_DIR / ("%s.yaml" % settings.profile_name)
        raw_yaml = yaml.safe_load(profile_path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        raw_yaml = {}
    legacy_map = raw_yaml.get("kaggle_dataset_map") or {}
    return {str(k).strip().casefold(): str(v).strip() for k, v in legacy_map.items() if str(k).strip() and str(v).strip()}


def _migrate_v1_to_v2(raw: Dict[str, Any]) -> Dict[str, Any]:
    """§10's migration, entirely in memory. *raw* is whatever's currently on
    disk at datasets.json (v1 bindings-registry shape, or {} if the file
    doesn't exist yet)."""
    out: Dict[str, Any] = {}
    meta = dict(raw.get(_META_KEY) or {})
    legacy_map = _legacy_kaggle_slug_map()
    frag_names_by_key = {_key(n): n for n in known_dataset_names()}
    # Only names that actually carried something forward (an old registry
    # entry, or a legacy Kaggle slug) get a v2 record. A fragment with
    # nothing to migrate gets none — §3.1's "a fragment with no XDash record
    # is listed too" is `list_datasets()`'s job, not a manufactured empty
    # record here.
    keys: Set[str] = ({k for k in raw if k != _META_KEY} | set(legacy_map))
    for key in sorted(keys):
        old = raw.get(key) or {}
        display_name = frag_names_by_key.get(key) or old.get("name") or key
        slug = legacy_map.get(key)
        notes: List[str] = []
        dropped: Dict[str, List[str]] = {}
        old_slug = ((old.get("sources") or {}).get("kaggle") or {}).get("slug")
        if old_slug and old_slug != slug:
            dropped.setdefault(old_slug, []).append("registry default")
        for runtime, binding in (old.get("bindings") or {}).items():
            src = (binding or {}).get("source")
            if src and src != slug:
                dropped.setdefault(src, []).append(runtime)
        for value, sources_list in dropped.items():
            notes.append("dropped conflicting slug %s (%s)" % (value, ", ".join(sources_list)))

        record: Dict[str, Any] = {"name": display_name, "tags": [], "sources": {}}
        if slug:
            if _SLUG_RE.match(slug):
                record["sources"]["kaggle"] = {"slug": slug}
            else:
                notes.append("dropped malformed slug %r" % slug)
        # §10 item 4: only an explicit ssh path binding survives, as a host
        # override. Everything else (fetch/attach/push, any colab:*/kaggle:*
        # binding) is dropped — delivery is computed now.
        for runtime, binding in (old.get("bindings") or {}).items():
            if runtime.startswith("ssh:") and runtime != "ssh:*" and (binding or {}).get("mode") == "path" and (binding or {}).get("path"):
                record.setdefault("hosts", {})[runtime] = {"path": binding["path"]}
        if notes:
            record["migration_notes"] = notes
        out[key] = record
    if meta.get("data_account"):
        out[_META_KEY] = {"data_account": meta["data_account"]}
    return out


def _load() -> Dict[str, Any]:
    if not _store.exists():
        return _migrate_v1_to_v2({})
    raw = _store.load()
    raw = raw if isinstance(raw, dict) else {}
    if (raw.get(_META_KEY) or {}).get("schema") == 2:
        return raw
    return _migrate_v1_to_v2(raw)


def _mark_dataset_map_migrated() -> None:
    legacy = settings.state_dir / "dataset_map.json"
    marker = settings.state_dir / "dataset_map.json.migrated"
    if legacy.is_file() and not marker.is_file():
        try:
            legacy.rename(marker)
        except OSError:
            pass


def _save(data: Dict[str, Any]) -> None:
    meta = dict(data.get(_META_KEY) or {})
    meta["schema"] = 2
    data[_META_KEY] = meta
    path = _store.path
    if path.is_file():
        try:
            existing = json.loads(path.read_text())
        except (OSError, ValueError):
            existing = None
        if isinstance(existing, dict) and (existing.get(_META_KEY) or {}).get("schema") != 2:
            backup = path.with_name("datasets.v1.json.bak")
            if not backup.is_file():
                atomic_write_text(backup, path.read_text())
            _mark_dataset_map_migrated()
    else:
        _mark_dataset_map_migrated()
    _store.save(data)


def data_account() -> Optional[str]:
    return (_load().get(_META_KEY) or {}).get("data_account") or None


def set_data_account(name: Optional[str]) -> None:
    data = _load()
    meta = dict(data.get(_META_KEY) or {})
    meta["data_account"] = (name or "").strip() or None
    data[_META_KEY] = meta
    _save(data)


def data_account_creds() -> Optional[Dict[str, str]]:
    """`{KAGGLE_USERNAME, KAGGLE_KEY}` for the designated data account
    (§3.6), read straight off its stored `kaggle.json` — None when no
    account is designated or it has no legacy username/key pair."""
    name = data_account()
    if not name:
        return None
    creds_path = settings.kaggle_creds_dir / name / "kaggle.json"
    if not creds_path.is_file():
        return None
    try:
        pair = json.loads(creds_path.read_text())
        return {"KAGGLE_USERNAME": str(pair["username"]), "KAGGLE_KEY": str(pair["key"])}
    except (OSError, ValueError, KeyError):
        return None


# --------------------------------------------------------------------------- identity (never typed, §5.1)
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
    names = []
    for path in _fragment_paths():
        raw = _read_yaml(path)
        name = _dotted_get(raw, settings.dataset_name_key)
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _fragment_identity(name: str) -> Optional[Dict[str, Any]]:
    """The fragment matching *name* (casefolded), or None — one scan giving
    both identity (name/root) and the read-only badges (§7) plus a
    fragment-declared Kaggle slug (§3.3, Decision 7), so callers don't
    re-glob configs/dataset/*.yaml for each separately."""
    key = _key(name)
    for path in _fragment_paths():
        raw = _read_yaml(path) or {}
        ds = raw.get("dataset")
        if not isinstance(ds, dict):
            continue
        frag_name = ds.get("name")
        if not (isinstance(frag_name, str) and _key(frag_name) == key):
            continue
        root = ds.get("root")
        slug = ds.get("kaggle_dataset")
        return {
            "name": frag_name.strip(),
            "root": root.strip() if isinstance(root, str) and root.strip() else None,
            "fragment": path.stem,
            "modality": ds.get("modality"),
            "channel_mode": ds.get("channel_mode"),
            "dedup": bool(ds.get("dedup")),
            "external": bool(ds.get("external")),
            "kaggle_dataset": slug.strip() if isinstance(slug, str) and slug.strip() else None,
        }
    return None


def dataset_identity_for_config(config_path: str) -> "tuple[Optional[str], Optional[str]]":
    """(dataset name, root) declared by *config_path* (configs_dir-relative),
    walking its `compose` fragments the same way the framework's own loader
    merges them (§5.1: read fresh, never typed)."""
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
    frag = _fragment_identity(name)
    return frag["root"] if frag else None


def _effective_kaggle_slug(name: str, record: Dict[str, Any], frag: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """§3.3: a fragment's own `dataset.kaggle_dataset` wins and locks the
    field (Decision 7); otherwise the record's `sources.kaggle.slug`."""
    frag = frag if frag is not None else _fragment_identity(name)
    if frag and frag.get("kaggle_dataset"):
        return frag["kaggle_dataset"]
    return ((record.get("sources") or {}).get("kaggle") or {}).get("slug") or None


def _local_source_path(record: Dict[str, Any], root: Optional[str]) -> Optional[str]:
    explicit = ((record.get("sources") or {}).get("local") or {}).get("path")
    if explicit:
        return explicit
    if root:
        return str(settings.repo_root / _repo_relative(root))
    return None


def _repo_relative(path: str) -> str:
    return path[1:] if path.startswith("/") else path


def _dir_size_human(path: str) -> str:
    try:
        total = sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())
    except OSError:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if total < 1024 or unit == "TB":
            return "%.0f %s" % (total, unit) if unit == "B" else "%.1f %s" % (total, unit)
        total /= 1024.0
    return ""


# --------------------------------------------------------------------------- Kaggle slug (§3.3)
def normalize_kaggle_slug(raw: Optional[str]) -> Optional[str]:
    text = (raw or "").strip()
    if not text:
        return None
    m = _KAGGLE_URL_RE.match(text)
    if m:
        return "%s/%s" % (m.group(1), m.group(2))
    return text


def validate_kaggle_slug(raw: Optional[str]) -> str:
    slug = normalize_kaggle_slug(raw)
    if not slug or not _SLUG_RE.match(slug):
        raise DatasetError("Kaggle slug must look like owner/dataset-slug (got %r)" % raw)
    return slug


def _normalize_tags(tags: Optional[List[str]]) -> List[str]:
    if tags is None:
        return []
    if not isinstance(tags, list):
        raise DatasetError("tags must be a list of strings")
    out: List[str] = []
    seen: Set[str] = set()
    for t in tags:
        s = str(t).strip().lower()
        if s and _TAG_RE.match(s) and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _within_home(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise DatasetError("Path must be absolute")
    try:
        resolved = p.resolve()
    except OSError:
        raise DatasetError("Could not resolve path %r" % path)
    home = Path.home().resolve()
    if home != resolved and home not in resolved.parents:
        raise DatasetError("Path must be under %s" % home)
    return resolved


def _validate_local_path(path: str) -> str:
    return str(_within_home(path))


# --------------------------------------------------------------------------- CRUD (§9)
def get_dataset(name: str) -> Dict[str, Any]:
    key = _key(name)
    data = _load()
    record = data.get(key)
    frag = _fragment_identity(name)
    if record is None and frag is None:
        raise DatasetError("Unknown dataset %r" % name, status=404)
    display_name = (frag or {}).get("name") or (record or {}).get("name") or name
    root = (frag or {}).get("root")
    return {
        "name": display_name,
        "identity_source": "draft" if root is None else "fragment",
        "root": root,
        "fragment": (frag or {}).get("fragment"),
        "tags": list((record or {}).get("tags") or []),
        "badges": (
            {
                "modality": frag.get("modality"), "channel_mode": frag.get("channel_mode"),
                "dedup": frag.get("dedup", False), "external": frag.get("external", False),
            } if frag else {}
        ),
        "sources": (record or {}).get("sources") or {},
        "hosts": (record or {}).get("hosts") or {},
        "checks": (record or {}).get("checks") or {},
        "fingerprints": (record or {}).get("fingerprints") or {},
        "migration_notes": (record or {}).get("migration_notes") or [],
        "kaggle_slug_locked": bool((frag or {}).get("kaggle_dataset")),
        "plans": _runtime_plans(display_name),
    }


def list_datasets() -> List[Dict[str, Any]]:
    data = _load()
    names = (set(data) | {_key(n) for n in known_dataset_names()}) - {_META_KEY}
    return [get_dataset(k) for k in sorted(names)]


def create_draft(name: str, tags: Optional[List[str]] = None, sources: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise DatasetError("A dataset needs a name")
    key = _key(name)
    data = _load()
    if key in data:
        raise DatasetError("A dataset named '%s' already exists" % name, status=409)
    if root_for_dataset(name) is not None:
        return {"exists": "fragment", "name": name}
    record: Dict[str, Any] = {"name": name, "tags": _normalize_tags(tags), "sources": {}}
    if sources:
        record["sources"] = _validated_sources(name, sources, {})
    data[key] = record
    _save(data)
    return get_dataset(name)


def _validated_sources(name: str, sources: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    current = dict(current)
    if "kaggle" in sources:
        kv = sources["kaggle"]
        if kv is None:
            current.pop("kaggle", None)
        else:
            slug = kv.get("slug") if isinstance(kv, dict) else kv
            if _fragment_identity(name) and (_fragment_identity(name) or {}).get("kaggle_dataset"):
                raise DatasetError("This dataset's Kaggle slug is declared in its fragment and can't be edited here")
            current["kaggle"] = {"slug": validate_kaggle_slug(slug)}
    if "local" in sources:
        lv = sources["local"]
        if lv is None:
            current.pop("local", None)
        else:
            path = lv.get("path") if isinstance(lv, dict) else lv
            current["local"] = {"path": _validate_local_path(path) if path else None}
    return current


def update_dataset(name: str, tags: Optional[List[str]] = None, sources: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    key = _key(name)
    data = _load()
    is_fragment = root_for_dataset(name) is not None
    record = data.get(key)
    if record is None:
        if not is_fragment:
            raise DatasetError("Unknown dataset %r" % name, status=404)
        record = {"name": name, "tags": [], "sources": {}}
    if tags is not None:
        record["tags"] = _normalize_tags(tags)
    if sources is not None:
        record["sources"] = _validated_sources(name, sources, record.get("sources") or {})
    record["name"] = record.get("name") or name
    data[key] = record
    _save(data)
    return get_dataset(name)


def delete_dataset(name: str) -> None:
    key = _key(name)
    data = _load()
    if key not in data:
        raise DatasetError("Unknown dataset %r" % name, status=404)
    del data[key]
    _save(data)


def link_fragment(name: str, fragment: str) -> Dict[str, Any]:
    key = _key(name)
    data = _load()
    record = data.get(key)
    if record is None:
        raise DatasetError("Unknown draft %r" % name, status=404)
    new_key = _key(fragment)
    if new_key != key and new_key in data:
        raise DatasetError("A record for '%s' already exists" % fragment, status=409)
    if root_for_dataset(fragment) is None:
        raise DatasetError("No fragment declares '%s'" % fragment)
    record["name"] = fragment
    if new_key != key:
        del data[key]
    data[new_key] = record
    _save(data)
    return get_dataset(fragment)


def set_host_override(name: str, slot: str, path: str) -> Dict[str, Any]:
    if not slot.startswith("ssh:"):
        raise DatasetError("Host overrides are ssh:<host> slots only, got %r" % slot)
    path = (path or "").strip()
    if not path:
        raise DatasetError("path is required")
    key = _key(name)
    data = _load()
    record = data.get(key) or {"name": name, "tags": [], "sources": {}}
    record.setdefault("hosts", {})[slot] = {"path": path}
    data[key] = record
    _save(data)
    return get_dataset(name)


def delete_host_override(name: str, slot: str) -> Dict[str, Any]:
    key = _key(name)
    data = _load()
    record = data.get(key)
    if record:
        (record.get("hosts") or {}).pop(slot, None)
        data[key] = record
        _save(data)
    return get_dataset(name)


# --------------------------------------------------------------------------- delivery planning (§4)
def _plan(strategy: Optional[str] = None, source: Optional[str] = None, target: Optional[str] = None,
          state: str = "unknown", code: Optional[str] = None, detail: str = "", action: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"strategy": strategy, "source": source, "target": target, "state": state, "code": code, "detail": detail, "action": action}


def _last_check(record: Dict[str, Any], target: str) -> Optional[Dict[str, Any]]:
    return (record.get("checks") or {}).get(target)


def plan_delivery(name: str, runtime_id: str, runtime_kind: str, config_path: Optional[str] = None) -> Dict[str, Any]:
    """§4.1/§4.2 — the one function every caller (preflight, every runner's
    `can_accept()`/`dispatch()`, `GET /api/datasets`, the UI) asks "how does
    *runtime_id* get *name*'s files". A pure read over the record's sources/
    hosts/cached checks — never calls out (§9). *config_path* is accepted
    for parity with the plan's literal signature and future use (e.g. D3's
    `dataset-fragment-uncommitted`); D1/D2 don't need it for the decision
    itself."""
    key = _key(name)
    data = _load()
    record = data.get(key) or {}
    frag = _fragment_identity(name)
    root = frag["root"] if frag else None
    if root is None:
        return _plan(state="blocked", code=CODE_DATASET_DRAFT,
                     detail="'%s' is a draft — no configs/dataset/*.yaml fragment declares it yet" % name)
    kaggle_slug = _effective_kaggle_slug(name, record, frag)
    local_path = _local_source_path(record, root)
    target_abs = str(settings.repo_root / _repo_relative(root))

    if runtime_kind == "local":
        if (settings.repo_root / _repo_relative(root)).is_dir():
            return _plan(strategy="local-existing", target=target_abs, state="ready", detail="on disk")
        if local_path and Path(local_path).is_dir():
            return _plan(strategy="local-link", source=local_path, target=target_abs, state="will-transfer",
                         detail="link to %s" % local_path)
        return _plan(state="blocked", code=CODE_DATASET_UNAVAILABLE,  # DATASETS_PLAN.md D3: relay
                     detail="No local copy of '%s' — downloading via XDash's cache comes later" % name)

    if runtime_kind == "kaggle":
        if not kaggle_slug:
            return _plan(state="blocked", code=CODE_NO_KAGGLE_SOURCE, detail="'%s' has no Kaggle slug configured" % name)
        check = _last_check(record, runtime_id)
        if check and check.get("state") == "blocked":
            return _plan(strategy="attach", source=kaggle_slug, state="blocked",
                         code=check.get("code") or CODE_KAGGLE_NO_ACCESS, detail=check.get("detail") or "",
                         action={"dataset": name, "row": runtime_id})
        state = "ready" if check and check.get("state") == "ready" else "unknown"
        return _plan(strategy="attach", source=kaggle_slug, state=state, detail="attach %s" % kaggle_slug)

    if runtime_kind == "colab":
        acct = data_account()
        if kaggle_slug and acct:
            check = _last_check(record, "kaggle:%s" % acct)
            if check and check.get("state") == "blocked":
                return _plan(strategy="colab-download", source=kaggle_slug, state="blocked",
                             code=check.get("code") or CODE_KAGGLE_NO_ACCESS, detail=check.get("detail") or "")
            return _plan(strategy="colab-download", source=kaggle_slug, state="will-transfer",
                         detail="downloads from Kaggle on the VM (data account: %s)" % acct)
        if local_path and Path(local_path).is_dir():
            return _plan(strategy="colab-push", source=local_path, state="will-transfer",
                         detail="pushed from this machine (%s)" % local_path)
        if not acct:
            return _plan(state="blocked", code=CODE_NO_DATA_ACCOUNT,
                         detail="No local copy of '%s' — set a data account in Datasets to let Colab download it from Kaggle" % name)
        return _plan(state="blocked", code=CODE_DATASET_UNAVAILABLE,  # DATASETS_PLAN.md D3: relay
                     detail="No local copy of '%s' and no Kaggle slug configured" % name)

    if runtime_kind == "ssh":
        override = (record.get("hosts") or {}).get(runtime_id)
        if override and override.get("path"):
            check = _last_check(record, runtime_id)
            state = (check or {}).get("state") or "unknown"
            code = (check or {}).get("code") if state == "blocked" else None
            detail = (check or {}).get("detail") or ("at %s" % override["path"])
            return _plan(strategy="host-override", source=override["path"], state=state, code=code, detail=detail)
        check = _last_check(record, runtime_id)
        if check and check.get("state") in ("ready", "partial"):
            return _plan(strategy="repo-path", state=check["state"], detail=check.get("detail") or "")
        if local_path and Path(local_path).is_dir():
            size = _dir_size_human(local_path)
            return _plan(strategy="ssh-push", source=local_path, state="will-transfer",
                         detail="copy from this machine at dispatch%s" % ((", " + size) if size else ""))
        return _plan(state="blocked", code=CODE_DATASET_UNAVAILABLE,  # DATASETS_PLAN.md D3: relay (when a kaggle slug exists)
                     detail="No local copy of '%s' to push, and no source configured on %s" % (name, runtime_id))

    return _plan(state="blocked", code=CODE_DATASET_UNAVAILABLE, detail="Unknown runtime kind %r" % runtime_kind)


def plan_delivery_for_config(config_path: str, runtime_id: str, runtime_kind: str) -> Dict[str, Any]:
    """What preflight and every runner's `can_accept()` actually have in
    hand: a config path, not a dataset name. Resolves the name
    (`dataset_identity_for_config()`) then delegates to `plan_delivery()` —
    replaces the old `data_mode_for_experiment()` (§11)."""
    name, _root = dataset_identity_for_config(config_path)
    if not name:
        plan = _plan(state="blocked", code="no-dataset-binding", detail="Config declares no dataset")
    else:
        plan = plan_delivery(name, runtime_id, runtime_kind, config_path=config_path)
    plan["dataset"] = name
    return plan


def _runtime_plans(name: str) -> Dict[str, Any]:
    try:
        from .runners import registry as runner_registry
    except ImportError:
        return {}
    out = {}
    for runner in runner_registry.list_runners():
        out[runner.id] = plan_delivery(name, runner.id, runner.kind)
    return out


# --------------------------------------------------------------------------- locate_root (§4.5, fixes DS8)
class DatasetLayoutError(DatasetError):
    def __init__(self, detail: str):
        super().__init__(detail, status=422)
        self.code = CODE_LAYOUT_AMBIGUOUS


# The standalone, dependency-free copy embedded verbatim into the Kaggle
# notebook's cell 11 and into the generated Python snippet a Colab on-VM
# download runs (§4.5: "This is the only implementation"; neither target
# can `import backend.datasets`). `locate_root()` below re-implements the
# exact same three-step rule against real Paths; a test runs both on the
# same fixture trees and asserts they agree.
LOCATE_ROOT_PY_SOURCE = '''\
def locate_root(download_dir, root, expect_top=None):
    import os, glob
    leaf = os.path.basename(str(root).rstrip("/"))
    candidates = [p for p in glob.glob(os.path.join(str(download_dir), "**", leaf), recursive=True) if os.path.isdir(p)]
    if len(candidates) == 1:
        return candidates[0]
    if expect_top:
        try:
            top = set(os.listdir(str(download_dir)))
        except OSError:
            top = set()
        if set(expect_top) <= top:
            return str(download_dir)
    raise AssertionError(
        "No unique '%s' directory found under %s (candidates: %s)" % (leaf, download_dir, candidates)
    )
'''


def locate_root(download_dir: Path, root: str, expect_top: Optional[Set[str]] = None) -> Path:
    download_dir = Path(download_dir)
    leaf = Path(str(root).rstrip("/")).name
    candidates = [p for p in download_dir.glob("**/%s" % leaf) if p.is_dir()]
    if len(candidates) == 1:
        return candidates[0]
    if expect_top:
        try:
            top = {p.name for p in download_dir.iterdir()}
        except OSError:
            top = set()
        if set(expect_top) <= top:
            return download_dir
    raise DatasetLayoutError(
        "No unique '%s' directory found under %s (candidates: %s)" % (leaf, download_dir, [str(c) for c in candidates])
    )


def _colab_download_script(slug: str, root: str, cache_dir: str) -> str:
    """The generated Python snippet a Colab VM runs (§4.2/§4.5): downloads
    *slug* via kagglehub (preinstalled on Colab, authenticated by the
    credentials file `_stage_colab()` writes just before this runs), locates
    the real root inside it, and copies it into the host cache."""
    driver = (
        "import kagglehub, shutil\n"
        "downloaded = kagglehub.dataset_download(%r)\n"
        "found = locate_root(downloaded, %r)\n"
        "shutil.copytree(str(found), %r, dirs_exist_ok=True)\n"
    ) % (slug, root, cache_dir)
    return LOCATE_ROOT_PY_SOURCE + "\n" + driver


# --------------------------------------------------------------------------- placement (§4.4, executed at dispatch)
def _sh(value: str) -> str:
    import shlex
    return shlex.quote(str(value))


def _same_path(transport: Transport, a: str, b: str) -> bool:
    cmd = "A=$(readlink -f %s 2>/dev/null); B=$(readlink -f %s 2>/dev/null); [ -n \"$A\" ] && [ \"$A\" = \"$B\" ]" % (_sh(a), _sh(b))
    proc = transport.run(["sh", "-c", cmd])
    return proc.returncode == 0


def _is_symlink(transport: Transport, path: str) -> bool:
    proc = transport.run(["sh", "-c", "test -L %s" % _sh(path)])
    return proc.returncode == 0


def _atomic_link(transport: Transport, source: str, target: Path) -> Dict[str, Any]:
    """§4.4 rules 1-5: source==target is a no-op (fixes DS5's self-link), a
    real occupied directory blocks instead of being written into, the
    replace is atomic (`ln` into a temp name, then `mv -T`), the parent is
    created first, and the exit code is checked (fixes DS6)."""
    target_s = str(target)
    if _same_path(transport, source, target_s):
        return {"linked": False, "detail": "already at %s" % target_s}
    if transport.exists(target_s, "d") and not _is_symlink(transport, target_s):
        raise PlacementError(
            CODE_TARGET_OCCUPIED,
            "'%s' already has files that aren't XDash's — move them, or point a host override at them" % target_s,
        )
    tmp = target_s + ".xdash-tmp"
    cmd = " && ".join([
        "mkdir -p %s" % _sh(str(target.parent)),
        "ln -sfn %s %s" % (_sh(source), _sh(tmp)),
        "mv -T %s %s" % (_sh(tmp), _sh(target_s)),
    ])
    proc = transport.run(["sh", "-c", cmd])
    if proc.returncode != 0:
        raise PlacementError(CODE_DATASET_UNAVAILABLE, (proc.stderr or proc.stdout or "").strip()[-300:] or "link failed")
    return {"linked": True, "detail": "linked to %s" % source}


def stage(name: str, runtime_id: str, runtime_kind: str, transport: Transport, remote_repo_root: Path,
          data_account_creds: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Executed at dispatch (§4.7), for a non-local runtime only — makes
    `<remote_repo_root>/<root>` resolve to real data over *transport*, using
    the host cache layout `<repo_root>/data/.xdash-cache/<key>/` (§4.4). The
    local copy, when one is used as a source, is always
    `settings.repo_root`'s own checkout (§3.4's default) — this dashboard's
    own working tree, never a caller-supplied override. Recomputes the plan
    fresh rather than trusting whatever `can_accept()` cached (§4.1) — a
    binding that stopped resolving in between is caught here, not silently
    launched against missing data."""
    root = root_for_dataset(name)
    if not root:
        raise PlacementError(CODE_DATASET_DRAFT, "'%s' has no declared root" % name)
    target = remote_repo_root / _repo_relative(root)

    if runtime_kind == "ssh":
        # DS5 fix, checked live before trusting any cached plan/check: when
        # there's no host override (§4.2's strategy #1 always wins first) and
        # the host already has a REAL directory sitting exactly at the
        # target, it is never touched and never linked to itself — regardless
        # of whether anyone ever ran a Check on this host. This is what lets
        # an already-staged SSH box "just work" with zero configuration
        # (§4.2's SSH strategy #2), the same way it did before checks existed.
        has_override = bool(((_load().get(_key(name)) or {}).get("hosts") or {}).get(runtime_id, {}).get("path"))
        target_s = str(target)
        if not has_override and transport.exists(target_s, "d") and not _is_symlink(transport, target_s):
            return {"strategy": "repo-path", "detail": "already at %s (no transfer needed)" % target_s}

    plan = plan_delivery(name, runtime_id, runtime_kind)
    if plan["state"] == "blocked":
        raise PlacementError(plan.get("code") or CODE_DATASET_UNAVAILABLE, plan.get("detail") or "")

    if runtime_kind == "kaggle":
        return {"strategy": "attach", "source": plan.get("source"), "detail": plan["detail"]}
    if runtime_kind == "ssh":
        return _stage_ssh(name, plan, transport, remote_repo_root, target)
    if runtime_kind == "colab":
        return _stage_colab(name, plan, root, transport, remote_repo_root, target, data_account_creds)
    raise PlacementError(CODE_DATASET_UNAVAILABLE, "Unknown runtime kind %r" % runtime_kind)


def _host_cache_dir(remote_repo_root: Path, name: str) -> str:
    return str(remote_repo_root / "data" / ".xdash-cache" / _key(name))


def _stage_ssh(name: str, plan: Dict[str, Any], transport: Transport, remote_repo_root: Path, target: Path) -> Dict[str, Any]:
    strategy = plan["strategy"]
    if strategy == "host-override":
        source = plan["source"]
        if not transport.exists(source, "d"):
            raise PlacementError(CODE_DATASET_UNAVAILABLE, "Host override '%s' not found for '%s'" % (source, name))
        link = _atomic_link(transport, source, target)
        return {"strategy": "host-override", "source": source, "detail": link["detail"]}
    if strategy == "ssh-push":
        local_dir = plan["source"]
        cache = _host_cache_dir(remote_repo_root, name)
        transport.run(["sh", "-c", "mkdir -p %s" % _sh(cache)])
        transport.push(Path(local_dir), cache, delete=True)
        link = _atomic_link(transport, cache, target)
        return {"strategy": "ssh-push", "source": local_dir, "cache": cache, "detail": "pushed then " + link["detail"]}
    raise PlacementError(plan.get("code") or CODE_DATASET_UNAVAILABLE, plan.get("detail") or "Nothing to stage for '%s'" % name)


def _stage_colab(name: str, plan: Dict[str, Any], root: str, transport: Transport, remote_repo_root: Path,
                  target: Path, data_account_creds: Optional[Dict[str, str]]) -> Dict[str, Any]:
    strategy = plan["strategy"]
    cache = _host_cache_dir(remote_repo_root, name)
    if strategy == "colab-push":
        local_dir = plan["source"]
        transport.run(["sh", "-c", "mkdir -p %s" % _sh(cache)])
        transport.push(Path(local_dir), cache, delete=True)
        link = _atomic_link(transport, cache, target)
        return {"strategy": "colab-push", "source": local_dir, "detail": "pushed then " + link["detail"]}
    if strategy == "colab-download":
        slug = plan["source"]
        if not data_account_creds:
            raise PlacementError(CODE_NO_DATA_ACCOUNT, "No data-account credentials available to download '%s' on Colab" % name)
        # §4.6: written over stdin (Transport.put_text), never an argument —
        # and deleted in a trap the moment the download finishes or fails.
        creds_json = json.dumps({"username": data_account_creds.get("KAGGLE_USERNAME"), "key": data_account_creds.get("KAGGLE_KEY")})
        transport.put_text("~/.config/kaggle/kaggle.json", creds_json, mode=0o600)
        transport.put_text("~/.kaggle/kaggle.json", creds_json, mode=0o600)
        script = _colab_download_script(slug, root, cache)
        cmd = "trap 'rm -f ~/.config/kaggle/kaggle.json ~/.kaggle/kaggle.json' EXIT; python3 -c %s" % _sh(script)
        proc = transport.run(["sh", "-c", cmd], timeout=1800)
        if proc.returncode != 0:
            raise PlacementError(CODE_DATASET_UNAVAILABLE, (proc.stderr or proc.stdout or "").strip()[-300:] or "download failed")
        link = _atomic_link(transport, cache, target)
        return {"strategy": "colab-download", "source": slug, "detail": "downloaded then " + link["detail"]}
    raise PlacementError(plan.get("code") or CODE_DATASET_UNAVAILABLE, plan.get("detail") or "Nothing to stage for '%s' on Colab" % name)


# --------------------------------------------------------------------------- checks (§5, synchronous in D1/D2)
def _local_fingerprint(root_path: Path) -> Optional[Dict[str, Any]]:
    if not root_path.is_dir():
        return None
    files, total_bytes, entries = 0, 0, []
    for p in root_path.rglob("*"):
        if p.is_file():
            files += 1
            size = p.stat().st_size
            total_bytes += size
            entries.append("%s\t%d" % (p.relative_to(root_path).as_posix(), size))
    manifest = "sha256:" + hashlib.sha256("\n".join(sorted(entries)).encode()).hexdigest()
    return {"files": files, "bytes": total_bytes, "manifest": manifest}


def _remote_fingerprint(transport: Transport, dir_path: str) -> Optional[Dict[str, Any]]:
    """§5.2's remote fingerprint: `find`'s sorted `relpath\\tsize` lines,
    fetched once and hashed here (client-side) rather than piping through a
    remote `sha256sum` — same manifest string, one round trip. None on a
    missing dir; raises TransportError only when the host can't be asked at
    all (unreachable) — a `find` failure (non-GNU host) returns None too, so
    the caller reports `unknown` rather than a false green."""
    if not transport.exists(dir_path, "d"):
        return None
    proc = transport.run(["sh", "-c", "cd %s && find . -type f -printf '%%P\\t%%s\\n' | LC_ALL=C sort" % _sh(dir_path)], timeout=120)
    if proc.returncode != 0:
        return None
    lines = [l for l in (proc.stdout or "").splitlines() if l]
    total_bytes = 0
    for l in lines:
        if "\t" in l:
            total_bytes += int(l.rsplit("\t", 1)[1])
    manifest = "sha256:" + hashlib.sha256("\n".join(lines).encode()).hexdigest()
    return {"files": len(lines), "bytes": total_bytes, "manifest": manifest}


def _check_kaggle_access(slug: str, account_name: str) -> Dict[str, Any]:
    """`kaggle datasets files <slug> --page-size 1` as *account_name* (§5.1).
    1.7.4.5's exit code for a 403/404 hasn't been verified (D1-live), so this
    parses the text for the status regardless of exit code."""
    from . import kaggle as kaggle_backend
    try:
        proc = kaggle_backend._run_kaggle(["datasets", "files", slug, "--page-size", "1"], account_name, timeout=30)
    except kaggle_backend.KaggleOpsError as e:
        return {"state": "unknown", "code": None, "detail": str(e)}
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if "403" in text or "Forbidden" in text:
        return {"state": "blocked", "code": CODE_KAGGLE_NO_ACCESS, "detail": text[-300:] or "403 Forbidden"}
    if "404" in text or "Not Found" in text:
        return {"state": "blocked", "code": "kaggle-not-found", "detail": text[-300:] or "404 Not Found"}
    if proc.returncode == 0:
        return {"state": "ready", "code": None, "detail": text[-300:] or "ok"}
    return {"state": "unknown", "code": None, "detail": text[-300:] or ("exit %s" % proc.returncode)}


def _check_ssh(name: str, slot: str, record: Dict[str, Any], root: Optional[str]) -> Dict[str, Any]:
    try:
        from .runners import registry as runner_registry
        runner = runner_registry.get_runner(slot)
    except (ImportError, KeyError):
        return {"state": "unknown", "code": None, "detail": "Unknown host '%s'" % slot}
    transport = getattr(runner, "_transport", None)
    if transport is None:
        return {"state": "unknown", "code": None, "detail": "No transport for '%s'" % slot}
    override = (record.get("hosts") or {}).get(slot)
    if override and override.get("path"):
        target_path = override["path"]
    elif root:
        repo_root = getattr(getattr(runner, "host", None), "repo_root", None) or settings.repo_root
        target_path = str(repo_root / _repo_relative(root))
    else:
        return {"state": "blocked", "code": CODE_DATASET_DRAFT, "detail": "No declared root"}
    try:
        fp = _remote_fingerprint(transport, target_path)
    except TransportError as e:
        return {"state": "unknown", "code": None, "detail": str(e)}
    if fp is None:
        return {"state": "blocked", "code": CODE_DATASET_UNAVAILABLE, "detail": "not found at %s" % target_path}
    reference = (record.get("fingerprints") or {}).get("local")
    if reference and reference.get("manifest") and reference["manifest"] != fp["manifest"]:
        return {"state": "partial", "code": None,
                "detail": "%d/%d files (differs from the local copy)" % (fp["files"], reference["files"])}
    record.setdefault("fingerprints", {})[slot] = {**fp, "at": _now()}
    return {"state": "ready", "code": None, "detail": "%d files at %s" % (fp["files"], target_path)}


def run_checks(name: str, targets: Optional[List[str]] = None) -> Dict[str, Any]:
    """`POST /api/datasets/<n>/check` (deviation from §9: synchronous, not a
    background job — D1/D2's own scope note; a 30s timeout per Kaggle
    account). Runs the Kaggle access check for every registered account
    plus the data account, the local fingerprint, and the remote fingerprint
    for every ssh host that has an override or was named in *targets*."""
    key = _key(name)
    data = _load()
    record = data.get(key) or {"name": name, "tags": [], "sources": {}}
    root = root_for_dataset(name)
    kaggle_slug = _effective_kaggle_slug(name, record)
    want = set(targets) if targets else None
    results: Dict[str, Any] = {}

    if kaggle_slug:
        acct_names: Set[str] = set()
        try:
            from . import kaggle as kaggle_backend
            acct_names = {a["name"] for a in kaggle_backend.list_accounts()}
        except ImportError:
            pass
        if data_account():
            acct_names.add(data_account())
        for acct in sorted(acct_names):
            slot = "kaggle:%s" % acct
            if want is None or slot in want:
                results[slot] = _check_kaggle_access(kaggle_slug, acct)

    if root and (want is None or "local" in want):
        local_path = _local_source_path(record, root)
        fp = _local_fingerprint(Path(local_path)) if local_path else None
        if fp:
            record.setdefault("fingerprints", {})["local"] = {**fp, "at": _now()}
            results["local"] = {"state": "ready", "code": None, "detail": "%d files" % fp["files"]}
        else:
            results["local"] = {"state": "blocked", "code": CODE_DATASET_UNAVAILABLE,
                                 "detail": "not found at %s" % (local_path or "(no local source configured)")}

    ssh_slots = set((record.get("hosts") or {}).keys())
    if want is None:
        try:
            from .runners import registry as runner_registry
            ssh_slots |= {r.id for r in runner_registry.list_runners() if r.kind == "ssh"}
        except ImportError:
            pass
    else:
        ssh_slots = {t for t in want if t.startswith("ssh:")}
    for slot in sorted(ssh_slots):
        results[slot] = _check_ssh(name, slot, record, root)

    for target, result in results.items():
        record.setdefault("checks", {})[target] = {**result, "at": _now()}
    data[key] = record
    _save(data)
    return {"checks": {t: record["checks"][t] for t in results}}


# --------------------------------------------------------------------------- fs picker (§8.5)
def fs_list(path: Optional[str], mode: str = "dir") -> Dict[str, Any]:
    home = Path.home().resolve()
    base = Path(path).expanduser().resolve() if path else home
    if home != base and home not in base.parents:
        raise DatasetError("Path escapes $HOME")
    if not base.is_dir():
        raise DatasetError("Not a directory: %s" % base, status=404)
    entries = []
    try:
        children = sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as e:
        raise DatasetError(str(e))
    for child in children:
        if child.name.startswith("."):
            continue
        try:
            is_dir = child.is_dir()
        except OSError:
            continue
        if is_dir:
            entries.append({"name": child.name, "type": "dir", "path": str(child)})
        elif mode == "file" and child.suffix.lower() in _IMAGE_EXTS:
            entries.append({"name": child.name, "type": "file", "path": str(child)})
    parent = str(base.parent) if base != home and base != base.parent else None
    return {"path": str(base), "parent": parent, "entries": entries}


# --------------------------------------------------------------------------- samples + channel preview (§8.4)
def _dataset_root_path(name: str) -> Path:
    record = _load().get(_key(name)) or {}
    root = root_for_dataset(name)
    local = _local_source_path(record, root)
    if not local:
        raise DatasetError("No local copy configured for '%s'" % name, status=404)
    p = Path(local).resolve()
    if not p.is_dir():
        raise DatasetError("Local copy not found at %s" % p, status=404)
    return p


def _within_root(base: Path, rel: Optional[str]) -> Path:
    target = (base / (rel or "")).resolve()
    if base != target and base not in target.parents:
        raise DatasetError("Path escapes the dataset root")
    return target


def dataset_tree(name: str, rel_path: Optional[str], page: int = 0, page_size: int = 48) -> Dict[str, Any]:
    base = _dataset_root_path(name)
    target = _within_root(base, rel_path)
    if not target.is_dir():
        raise DatasetError("Not a directory: %s" % rel_path, status=404)
    dirs, images = [], []
    for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
        if child.name.startswith("."):
            continue
        if child.is_dir():
            dirs.append(child.name)
        elif child.suffix.lower() in _IMAGE_EXTS:
            images.append(child.name)
    start = page * page_size
    return {"path": rel_path or "", "dirs": dirs, "images": images[start:start + page_size],
            "total_images": len(images), "page": page, "page_size": page_size}


def dataset_file_path(name: str, rel_path: str) -> Path:
    base = _dataset_root_path(name)
    target = _within_root(base, rel_path)
    if not target.is_file():
        raise DatasetError("Not found: %s" % rel_path, status=404)
    return target


def dataset_mask_pair(name: str, rel_path: str) -> Optional[str]:
    """§8.4: when *rel_path* is under an `images/` directory and the sibling
    `masks/` has a same-stem image, its relpath."""
    base = _dataset_root_path(name)
    target = _within_root(base, rel_path)
    parts = target.relative_to(base).parts
    if "images" not in parts:
        return None
    idx = len(parts) - 1 - parts[::-1].index("images")
    mask_dir = base.joinpath(*parts[:idx], "masks")
    if not mask_dir.is_dir():
        return None
    stem = target.stem
    for candidate in sorted(mask_dir.glob(stem + ".*")):
        if candidate.suffix.lower() in _IMAGE_EXTS:
            return str(candidate.relative_to(base))
    return None


def dataset_thumb_path(name: str, rel_path: str, size: int = 160) -> Path:
    """A Pillow-resized JPEG, cached under `data/<profile>/thumbs/<key>/`
    (deviation from §8.4's "under the relay cache directory" — the relay
    cache is D3; this machine-local cache is unaffected by that)."""
    src = dataset_file_path(name, rel_path)
    cache_dir = settings.state_dir / "thumbs" / _key(name)
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(("%s:%d" % (rel_path, size)).encode()).hexdigest()
    out = cache_dir / (digest + ".jpg")
    if not out.is_file() or out.stat().st_mtime < src.stat().st_mtime:
        from PIL import Image
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((size, size))
            im.save(out, "JPEG", quality=85)
    return out


def channel_preview(name: str, image_path: str, mode: str) -> Dict[str, Any]:
    from . import bridge
    p = Path(image_path)
    if p.is_absolute():
        resolved = _within_home(str(p))
    else:
        resolved = dataset_file_path(name, image_path)
    frag = _fragment_identity(name) or {}
    modality = frag.get("modality") or "colour"
    return bridge.run_bridge_script(
        "channel_preview.py", [json.dumps({"image_path": str(resolved), "mode": mode, "modality": modality})],
        timeout=30, use_cache=False,
    )
