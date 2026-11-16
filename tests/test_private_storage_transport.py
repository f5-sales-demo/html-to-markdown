"""Storage failures cannot submit inference or invalidate complete cached decisions."""

import errno
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_codex_worker import body, client

from html_to_markdown import private_storage
from html_to_markdown.enrichment.analysis import sha
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
    scratch = list(tmp_path.glob("markdown-codex-*/output.json"))
    assert len(scratch) == 1 and json.loads(scratch[0].read_bytes()) == {"ok": True}
    monkeypatch.setattr("html_to_markdown.enrichment.codex_client.atomic_write", writer)
    monkeypatch.setattr(
        "html_to_markdown.enrichment.codex_client.subprocess.run",
        lambda *a, **k: pytest.fail("complete transport must be recovered"),
    )
    recovered = worker.execute(request)
    assert recovered.error is None and recovered.request == request
    assert (tmp_path / (request_hash(request) + ".codex.json")).exists()


def test_failed_attempt_copy_and_rejected_completion_never_reuse_an_attempt_number(
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
            raise StorageUnavailable("failed large copy")
        return writer(path, data)

    monkeypatch.setattr("html_to_markdown.enrichment.codex_client.atomic_write", fail_attempt)
    worker = client(tmp_path)
    request = body()
    digest = request_hash(request)
    with pytest.raises(StorageUnavailable):
        worker.execute(request)
    first = tmp_path / (digest + ".completed-001.json")
    original = first.read_bytes()
    monkeypatch.setattr("html_to_markdown.enrichment.codex_client.atomic_write", writer)
    recovered = worker.execute(request)
    worker.invalidate(recovered)
    assert worker.execute(request).error is None
    assert first.read_bytes() == original
    assert (tmp_path / (digest + ".completed-002.json")).is_file()
    assert (tmp_path / (digest + ".attempt-002.json")).is_file()
    assert len(calls) == 2


def test_interrupted_completion_write_retains_start_and_next_attempt_number(tmp_path, monkeypatch):
    writer = atomic_write

    def fail_completion(path, data):
        if ".completed-" in path.name:
            raise StorageUnavailable("failed completed checkpoint")
        return writer(path, data)

    monkeypatch.setattr(
        "html_to_markdown.enrichment.codex_client.subprocess.run", completed_transport
    )
    monkeypatch.setattr("html_to_markdown.enrichment.codex_client.atomic_write", fail_completion)
    worker = client(tmp_path)
    request = body()
    digest = request_hash(request)
    with pytest.raises(StorageUnavailable):
        worker.execute(request)
    assert (tmp_path / (digest + ".started-001.json")).is_file()
    monkeypatch.setattr("html_to_markdown.enrichment.codex_client.atomic_write", writer)
    assert worker.execute(request).error is None
    assert (tmp_path / (digest + ".completed-002.json")).is_file()


def test_native_scratch_uses_reserved_filesystem_instead_of_system_temp(tmp_path, monkeypatch):
    system_temp = tmp_path / "other-filesystem"
    system_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_temp))
    journal = tmp_path / "journal"
    journal.mkdir()
    calls = []

    def run(args, **kwargs):
        root = Path(args[args.index("-C") + 1])
        assert root.parent == journal
        assert root.stat().st_mode & 0o077 == 0
        calls.append(root)
        return completed_transport(args, **kwargs)

    monkeypatch.setattr("html_to_markdown.enrichment.codex_client.subprocess.run", run)
    assert client(journal).execute(body()).error is None
    assert len(calls) == 1 and not calls[0].exists()
    assert not list(system_temp.iterdir())


def test_semantic_invalidation_without_cache_still_blocks_checkpoint_recovery(
    tmp_path, monkeypatch
):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return completed_transport(args, **kwargs)

    monkeypatch.setattr("html_to_markdown.enrichment.codex_client.subprocess.run", run)
    worker = client(tmp_path)
    request = body()
    first = worker.execute(request)
    cache = tmp_path / (request_hash(request) + ".codex.json")
    copied = cache.read_bytes()
    cache.unlink()
    worker.invalidate(first)
    # pylint: disable-next=protected-access
    assert worker._completed_checkpoint(request, request_hash(request)) is None
    cache.write_bytes(copied)
    assert worker.execute(request).error is None and len(calls) == 2


@pytest.mark.parametrize(
    "tampering", ["partial", "shape", "request_bytes", "request_hash", "response_hash", "tools"]
)
def test_checkpoint_tampering_never_becomes_a_cached_approval(tmp_path, monkeypatch, tampering):
    monkeypatch.setattr(
        "html_to_markdown.enrichment.codex_client.subprocess.run", completed_transport
    )
    worker = client(tmp_path)
    request = body()
    worker.execute(request)
    digest = request_hash(request)
    checkpoint = tmp_path / (digest + ".completed-001.json")
    (tmp_path / (digest + ".codex.json")).unlink()
    value = json.loads(checkpoint.read_bytes())
    if tampering == "request_bytes":
        value["request_file_sha256"] = "0" * 64
    elif tampering == "request_hash":
        value["evidence"]["request_sha256"] = "0" * 64
    elif tampering == "response_hash":
        value["evidence"]["response_sha256"] = "0" * 64
    elif tampering == "tools":
        value["evidence"]["response"]["events"] = [
            {"type": "item.completed", "item": {"type": "command_execution"}}
        ]
        value["evidence"]["response_sha256"] = sha(
            json.dumps(value["evidence"]["response"], sort_keys=True, separators=(",", ":"))
        )
    checkpoint.write_bytes(
        b"{"
        if tampering == "partial"
        else b"[]"
        if tampering == "shape"
        else json.dumps(value).encode()
    )
    original = checkpoint.read_bytes()
    # pylint: disable-next=protected-access
    assert worker._completed_checkpoint(request, digest) is None
    assert checkpoint.read_bytes() == original
    assert list(tmp_path.glob("*.rejected-completion-*.json"))
    assert worker.execute(request).error is None
    assert checkpoint.read_bytes() == original
    assert (tmp_path / (digest + ".completed-002.json")).exists()


def test_changed_original_request_is_not_rebound_for_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "html_to_markdown.enrichment.codex_client.subprocess.run", completed_transport
    )
    worker = client(tmp_path)
    request = body()
    worker.execute(request)
    digest = request_hash(request)
    (tmp_path / (digest + ".codex.json")).unlink()
    persisted = tmp_path / (digest + ".request.json")
    changed = body()
    changed["instructions"] += " changed policy"
    persisted.write_text(json.dumps(changed))
    monkeypatch.setattr(
        "html_to_markdown.enrichment.codex_client.subprocess.run",
        lambda *a, **k: pytest.fail("wrong request must not trigger another inference"),
    )
    with pytest.raises(ValueError, match="original request digest changed"):
        worker.execute(request)
