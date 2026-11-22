"""Final-byte-verified history lookup across one grouped governed replay."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from .baseline_history_lookup import HistoryLookup, safe


@dataclass
class _Scope:
    output: Path
    worker_root: Path
    lookup: HistoryLookup
    used: bool = False


CURRENT: ContextVar[_Scope | None] = ContextVar("baseline_ledger_scope", default=None)


def scoped_lookup(output: Path, worker_root: Path) -> HistoryLookup | None:
    scope = CURRENT.get()
    if scope is not None and scope.output == output and scope.worker_root == worker_root:
        scope.used = True
        return scope.lookup
    return None


@contextmanager
def ledger_scope(output: Path, worker_root: Path) -> Iterator[None]:
    """No grouped result can return before complete consulted custody is verified."""
    safe(output)
    safe(worker_root)
    previous = CURRENT.get()
    if previous is not None and previous.output == output and previous.worker_root == worker_root:
        yield
        return
    scope = _Scope(output, worker_root, HistoryLookup(output, worker_root))
    token = CURRENT.set(scope)
    try:
        yield
        if scope.used:
            scope.lookup.verify()
    finally:
        CURRENT.reset(token)


@contextmanager
def governed_scope(output: Path) -> Iterator[None]:
    """Use the same custody root as retained baseline prerequisite verification."""
    root = Path(os.environ.get("HTML_TO_MARKDOWN_BASELINE_HISTORY_ROOT", str(output)))
    with ledger_scope(output / "dependency-privacy", root):
        yield
