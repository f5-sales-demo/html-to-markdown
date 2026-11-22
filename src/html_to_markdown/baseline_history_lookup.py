"""Bounded batch discovery; final byte verification gates every lookup result."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any

from .history_streaming import SOURCE_KEYS, HistoryStream, binding_from_stream

__all__ = ["SOURCE_KEYS", "MAX_HISTORY_ENTRIES", "HistoryLookup", "history_roots", "safe"]

MAX_HISTORY_ENTRIES = 16384


def safe(path: Path) -> None:
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("unsafe retained privacy ledger path")


def history_roots(output: Path, worker_root: Path) -> list[Path]:
    safe(output)
    safe(worker_root)
    roots = {output}
    for root in worker_root.glob("*/*/curation-renewal/dependency-privacy"):
        safe(root)
        if root.is_dir():
            roots.add(root)
    return sorted(roots)


def _key(binding: dict[str, Any]) -> tuple[str | None, ...]:
    values = tuple(binding.get(name) for name in SOURCE_KEYS)
    if any(value is not None and not isinstance(value, str) for value in values):
        raise ValueError("invalid retained privacy source identity")
    return values


class HistoryLookup:
    """Keep only source identities; never cache article approvals or raw responses.

    The caller MUST verify after all lookup work and before returning its batch.
    Known files are skipped only inside this transaction. Their complete bytes
    are hashed again on final verification, independently of filesystem stats.
    Matching records still undergo the original source/proposal/budget checks.
    """

    def __init__(self, output: Path, worker_root: Path) -> None:
        self.output = output
        self.worker_root = worker_root
        self._entries: dict[Path, tuple[str | None, ...]] = {}
        self._digests: dict[str, tuple[str | None, ...]] = {}
        self._currents: dict[Path, str | None] = {}
        self._roots: list[Path] | None = None
        self._lock = threading.RLock()
        self._metrics = {"files_parsed": 0, "bytes_parsed": 0, "bytes_hashed": 0}

    @property
    def metrics(self) -> dict[str, int]:
        with self._lock:
            return dict(self._metrics)

    def _enumerate(self) -> list[Path]:
        paths = []
        roots = history_roots(self.output, self.worker_root)
        if self._roots is not None and roots != self._roots:
            raise ValueError("retained privacy history custody changed")
        self._roots = roots
        for root in roots:
            history = root / "history"
            safe(history)
            for path in sorted(history.glob("*.json")):
                safe(path)
                paths.append(path)
                if len(paths) > MAX_HISTORY_ENTRIES:
                    raise ValueError("retained privacy history lookup capacity exceeded")
        return paths

    def _load(self, path: Path) -> None:
        # Complete JSON syntax and original bytes are checked without retaining
        # unrelated request/response payloads, including four-worker first access.
        safe(path)
        if path.stem in self._digests:
            if self._file_digest(path) != path.stem:
                raise ValueError("retained privacy history digest changed")
            self._entries[path] = self._digests[path.stem]
            return
        with path.open("rb") as source:
            stream = HistoryStream(source)
            binding = binding_from_stream(stream)
        parsed = stream.bytes_read
        if stream.digest.hexdigest() != path.stem:
            raise ValueError("retained privacy history digest changed")
        if stream.surrogate_escapes:
            with path.open("rb") as source:
                stream = HistoryStream(source)
                binding = binding_from_stream(stream, standard_unicode=True)
            parsed += stream.bytes_read
            if stream.digest.hexdigest() != path.stem:
                raise ValueError("retained privacy history digest changed")
        self._entries[path] = _key(binding)
        self._digests[path.stem] = self._entries[path]
        self._metrics["files_parsed"] += 1
        self._metrics["bytes_parsed"] += parsed
        self._metrics["bytes_hashed"] += parsed

    def paths(self, wanted: dict[str, Any]) -> list[Path]:
        with self._lock:
            paths = self._enumerate()
            if not self._entries.keys() <= set(paths):
                raise ValueError("retained privacy history custody changed")
            for path in paths:
                if path not in self._entries:
                    self._load(path)
            key = _key(wanted)
            return [path for path in paths if self._entries[path] == key]

    def remember(self, path: Path) -> None:
        """Register this transaction's newly preserved original, never an approval."""
        with self._lock:
            if path.parent.parent not in history_roots(self.output, self.worker_root):
                raise ValueError("retained privacy history outside lookup custody")
            if path not in self._entries:
                if len(self._entries) >= MAX_HISTORY_ENTRIES:
                    raise ValueError("retained privacy history lookup capacity exceeded")
                self._load(path)

    def watch_current(self, path: Path, digest: str | None) -> None:
        """Pin every inspected current target, including absent or unrelated ones."""
        with self._lock:
            if path in self._currents and self._currents[path] != digest:
                raise ValueError("retained privacy current ledger changed")
            if len(self._currents) >= MAX_HISTORY_ENTRIES and path not in self._currents:
                raise ValueError("retained privacy history lookup capacity exceeded")
            self._currents[path] = digest

    def update_current(self, path: Path) -> None:
        """Allow only the exact destination record changed by this transaction."""
        with self._lock:
            safe(path)
            if path.parent != self.output / "documents" or path not in self._currents:
                raise ValueError("retained privacy current ledger outside lookup custody")
            self._currents[path] = self._file_digest(path)

    def _file_digest(self, path: Path) -> str:
        safe(path)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
                self._metrics["bytes_hashed"] += len(block)
        return digest.hexdigest()

    def _generations(self, paths: list[Path]) -> dict[Path, tuple[int, ...] | None]:
        result: dict[Path, tuple[int, ...] | None] = {}
        for path in paths:
            safe(path)
            if path.is_file():
                info = path.stat()
                result[path] = (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )
            else:
                result[path] = None
        return result

    def verify(self) -> None:
        """Reject concurrent additions, removals, symlinks and changed exact bytes."""
        with self._lock:
            paths = self._enumerate()
            if set(paths) != self._entries.keys():
                raise ValueError("retained privacy history custody changed")
            generations = self._generations(paths + list(self._currents))
            for path in paths:
                if self._file_digest(path) != path.stem:
                    raise ValueError("retained privacy history digest changed")
            for path, expected in self._currents.items():
                safe(path)
                actual = self._file_digest(path) if path.is_file() else None
                if actual != expected:
                    raise ValueError("retained privacy current ledger changed")
            if generations != self._generations(paths + list(self._currents)):
                raise ValueError("retained privacy history changed during verification")
            if set(self._enumerate()) != self._entries.keys():
                raise ValueError("retained privacy history custody changed")
