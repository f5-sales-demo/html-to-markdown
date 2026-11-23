"""Capture readiness follows complete durable import, without per-payload parent flushes."""

from __future__ import annotations

import errno
import io
import os
import stat
import tarfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from html_to_markdown import capture_store
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.work_queue import unpack_queue


def _archive(root: Path) -> tuple[Path, dict[str, bytes]]:
    payloads = {
        f"captures/{sha(raw)}.html": raw
        for raw in (f"Complete synthetic source article {index}.".encode() for index in range(13))
    }
    payloads["inventory.json"] = b'{"documents":[]}'
    archive = root / "sources.tar"
    with tarfile.open(archive, "w") as stream:
        for name, raw in sorted(payloads.items()):
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            stream.addfile(member, io.BytesIO(raw))
    return archive, payloads


def _is_directory(descriptor: int, path: Path) -> bool:
    if not path.exists():
        return False
    observed = os.fstat(descriptor)
    expected = path.stat()
    return stat.S_ISDIR(observed.st_mode) and (observed.st_dev, observed.st_ino) == (
        expected.st_dev,
        expected.st_ino,
    )


def test_complete_import_flushes_shared_capture_parent_once(tmp_path, monkeypatch):
    archive, payloads = _archive(tmp_path)
    destination = tmp_path / "received"
    original_sync = os.fsync
    capture_flushes = []
    store_flushes = []
    file_flushes = []

    def sync(descriptor):
        if _is_directory(descriptor, destination / "captures"):
            capture_flushes.append(descriptor)
        if _is_directory(descriptor, tmp_path / "store/objects"):
            store_flushes.append(descriptor)
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            file_flushes.append(descriptor)
        original_sync(descriptor)

    monkeypatch.setattr(capture_store.os, "fsync", sync)
    unpack_queue(archive, destination, capture_store=tmp_path / "store")
    for name, raw in payloads.items():
        path = destination / name
        assert path.read_bytes() == raw
        assert path.stat().st_mode & 0o077 == 0
    assert len(capture_flushes) == 1
    assert len(store_flushes) == 1
    assert len(file_flushes) >= len(payloads)


def test_failed_final_capture_sync_preserves_complete_input_and_rejects_readiness(
    tmp_path, monkeypatch
):
    archive, payloads = _archive(tmp_path)
    original_archive = archive.read_bytes()
    destination = tmp_path / "received"
    original_sync = os.fsync

    def sync(descriptor):
        if _is_directory(descriptor, destination / "captures"):
            raise OSError(errno.EIO, "capture directory durability failed")
        original_sync(descriptor)

    with monkeypatch.context() as scope:
        scope.setattr(capture_store.os, "fsync", sync)
        with pytest.raises(OSError, match="durability"):
            unpack_queue(archive, destination, capture_store=tmp_path / "store")
    assert archive.read_bytes() == original_archive
    assert all((destination / name).read_bytes() == raw for name, raw in payloads.items())
    unpack_queue(archive, destination, capture_store=tmp_path / "store")
    assert all((destination / name).read_bytes() == raw for name, raw in payloads.items())


def test_standalone_payload_returns_only_after_capture_directory_sync(tmp_path, monkeypatch):
    destination = tmp_path / "capture/payload"
    original_sync = os.fsync
    observed = []

    def sync(descriptor):
        if _is_directory(descriptor, destination.parent):
            observed.append(descriptor)
        original_sync(descriptor)

    monkeypatch.setattr(capture_store.os, "fsync", sync)
    capture_store.store_bytes(tmp_path / "store", destination, b"complete synthetic source")
    assert observed
    assert destination.read_bytes() == b"complete synthetic source"


def test_complete_preflight_rejects_late_corruption_before_any_import(tmp_path):
    archive, payloads = _archive(tmp_path)
    name = sorted(payloads)[-2]
    payloads[name] = b"changed source bytes under the original digest"
    with tarfile.open(archive, "w") as stream:
        for member_name, raw in sorted(payloads.items()):
            member = tarfile.TarInfo(member_name)
            member.size = len(raw)
            stream.addfile(member, io.BytesIO(raw))
    with pytest.raises(ValueError, match="digest"):
        unpack_queue(archive, tmp_path / "received", capture_store=tmp_path / "store")
    assert not (tmp_path / "received").exists()
    assert not (tmp_path / "store").exists()


def test_independent_thread_cannot_flush_or_discard_another_import_scope(tmp_path, monkeypatch):
    first = tmp_path / "first/capture/payload"
    second = tmp_path / "second/capture/payload"
    ready = Event()
    finish = Event()
    observed = []
    original_sync = os.fsync

    def sync(descriptor):
        for path in [first.parent, second.parent]:
            if _is_directory(descriptor, path):
                observed.append(path)
        original_sync(descriptor)

    def held_import():
        with capture_store.durable_links():
            capture_store.store_bytes(tmp_path / "first/store", first, b"first synthetic source")
            ready.set()
            assert finish.wait(timeout=5)

    monkeypatch.setattr(capture_store.os, "fsync", sync)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(held_import)
        try:
            assert ready.wait(timeout=5)
            with capture_store.durable_links():
                capture_store.store_bytes(
                    tmp_path / "second/store", second, b"second synthetic source"
                )
            assert observed == [second.parent]
        finally:
            finish.set()
        pending.result(timeout=5)
    assert observed == [second.parent, first.parent]
    assert first.read_bytes() == b"first synthetic source"
    assert second.read_bytes() == b"second synthetic source"
