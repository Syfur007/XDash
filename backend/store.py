"""One crash-safe JSON store for every XDash-owned state file (XDASH_PLAN.md
§3.8, closes X11).

Before this existed, fourteen call sites each did their own
`path.write_text(json.dumps(...))` and their own `try: json.loads(...) except:
return {}`. Together those two halves were a data-loss machine: a crash
mid-write left a truncated file, the next load swallowed the parse error and
returned an empty store, and the next save wrote that empty store over the
truncated one. One crash, every experiment gone.

`JsonStore` fixes both halves:

- **Atomic write.** The new content goes to a unique temp file in the same
  directory, is fsync'd, then `os.replace()`d over the old file, and the
  directory entry is fsync'd too. A crash at any point leaves either the
  complete old file or the complete new one, never a mix.
- **A `.bak` of the last good copy.** Before every replace, the current file
  (if it parses) is copied to `<name>.bak` the same atomic way.
- **Fail loud.** A file that exists but doesn't parse raises
  `StoreCorruptError` naming the `.bak` to restore from. It never returns an
  empty default, so nothing can save over it by accident. A missing file
  whose `.bak` still exists is treated the same way: that is not a fresh
  install, something deleted the store.
- **Migrations** run in memory on load, keyed by a top-level
  `schema_version`. The first save after a version bump keeps a permanent
  `<name>.pre-v<N>.bak` of the old on-disk copy, so a migration can never
  destroy the only copy of the user's data.

Stdlib only, Python 3.8-compatible like the rest of backend/.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Optional, Union

PathLike = Union[str, Path]
PathSource = Union[PathLike, Callable[[], PathLike]]


class StoreCorruptError(Exception):
    """A state file exists but is not valid JSON (or went missing while its
    backup did not). Deliberately not a subclass of ValueError: no caller's
    existing `except ValueError` may swallow it by accident."""

    def __init__(self, path: Path, detail: str):
        self.path = Path(path)
        backup = backup_path(self.path)
        hint = (
            f" A backup of the last good copy is at {backup} — inspect it, then restore with "
            f"`cp '{backup}' '{self.path}'`."
            if backup.is_file() else " No backup exists."
        )
        super().__init__(f"State file {self.path} is unreadable ({detail}).{hint}")


def backup_path(path: PathLike) -> Path:
    path = Path(path)
    return path.with_name(path.name + ".bak")


def _fsync_dir(directory: Path) -> None:
    # Makes the rename itself durable. Best-effort: some filesystems refuse
    # O_RDONLY on a directory, and losing only the rename on power loss still
    # leaves the old complete file behind.
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: PathLike, data: bytes, mode: Optional[int] = None) -> None:
    """Write *data* to *path* so that a crash at any instant leaves either
    the complete old file or the complete new one. *mode* (e.g. 0o600 for a
    file holding secrets) is applied to the temp file before the rename, so
    the final file never exists with looser permissions, even briefly."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per process *and* thread: two threads saving the same store
    # must never share (and truncate) one temp file.
    tmp = path.with_name(".%s.%d.%d.tmp" % (path.name, os.getpid(), threading.get_ident()))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666 if mode is None else mode)
    try:
        try:
            if mode is not None:
                os.fchmod(fd, mode)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def atomic_write_text(path: PathLike, text: str, mode: Optional[int] = None) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def _schema_version_of(data: Any) -> int:
    if isinstance(data, dict):
        try:
            return int(data.get("schema_version") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


class JsonStore:
    """One JSON state file.

    *path* may be a callable so a store follows `settings.reload()` (a
    profile switch moves every per-profile file) without being rebuilt.
    *default* builds the value for a file that doesn't exist yet. It is a
    factory, never a shared object, so no caller can mutate the default.
    """

    def __init__(
        self,
        path: PathSource,
        default: Callable[[], Any],
        *,
        schema_version: Optional[int] = None,
        migrate: Optional[Callable[[Any, int], Any]] = None,
        mode: Optional[int] = None,
        indent: Optional[int] = 2,
        sort_keys: bool = False,
    ):
        self._path = path
        self._default = default
        self.schema_version = schema_version
        self._migrate = migrate
        self._mode = mode
        self._indent = indent
        self._sort_keys = sort_keys
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return Path(self._path() if callable(self._path) else self._path)

    # ------------------------------------------------------------------ read
    def _parse(self, path: Path, raw: bytes) -> Any:
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise StoreCorruptError(path, "%s: %s" % (type(e).__name__, e))

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> Any:
        """The stored value, migrated to the current schema. Raises
        StoreCorruptError instead of ever returning a default for a file
        that exists but can't be read."""
        path = self.path
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            if backup_path(path).is_file():
                raise StoreCorruptError(path, "the file is missing but its backup exists")
            return self._default()
        except OSError as e:
            raise StoreCorruptError(path, "unreadable: %s" % e)
        data = self._parse(path, raw)
        if self.schema_version is not None:
            found = _schema_version_of(data)
            if found < self.schema_version:
                if self._migrate is not None:
                    data = self._migrate(data, found)
                if isinstance(data, dict):
                    data["schema_version"] = self.schema_version
        return data

    # ----------------------------------------------------------------- write
    def save(self, data: Any) -> None:
        with self._lock:
            path = self.path
            text = json.dumps(data, indent=self._indent, sort_keys=self._sort_keys, default=str)
            try:
                current = path.read_bytes()
            except FileNotFoundError:
                current = None
            if current is not None:
                try:
                    old = json.loads(current.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    old = None  # corrupt: keep whatever last good .bak already exists
                if old is not None:
                    atomic_write_bytes(backup_path(path), current, mode=self._mode)
                    if self.schema_version is not None and _schema_version_of(old) < self.schema_version:
                        # Permanent, never overwritten: the pre-migration copy.
                        keep = path.with_name("%s.pre-v%d.bak" % (path.name, self.schema_version))
                        if not keep.exists():
                            atomic_write_bytes(keep, current, mode=self._mode)
            atomic_write_text(path, text, mode=self._mode)
