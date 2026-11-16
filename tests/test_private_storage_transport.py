"""Storage failures cannot submit inference or invalidate complete cached decisions."""

import errno
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_codex_worker import body, client

from html_to_markdown import private_storage
from html_to_markdown.enrichment.gates import request_hash
from html_to_markdown.private_storage import StorageUnavailable, atomic_write


def completed_transport(args, **kwargs):
    Path(args[args.index("--output-last-message") + 1]).write_text('{"ok":true}')
    return SimpleNamespace(returncode=0, stdout='{"type":"turn.completed"}\n', stderr="")


def test_no_free_storage_fails_before_inference_and_preserves_a_pending_receipt(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(private_storage.shutil, "disk_usage", lambda path: SimpleNamespace(free=1))
    monkeypatch.setattr(
        "html_to_markdown.enrichment.codex_client.subprocess.run",
        lambda *a, **k: pytest.fail("new inference must not run"),
    )
    request = body()
    with pytest.raises(StorageUnavailable):
        client(tmp_path).execute(request)
    pending = json.loads(
        (tmp_path / (request_hash(request) + ".storage-pending.json")).read_bytes()
    )
    assert pending["request_sha256"] == request_hash(request)
    assert not pending["inference_invoked"] and not pending["source_or_review_approval"]
    assert not list(tmp_path.glob("*.attempt-*.json"))


def test_output_copy_failure_resumes_from_complete_cache_without_inference(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "html_to_markdown.enrichment.codex_client.subprocess.run", completed_transport
    )
    monkeypatch.setattr(private_storage, "STORAGE_EVENTS_ROOT", tmp_path / "storage-events")
    worker = client(tmp_path / "journal")
    worker.journal.mkdir()
    request = body()
    evidence = worker.execute(request)
    assert evidence.error is None
    cached_bytes = (worker.journal / (request_hash(request) + ".codex.json")).read_bytes()
    target = tmp_path / "curation.json"
    original = private_storage.os.replace

    def replace(source, destination):
        if Path(destination) == target:
            raise OSError(errno.ENOSPC, "full disk during curation copy")
        return original(source, destination)

    monkeypatch.setattr(private_storage.os, "replace", replace)
    with pytest.raises(StorageUnavailable):
        atomic_write(target, cached_bytes)
    assert (worker.journal / (request_hash(request) + ".codex.json")).read_bytes() == cached_bytes
    monkeypatch.setattr(private_storage.os, "replace", original)
    monkeypatch.setattr(private_storage.shutil, "disk_usage", lambda path: SimpleNamespace(free=1))
    monkeypatch.setattr(
        "html_to_markdown.enrichment.codex_client.subprocess.run",
        lambda *a, **k: pytest.fail("valid cached response must be reused"),
    )
    assert worker.execute(request) == evidence
    atomic_write(target, cached_bytes)
    assert target.read_bytes() == cached_bytes
    assert len(list(worker.journal.glob("*.attempt-*.json"))) == 1


def test_semantically_rejected_complete_checkpoint_cannot_restore_a_cache(tmp_path, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return completed_transport(args, **kwargs)

    monkeypatch.setattr("html_to_markdown.enrichment.codex_client.subprocess.run", run)
    worker = client(tmp_path)
    request = body()
    first = worker.execute(request)
    worker.invalidate(first)
    assert not (tmp_path / (request_hash(request) + ".codex.json")).exists()
    assert worker.execute(request).error is None and len(calls) == 2
    assert list(tmp_path.glob("*.rejected-*.json"))


def test_completed_transport_checkpoint_recovers_after_large_attempt_write_fails(
    tmp_path, monkeypatch
):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return completed_transport(args, **kwargs)

    monkeypatch.setattr("html_to_markdown.enrichment.codex_client.subprocess.run", run)
    writer = atomic_write

    def fail_attempt(path, data):
        if ".attempt-" in path.name:
            raise StorageUnavailable("simulated unavailable attempt storage")
        return writer(path, data)

    monkeypatch.setattr("html_to_markdown.enrichment.codex_client.atomic_write", fail_attempt)
    worker = client(tmp_path)
    request = body()
    with pytest.raises(StorageUnavailable):
        worker.execute(request)
    assert len(calls) == 1 and list(tmp_path.glob("*.completed-*.json"))
    monkeypatch.setattr("html_to_markdown.enrichment.codex_client.atomic_write", writer)
    monkeypatch.setattr(
        "html_to_markdown.enrichment.codex_client.subprocess.run",
        lambda *a, **k: pytest.fail("complete transport must be recovered"),
    )
    recovered = worker.execute(request)
    assert recovered.error is None and recovered.request == request
    assert (tmp_path / (request_hash(request) + ".codex.json")).exists()
