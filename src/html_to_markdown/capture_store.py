"""Private immutable capture payloads; mutable state never enters this store."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .private_storage import atomic_write
from .urls import SOURCE_ROOTS

_DIRECTORY_SYNCS: ContextVar[set[Path] | None] = ContextVar("capture_directory_syncs", default=None)


def _flush_directory(path: Path) -> None:
    _safe(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def durable_links() -> Iterator[None]:
    """Flush every affected namespace before a complete capture import can return."""
    pending: set[Path] = set()
    token = _DIRECTORY_SYNCS.set(pending)
    try:
        yield
    finally:
        _DIRECTORY_SYNCS.reset(token)
        failures: list[OSError | ValueError] = []
        for directory in sorted(pending):
            try:
                _flush_directory(directory)
            except (OSError, ValueError) as failure:
                failures.append(failure)
        if failures:
            first_failure = failures[0]
            if len(failures) > 1:
                first_failure.add_note(
                    f"{len(failures) - 1} additional capture directories failed durability"
                )
            raise first_failure


def _safe(path: Path) -> None:
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("capture store paths cannot contain symlinks")


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def _verify(path: Path, expected: str) -> None:
    _safe(path)
    if not path.is_file() or _digest(path) != expected:
        raise ValueError("immutable capture digest mismatch")
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError("immutable capture must have private owner permissions")


def _sync(path: Path) -> None:
    pending = _DIRECTORY_SYNCS.get()
    if pending is None:
        _flush_directory(path)
    else:
        _safe(path)
        pending.add(path)


def _write_payload(path: Path, data: bytes) -> None:
    if _DIRECTORY_SYNCS.get() is None:
        atomic_write(path, data)
    else:
        # File data and digest verification stay immediate. The owning import
        # cannot report success before its complete directory durability pass.
        atomic_write(path, data, sync_parent=False)
        _sync(path.parent)


def _link(source: Path, destination: Path, expected: str) -> None:
    _safe(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        _verify(destination, expected)
        if os.path.samefile(source, destination):
            return
    with tempfile.TemporaryDirectory(prefix=".capture-link-", dir=destination.parent) as temporary:
        target = Path(temporary) / "payload"
        try:
            os.link(source, target)
        except OSError as failure:
            if failure.errno != errno.EXDEV:
                raise
            atomic_write(target, source.read_bytes())
        _verify(target, expected)
        os.replace(target, destination)
        _sync(destination.parent)


def _store(root: Path, destination: Path, expected: str, data: bytes | None) -> Path:
    if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise ValueError("invalid capture digest")
    _safe(root)
    _safe(destination)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ValueError("capture store must have private owner permissions")
    for directory in (root / "objects", root / "locks"):
        _safe(directory)
        directory.mkdir(mode=0o700, exist_ok=True)
    cached = root / "objects" / expected
    lock_path = root / "locks" / expected
    _safe(cached)
    _safe(lock_path)
    if destination in (cached, lock_path):
        raise ValueError("capture destination must be separate from its store identity")
    with lock_path.open("a+b") as lock:
        lock_path.chmod(0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if destination.exists():
            _verify(destination, expected)
        if cached.exists():
            _verify(cached, expected)
        elif destination.exists():
            _link(destination, cached, expected)
        elif data is not None:
            _write_payload(cached, data)
            _verify(cached, expected)
        else:
            raise ValueError("missing immutable capture source")
        _link(cached, destination, expected)
    return destination


def store_bytes(root: Path, destination: Path, data: bytes) -> Path:
    """Write one complete payload once, then expose its original logical capture path."""
    return _store(root, destination, hashlib.sha256(data).hexdigest(), data)


def share_file(root: Path, destination: Path, expected: str) -> Path:
    """Seed or reuse exact already-captured bytes without changing any source record."""
    _verify(destination, expected)
    return _store(root, destination, expected, None)


def capture_digest(relative: Path) -> str | None:
    """Only closed content-addressed raw capture path families are immutable payloads."""
    parts = relative.parts
    if relative.is_absolute() or ".." in parts or not parts:
        return None
    patterns = {"captures": ".html", "responses": ".body", "browser-evidence": ".html"}
    if len(parts) == 2 and parts[0] in patterns:
        match = re.fullmatch(r"([0-9a-f]{64})" + re.escape(patterns[parts[0]]), parts[1])
        return match.group(1) if match else None
    if len(parts) == 3 and parts[0] == "extractions" and parts[2] == "index.md":
        return parts[1] if re.fullmatch(r"[0-9a-f]{64}", parts[1]) else None
    if (
        len(parts) >= 6
        and parts[:2] == ("articles", "content")
        and parts[2] in SOURCE_ROOTS
        and parts[-2] == "assets"
    ):
        return parts[-1] if re.fullmatch(r"[0-9a-f]{64}", parts[-1]) else None
    return None
