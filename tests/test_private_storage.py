"""Private persistence preserves complete evidence and rejects corruption or lost storage."""

import errno
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from html_to_markdown import private_storage
from html_to_markdown.private_storage import (
    StorageLease,
    StorageUnavailable,
    atomic_write,
    require_capacity,
)


@pytest.fixture(autouse=True)
def isolated_storage_events(tmp_path, monkeypatch):
    monkeypatch.setattr(private_storage, "STORAGE_EVENTS_ROOT", tmp_path / "storage-events")


def test_normal_private_write_retains_exact_bytes_and_permissions(tmp_path):
    path = tmp_path / "private/evidence.json"
    data = b'{"complete":"evidence"}'
    atomic_write(path, data)
    assert path.read_bytes() == data and path.stat().st_mode & 0o077 == 0


def test_supported_compression_preserves_complete_bytes_and_restores_metadata(
    tmp_path, monkeypatch
):
    path = tmp_path / "evidence.json"
    data = b"complete native source bytes\n" * 100
    calls = []
    monkeypatch.setattr(private_storage, "COMPRESSION_THRESHOLD", 1)
    monkeypatch.setattr(private_storage.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(private_storage.shutil, "which", lambda name: "/trusted/afsctool")

    def run(args, **kwargs):
        calls.append(args)
        target = Path(args[-1])
        before = target.stat()
        private_storage.os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000))
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(private_storage.subprocess, "run", run)
    atomic_write(path, data)
    assert path.read_bytes() == data and path.stat().st_mode & 0o077 == 0
    assert len(calls) == 1 and calls[0][:2] == ["/trusted/afsctool", "-c"]
    reports = [json.loads(p.read_bytes()) for p in (tmp_path / "storage-events").glob("*.json")]
    assert len(reports) == 1 and reports[0]["logical_bytes_verified"]


def test_missing_compression_tool_keeps_complete_private_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(private_storage, "COMPRESSION_THRESHOLD", 1)
    monkeypatch.setattr(private_storage.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(private_storage.shutil, "which", lambda name: None)
    monkeypatch.setattr(private_storage.subprocess, "run", lambda *a, **k: pytest.fail("no tool"))
    path = tmp_path / "evidence.json"
    atomic_write(path, b"complete input")
    assert path.read_bytes() == b"complete input"


def test_failed_compression_preserves_original_destination_and_partial_evidence(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(private_storage, "COMPRESSION_THRESHOLD", 1)
    monkeypatch.setattr(private_storage.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(private_storage.shutil, "which", lambda name: "/trusted/afsctool")
    path = tmp_path / "evidence.json"
    path.write_bytes(b"previous verified evidence")

    def run(args, **kwargs):
        Path(args[-1]).write_bytes(b"corrupt tool result")
        return SimpleNamespace(returncode=1, stderr=b"preserved tool failure")

    monkeypatch.setattr(private_storage.subprocess, "run", run)
    with pytest.raises(StorageUnavailable):
        atomic_write(path, b"new complete evidence")
    assert path.read_bytes() == b"previous verified evidence"
    assert list((tmp_path / "storage-events/incomplete-evidence").iterdir())
    assert any(
        p.read_bytes() == b"new complete evidence"
        for p in (tmp_path / "storage-events/incomplete-evidence").iterdir()
    )


def test_failed_atomic_publication_keeps_prior_output_and_complete_pending_bytes(
    tmp_path, monkeypatch
):
    path = tmp_path / "evidence.json"
    path.write_bytes(b"previous verified")
    original = private_storage.os.replace

    def replace(source, target):
        if Path(target) == path:
            raise OSError(errno.ENOSPC, "no space")
        return original(source, target)

    monkeypatch.setattr(private_storage.os, "replace", replace)
    with pytest.raises(StorageUnavailable):
        atomic_write(path, b"new completed response")
    assert path.read_bytes() == b"previous verified"
    assert any(
        p.read_bytes() == b"new completed response"
        for p in (tmp_path / "storage-events/incomplete-evidence").iterdir()
    )


def test_capacity_failure_is_visible_before_expensive_inference(tmp_path, monkeypatch):
    monkeypatch.setattr(private_storage.shutil, "disk_usage", lambda path: SimpleNamespace(free=1))
    with pytest.raises(StorageUnavailable, match="storage"):
        require_capacity(tmp_path, 100)
    assert not list(tmp_path.glob("*.codex.json"))


def test_concurrent_complete_checkpoints_never_publish_partial_bytes(tmp_path):
    path = tmp_path / "evidence.json"
    values = [bytes([i]) * 200 for i in range(8)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda value: atomic_write(path, value), values))
    assert path.read_bytes() in values and path.stat().st_mode & 0o077 == 0


def test_concurrent_inference_leases_cannot_overcommit_one_filesystem(tmp_path, monkeypatch):
    monkeypatch.setattr(
        private_storage.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=private_storage.RESERVE_BYTES + 400),
    )
    with StorageLease(tmp_path, 250):
        with pytest.raises(StorageUnavailable):
            StorageLease(tmp_path, 200)
        with StorageLease(tmp_path, 150), pytest.raises(StorageUnavailable):
            StorageLease(tmp_path, 1)
    with StorageLease(tmp_path, 400):
        pass


def test_storage_lease_is_released_after_interrupted_work(tmp_path, monkeypatch):
    monkeypatch.setattr(
        private_storage.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=private_storage.RESERVE_BYTES + 400),
    )
    with pytest.raises(RuntimeError), StorageLease(tmp_path, 400):
        raise RuntimeError("interrupted work")
    with StorageLease(tmp_path, 400):
        pass


