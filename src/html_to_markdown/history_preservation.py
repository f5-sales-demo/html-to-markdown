"""Register only exact history writes performed by an owning review transaction."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Protocol

from .capture_store import preserve_bytes


class HistoryObserver(Protocol):
    output: Path

    def remember(self, path: Path) -> None: ...


_ACTIVE: ContextVar[HistoryObserver | None] = ContextVar("retained_history_observer", default=None)


@contextmanager
def history_transaction(observer: HistoryObserver) -> Iterator[None]:
    """Scope history ownership to this worker; nested destinations remain independent."""
    token = _ACTIVE.set(observer)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def preserve_history(output: Path, data: bytes) -> Path:
    """Preserve the exact generation and register it before the writer proceeds."""
    path = output / "history" / (hashlib.sha256(data).hexdigest() + ".json")
    preserve_bytes(path, data)
    observer = _ACTIVE.get()
    if observer is not None and observer.output == output:
        observer.remember(path)
    return path
