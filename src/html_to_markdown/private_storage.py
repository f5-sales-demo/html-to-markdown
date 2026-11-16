"""Verified private checkpoints with transparent, lossless supported-Mac compression."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess  # nosec B404
import tempfile
import threading
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

COMPRESSION_THRESHOLD = 1024 * 1024
RESERVE_BYTES = 64 * 1024 * 1024
STORAGE_EVENTS_ROOT = Path.home() / ".local/share/html-to-markdown/storage-events"
_CAPACITY_LOCK = threading.Lock()
_RESERVED: dict[int, int] = {}


class StorageUnavailable(OSError):
    """Completed evidence remains private and source/editor budgets are unchanged."""


class StorageLease:
    """Reserve free space across the existing single worker's concurrent jobs."""

    def __init__(self, path: Path, needed: int) -> None:
        existing = path
        while not existing.exists():
            existing = existing.parent
        self.device = existing.stat().st_dev
        self.needed = needed
        with _CAPACITY_LOCK:
            reserved = _RESERVED.get(self.device, 0)
            if shutil.disk_usage(existing).free - reserved < needed + RESERVE_BYTES:
                raise StorageUnavailable(
                    "private evidence storage cannot reserve this inference request"
                )
            _RESERVED[self.device] = reserved + needed

    def __enter__(self) -> StorageLease:
        return self

    def __exit__(self, *_args: Any) -> None:
        with _CAPACITY_LOCK:
            _RESERVED[self.device] -= self.needed


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _event(parent: Path, value: dict[str, Any]) -> None:
    del parent
    # Operational receipts must never enter a captured/source-input file tree.
    directory = STORAGE_EVENTS_ROOT
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = json.dumps(value, sort_keys=True).encode()
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
        path = Path(stream.name)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o600)
    os.replace(path, directory / (uuid.uuid4().hex + ".json"))
    _sync_directory(directory)


def require_capacity(path: Path, needed: int) -> None:
    """Check real free storage before an expensive new inference request."""
    existing = path
    while not existing.exists():
        existing = existing.parent
    if shutil.disk_usage(existing).free < needed + RESERVE_BYTES:
        raise StorageUnavailable(
            "private evidence storage is insufficient; queue remains resumable"
        )


def _compress(path: Path, expected: str) -> dict[str, Any] | None:
    before = path.stat()
    if before.st_size < COMPRESSION_THRESHOLD or platform.system() != "Darwin":
        return None
    executable = shutil.which("afsctool")
    if executable is None:
        return None
    process = subprocess.run([executable, "-c", str(path)], capture_output=True, check=False)  # nosec B603
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    metadata = (after.st_size, after.st_mode, after.st_uid, after.st_gid, after.st_mtime_ns)
    previous = (before.st_size, before.st_mode, before.st_uid, before.st_gid, before.st_mtime_ns)
    if process.returncode or metadata != previous or _digest(path) != expected:
        raise StorageUnavailable("private evidence compression failed identity verification")
    return {
        "operation": "transparent_compression",
        "logical_sha256": expected,
        "logical_bytes": after.st_size,
        "logical_bytes_verified": True,
        "permissions_owner_timestamps_verified": True,
        "allocated_before": before.st_blocks * 512,
        "allocated_after": after.st_blocks * 512,
    }


def atomic_write(path: Path, data: bytes) -> None:
    """Publish only complete verified bytes; preserve interrupted evidence privately."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    expected = hashlib.sha256(data).hexdigest()
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=path.name + ".", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        event = _compress(temporary, expected)
        if _digest(temporary) != expected:
            raise StorageUnavailable("private evidence checkpoint bytes changed")
        if event is not None:
            _event(path.parent, event)
        os.replace(temporary, path)
        _sync_directory(path.parent)
    except (OSError, ValueError) as failure:
        if temporary is not None and temporary.exists():
            try:
                incomplete = STORAGE_EVENTS_ROOT / "incomplete-evidence"
                incomplete.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.replace(temporary, incomplete / (uuid.uuid4().hex + ".partial"))
                # Preserve the complete caller bytes as well if a failed tool
                # altered the staged file. The partial is diagnostic evidence.
                if isinstance(failure, StorageUnavailable):
                    original = incomplete / (uuid.uuid4().hex + ".original")
                    with original.open("xb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    original.chmod(0o600)
            except OSError:
                # The original private temporary stays available when even a
                # directory checkpoint cannot be allocated. Never delete it.
                pass
        with suppress(OSError):
            _event(
                path.parent,
                {
                    "operation": "persistence_failure",
                    "logical_sha256": expected,
                    "logical_bytes": len(data),
                    "error_type": type(failure).__name__,
                    "errno": getattr(failure, "errno", None),
                    "source_or_review_approval": False,
                },
            )
        raise StorageUnavailable(
            "private evidence storage failed; pending bytes preserved"
        ) from failure
