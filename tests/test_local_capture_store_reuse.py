"""Ubuntu imports reuse verified collector objects without duplicating captured bytes."""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from html_to_markdown import mac_worker
from html_to_markdown.capture_store import store_bytes
from html_to_markdown.enrichment.analysis import sha


def _input(tmp_path: Path, monkeypatch):
    queue = tmp_path / "queue"
    raw = b"Complete synthetic public article source."
    digest = sha(raw)
    collected = queue / "capture/my-f5-com/captures" / (digest + ".html")
    store_bytes(queue / ".capture-store", collected, raw)
    member_name = "captures/" + digest + ".html"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in {member_name: raw, "inventory.json": b'{"documents":[]}'}.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    archive_bytes = buffer.getvalue()

    def receive(args, **kwargs):
        assert args[args.index("--host") + 1] == "local"
        assert "export-review-queue" in args
        kwargs["stdout"].write(archive_bytes)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(mac_worker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(mac_worker.subprocess, "run", receive)
    worker = tmp_path / "worker"
    capture = worker / "capture/my-f5-com/capture"
    capture.parent.mkdir(parents=True)
    return queue, worker, capture, collected, member_name


def _receive(queue: Path, capture: Path) -> None:
    mac_worker._receive_capture(
        Path("/synthetic-transport"),
        "local",
        "/synthetic-runtime",
        str(queue),
        "capture/my-f5-com",
        capture,
    )


def test_local_import_reuses_the_collectors_complete_digest_guarded_object(tmp_path, monkeypatch):
    queue, worker, capture, collected, name = _input(tmp_path, monkeypatch)
    original = collected.read_bytes()
    _receive(queue, capture)
    assert (capture / name).read_bytes() == collected.read_bytes() == original
    assert os.path.samefile(capture / name, collected)
    assert not (worker / ".capture-store").exists()
    assert (capture / "inventory.json").read_bytes() == b'{"documents":[]}'


def test_corrupt_collector_cache_is_rejected_without_repair_or_source_admission(
    tmp_path, monkeypatch
):
    queue, _, capture, collected, name = _input(tmp_path, monkeypatch)
    collected.write_bytes(b"corrupted captured object")
    original = collected.read_bytes()
    with pytest.raises(ValueError, match="digest"):
        _receive(queue, capture)
    assert collected.read_bytes() == original
    assert not (capture / name).exists()
    assert not (capture / "inventory.json").exists()


def test_missing_shared_store_preserves_the_separate_worker_store(tmp_path, monkeypatch):
    queue, worker, capture, _, name = _input(tmp_path, monkeypatch)
    detached_queue = tmp_path / "detached-queue"
    detached_queue.mkdir()
    _receive(detached_queue, capture)
    assert (worker / ".capture-store").is_dir()
    assert (capture / name).read_bytes() == b"Complete synthetic public article source."


def test_collector_store_on_another_filesystem_keeps_the_local_worker_cache(tmp_path, monkeypatch):
    queue, worker, capture, collected, name = _input(tmp_path, monkeypatch)
    original_stat = Path.stat

    def device_stat(path, *args, **kwargs):
        observed = original_stat(path, *args, **kwargs)
        if path == queue / ".capture-store":
            values = list(observed)
            values[2] += 1
            return os.stat_result(values)
        return observed

    monkeypatch.setattr(Path, "stat", device_stat)
    _receive(queue, capture)
    assert (worker / ".capture-store").is_dir()
    assert (capture / name).read_bytes() == collected.read_bytes()
    assert not os.path.samefile(capture / name, collected)
