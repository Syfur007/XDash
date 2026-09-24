"""Slot identity — the single source of truth for how a slot/runner id maps
to (kind, name). Before this existed, `"local"` and `f"kaggle:{name}"` were
bare string literals duplicated across backend/experiments.py and this
package's own runner facades (Multi_runner_XDash.md Phase 2). Also owns the
runner list/lookup that used to live directly in
backend/runners/__init__.py, so a fourth/fifth kind is registered in one
place.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from .base import Runner

LOCAL = "local"  # the one slot id with no "kind:name" suffix — see slot_id()


def slot_id(kind: str, name: Optional[str] = None) -> str:
    """"local" has no name (there is exactly one); every other kind needs
    one ("ssh:mclab-gpu2", "colab:tanvir", "kaggle:tanvir")."""
    if kind == LOCAL:
        return LOCAL
    if not name:
        raise ValueError("slot_id() needs a name for kind %r" % kind)
    return "%s:%s" % (kind, name)


def parse_slot_id(slot: str) -> Tuple[str, Optional[str]]:
    """Inverse of slot_id(): "local" -> ("local", None); "kaggle:tanvir" ->
    ("kaggle", "tanvir")."""
    if slot == LOCAL:
        return LOCAL, None
    kind, sep, name = slot.partition(":")
    if not sep:
        return kind, None
    return kind, (name or None)


def list_runners() -> List[Runner]:
    """The local runner (always present) + one KaggleRunner per configured
    account. Phase 3 adds one runner per registered SSH/Colab host here —
    this is the only function that needs to change to register a new kind."""
    from .local import LocalRunner
    from .kaggle import list_kaggle_runners
    return [LocalRunner(), *list_kaggle_runners()]


def get_runner(slot: str) -> Runner:
    for r in list_runners():
        if r.id == slot:
            return r
    raise KeyError(f"Unknown runner '{slot}'")
