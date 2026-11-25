"""Local proposal discovery shares bounded streaming identities within one preparation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .baseline_history_lookup import HistoryLookup
from .history_preservation import preserve_history


class ProposalHistoryLookup:
    """Cache source identities, then verify complete original custody before returning.

    Matching proposals retain their independent full-byte and ledger checks. This
    transaction does not select foreign histories or retain raw response payloads.
    """

    def __init__(self, output: Path) -> None:
        self._output = output
        self._lookup = HistoryLookup(output, output)
        self._used = False

    @property
    def metrics(self) -> dict[str, int]:
        return self._lookup.metrics

    def paths(self, binding: dict[str, Any]) -> list[Path]:
        """Every exact source field selects only the original local history set."""
        self._used = True
        return [
            path for path in self._lookup.paths(binding) if path.parent == self._output / "history"
        ]

    def preserve(self, data: bytes) -> Path:
        """Register only this writer's exact history generation in both active scopes."""
        path = preserve_history(self._output, data)
        if self._used:
            self._lookup.remember(path)
        return path

    def verify(self) -> None:
        """No inventory or standalone selection can return before final byte checks."""
        if self._used:
            self._lookup.verify()