def test_storage_operations_do_not_enter_captured_input_file_inventory(tmp_path, monkeypatch):
    path = tmp_path / "captured-inputs/article.json"
    monkeypatch.setattr(private_storage, "COMPRESSION_THRESHOLD", 1)
    monkeypatch.setattr(private_storage.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(private_storage.shutil, "which", lambda name: "/trusted/afsctool")
    monkeypatch.setattr(
        private_storage.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0)
    )
    atomic_write(path, b"complete captured source")
    assert [
        p.relative_to(path.parent).as_posix() for p in path.parent.rglob("*") if p.is_file()
    ] == ["article.json"]


def test_directory_sync_failure_reports_committed_complete_bytes(tmp_path, monkeypatch):
    path = tmp_path / "evidence.json"
    path.write_bytes(b"previous verified evidence")
    # The fault is injected at the directory synchronization boundary.
    # pylint: disable-next=protected-access
    original = private_storage._sync_directory

    def sync(directory):
        if directory == tmp_path:
            raise OSError(errno.EIO, "directory sync failed after rename")
        original(directory)

    monkeypatch.setattr(private_storage, "_sync_directory", sync)
    with pytest.raises(StorageUnavailable, match="committed") as failure:
        atomic_write(path, b"complete replacement evidence")
    assert failure.value.committed
    assert path.read_bytes() == b"complete replacement evidence"
    assert not list((tmp_path / "storage-events").glob("incomplete-evidence/*"))
    reports = [json.loads(p.read_bytes()) for p in (tmp_path / "storage-events").glob("*.json")]
    assert len(reports) == 1
    assert reports[0]["destination_committed"]
    assert not reports[0]["directory_sync_verified"]


def test_interrupted_native_scratch_remains_private_and_resumable(tmp_path, monkeypatch):
    seen = []
    with pytest.raises(KeyboardInterrupt), private_storage.private_scratch(tmp_path) as root:
        seen.append(root)
        (root / "output.json").write_bytes(b"complete model output")
        raise KeyboardInterrupt
    assert seen[0].stat().st_mode & 0o077 == 0
    assert (seen[0] / "output.json").read_bytes() == b"complete model output"
    assert list((tmp_path / "storage-events").glob("*.json"))
