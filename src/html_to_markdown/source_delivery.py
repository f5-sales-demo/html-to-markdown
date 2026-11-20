"""Private source deliveries share immutable bytes and independently replay promotion."""

from __future__ import annotations

import fcntl
import os
import platform
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from .content_inventory import atomic_private
from .curation import json_bytes
from .enrichment.analysis import sha
from .source_inputs import verify_source
from .urls import SOURCE_ROOTS


def reject_linked_path(path: Path) -> None:
    if any(part.is_symlink() for part in [path, *path.parents]):
        raise ValueError("private source path cannot contain symlinks")


def source_destination(root: Path, relative: str, digest: str) -> Path:
    path = Path(relative)
    if (
        path.is_absolute()
        or ".." in path.parts
        or len(path.parts) != 2
        or path.parts[-1] not in SOURCE_ROOTS
    ):
        raise ValueError("invalid received queue identity")
    if path.as_posix() != relative or "\\" in relative:
        raise ValueError("invalid received queue identity")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("invalid received admission pin")
    destination = root / ".reviewed" / path / digest
    reject_linked_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return destination


@contextmanager
def private_lock(path: Path) -> Iterator[None]:
    reject_linked_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(descriptor, "a+b") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise ValueError("private source lock is not a regular file")
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def verify_bound_source(directory: Path, digest: str, source: str) -> dict[str, Any]:
    receipt = verify_source(directory, digest)
    if receipt["source"] != source:
        raise ValueError("received admission source differs from queue identity")
    return receipt


def promote_source(root: Path, candidate: Path, destination: Path, digest: str) -> Path:
    """Keep assembly outside the promotion and final independent verification interval."""
    source = destination.parent.name
    verify_bound_source(candidate, digest, source)
    with private_lock(root / ".publication/coordinator.lock"):
        reject_linked_path(destination)
        if destination.exists():
            verify_bound_source(destination, digest, source)
            return destination
        candidate.rename(destination)
        try:
            verify_bound_source(destination, digest, source)
        except Exception:
            # Preserve the candidate privately and remove its published namespace
            # before releasing the shared publication coordination lock.
            destination.rename(candidate)
            raise
    return destination


def _members(root: Path) -> dict[str, tuple[int, int, int, int, int]]:
    reject_linked_path(root)
    if not root.is_dir():
        raise ValueError("private source snapshot is not a directory")
    result: dict[str, tuple[int, int, int, int, int]] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        if "\\" in path.relative_to(root).as_posix():
            raise ValueError("private source snapshot contains an unsafe address")
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            value = (0, 0, info.st_mode, 0, info.st_mtime_ns)
        elif stat.S_ISREG(info.st_mode):
            value = (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)
        else:
            raise ValueError("private source snapshot contains an unsafe member")
        result[path.relative_to(root).as_posix()] = value
    return result


def _link_member(source: str, destination: str) -> str:
    origin, target = Path(source), Path(destination)
    reject_linked_path(origin)
    reject_linked_path(target)
    if target.exists():
        before, after = origin.stat(), target.stat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError("interrupted private source snapshot has conflicting bytes")
    else:
        # EXDEV and storage failures stay explicit: copying would silently
        # recreate the full payload duplication this transport removes.
        os.link(origin, target, follow_symlinks=False)
    return destination


def _clone(source: Path, candidate: Path, expected: dict[str, Any]) -> None:
    if source.stat().st_dev != candidate.parent.stat().st_dev:
        raise ValueError("local source snapshots require a common filesystem")
    if candidate.exists():
        _members(candidate)
    shutil.copytree(source, candidate, copy_function=_link_member, dirs_exist_ok=True)
    if _members(source) != expected or _members(candidate) != expected:
        raise ValueError("private source snapshot changed during delivery")


def receive_source_tree(root: Path, relative: str, digest: str, source: Path) -> Path:
    """Resume same-host delivery without copying payloads or rebinding model approvals."""
    if platform.system() != "Linux" or not root.is_absolute() or not source.is_absolute():
        raise ValueError("local source admission is routed to an absolute Ubuntu queue")
    destination = source_destination(root, relative, digest)
    expected = _members(source)
    receipt = verify_bound_source(source, digest, Path(relative).name)
    binding = {
        "source": str(source),
        "queue": relative,
        "source_review_sha256": digest,
        "member_guard_sha256": sha(json_bytes(expected)),
    }
    with private_lock(destination.parent / ".receive.lock"):
        if destination.exists():
            verify_bound_source(destination, digest, receipt["source"])
            return destination
        staging = destination.parent / (".receiving-source-" + digest)
        reject_linked_path(staging)
        transaction = staging / "transfer.json"
        if staging.exists():
            import json  # pylint: disable=import-outside-toplevel

            reject_linked_path(transaction)
            try:
                saved = json.loads(transaction.read_bytes())
            except (OSError, ValueError):
                saved = {}
            if (
                not isinstance(saved, dict)
                or saved.get("binding") != binding
                or saved.get("error_class") == "ValueError"
            ):
                staging.rename(destination.parent / (".failed-source-" + uuid4().hex))
        staging.mkdir(mode=0o700, exist_ok=True)
        atomic_private(transaction, json_bytes({"state": "copying", "binding": binding}))
        candidate = staging / "source"
        try:
            _clone(source, candidate, expected)
            verify_bound_source(source, digest, receipt["source"])
            result = promote_source(root, candidate, destination, digest)
        except Exception as failure:
            atomic_private(
                transaction,
                json_bytes(
                    {
                        "state": "failed",
                        "binding": binding,
                        "error_class": type(failure).__name__,
                        "error": str(failure),
                    }
                ),
            )
            raise
        acknowledgment = {
            "state": "source_received",
            "binding": binding,
            "all_members_shared": True,
            "source_replay_verified": True,
            "final_replay_verified": True,
            "article_approved_by_transport": False,
        }
        atomic_private(
            destination.parent / ".receipts" / (digest + ".json"), json_bytes(acknowledgment)
        )
        shutil.rmtree(staging)
        return result
