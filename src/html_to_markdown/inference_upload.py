"""Preserve bounded private uploads without holding broker state locks."""

from __future__ import annotations

import hashlib
import os
import select
import stat
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

UPLOAD_IDLE_TIMEOUT_SECONDS = 120
CHUNK_BYTES = 1024 * 1024


def _chunks(stream: BinaryIO) -> Iterator[bytes]:
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError):
        descriptor = None
    if descriptor is not None and stat.S_ISFIFO(os.fstat(descriptor).st_mode):
        # CLI stdin is an untouched pipe. Raw reads return available bytes instead
        # of waiting for a whole buffered chunk after select reports readiness.
        while True:
            ready, _, _ = select.select([descriptor], [], [], UPLOAD_IDLE_TIMEOUT_SECONDS)
            if not ready:
                raise TimeoutError("Inference upload inactive for 120 seconds; bytes preserved")
            chunk = os.read(descriptor, CHUNK_BYTES)
            if not chunk:
                return
            yield chunk
    else:
        while chunk := stream.read(CHUNK_BYTES):
            yield chunk


def preserve_upload(
    uploads: Path, archive_sha256: str, stream: BinaryIO, maximum_bytes: int
) -> Path:
    """Retain every attempt, including inactivity, truncation and failed digests."""
    archive = uploads / (archive_sha256 + "-" + uuid.uuid4().hex + ".tar.gz")
    count, digest = 0, hashlib.sha256()
    with os.fdopen(os.open(archive, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as target:
        try:
            for chunk in _chunks(stream):
                count += len(chunk)
                target.write(chunk)
                digest.update(chunk)
                if count > maximum_bytes:
                    raise ValueError("Inference evidence transfer exceeds its bounded spool")
        finally:
            target.flush()
            os.fsync(target.fileno())
    if digest.hexdigest() != archive_sha256:
        raise ValueError("Incomplete or changed inference transfer; bytes preserved")
    return archive
