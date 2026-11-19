"""Ubuntu owns durable inference evidence; Mac cleanup requires a complete receipt."""

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_codex_worker import body, client

import html_to_markdown.inference_queue as queue
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.inference_queue import claim, invalidate, receive, result, submit
from html_to_markdown.work_queue import pack_private_tree

OWNER = "a" * 32
OTHER = "c" * 32
EXECUTOR = "b" * 64


def packet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, job: str) -> tuple[bytes, Any]:
    spool = tmp_path / "spool"
    journal = spool / "journal"
    journal.mkdir(parents=True)

    def run(args, **_kwargs):
        Path(args[args.index("--output-last-message") + 1]).write_text('{"ok":true}')
        return SimpleNamespace(returncode=0, stdout='{"type":"turn.completed"}\n', stderr="")

    monkeypatch.setattr("html_to_markdown.enrichment.codex_client.subprocess.run", run)
    evidence = client(journal).execute(body())
    (spool / "envelope.json").write_bytes(
        json_bytes(
            {
                "job": job,
                "owner": OWNER,
                "executor_sha256": EXECUTOR,
                "evidence": evidence.model_dump(),
            }
        )
    )
    pins = {
        p.relative_to(spool).as_posix(): sha(p.read_bytes())
        for p in spool.rglob("*")
        if p.is_file()
    }
    (spool / "manifest.json").write_bytes(json_bytes(pins))
    stream = io.BytesIO()
    pack_private_tree(spool, stream)
    return stream.getvalue(), evidence


def test_sleep_resume_retains_same_claim_and_does_not_duplicate_job(tmp_path):
    root = tmp_path / "queue"
    job = submit(root, body(), EXECUTOR)
    assert submit(root, body(), EXECUTOR) == job
    first = claim(root, OWNER, EXECUTOR, limit=4)
    assert len(first) == 1 and first[0]["job"] == job
    assert not claim(root, OTHER, EXECUTOR, limit=4)
    assert claim(root, OWNER, EXECUTOR, limit=4) == first


def test_four_job_limit_and_changed_request_creates_distinct_identity(tmp_path):
    root = tmp_path / "queue"
    jobs = []
    for i in range(6):
        request = body()
        request["input"][0]["content"][0]["text"] += str(i)
        jobs.append(submit(root, request, EXECUTOR))
    assert len(set(jobs)) == 6
    assert len(claim(root, OWNER, EXECUTOR, limit=4)) == 4
    with pytest.raises(ValueError):
        claim(root, OWNER, EXECUTOR, limit=5)
    with pytest.raises(ValueError):
        claim(root, OTHER, EXECUTOR, limit=4)


def test_complete_evidence_roundtrip_replays_before_acknowledgment(tmp_path, monkeypatch):
    root = tmp_path / "queue"
    job = submit(root, body(), EXECUTOR)
    claim(root, OWNER, EXECUTOR)
    data, evidence = packet(tmp_path, monkeypatch, job)
    receipt = receive(root, job, OWNER, sha(data), io.BytesIO(data))
    assert receipt["transport_only"] and not receipt["article_approved"]
    assert receipt["all_evidence_files_verified"]
    assert result(root, job) == evidence
    assert receive(root, job, OWNER, sha(data), io.BytesIO(data)) == receipt
    assert result(root, job).request == body()


def test_partial_transfer_preserves_source_and_cannot_create_ack(tmp_path, monkeypatch):
    root = tmp_path / "queue"
    job = submit(root, body(), EXECUTOR)
    claim(root, OWNER, EXECUTOR)
    data, _ = packet(tmp_path, monkeypatch, job)
    with pytest.raises(ValueError):
        receive(root, job, OWNER, sha(data), io.BytesIO(data[: len(data) // 2]))
    assert result(root, job) is None
    assert list((root / job / "uploads").iterdir())


def test_stale_owner_or_executor_is_rejected_without_claim_rebinding(tmp_path, monkeypatch):
    root = tmp_path / "queue"
    job = submit(root, body(), EXECUTOR)
    with pytest.raises(ValueError):
        claim(root, OWNER, "d" * 64)
    claim(root, OWNER, EXECUTOR)
    data, _ = packet(tmp_path, monkeypatch, job)
    with pytest.raises(ValueError):
        receive(root, job, OTHER, sha(data), io.BytesIO(data))
    assert result(root, job) is None


def test_semantic_invalidation_preserves_attempts_but_never_reuses_approval(tmp_path, monkeypatch):
    root = tmp_path / "queue"
    job = submit(root, body(), EXECUTOR)
    claim(root, OWNER, EXECUTOR)
    data, evidence = packet(tmp_path, monkeypatch, job)
    receive(root, job, OWNER, sha(data), io.BytesIO(data))
    invalidate(root, job, evidence.response_sha256)
    assert result(root, job) is None
    replacement = submit(root, body(), EXECUTOR)
    assert replacement != job
    assert (root / job / "receipt.json").exists()
    assert list((root / job / "uploads").glob("*.tar.gz"))


@pytest.mark.parametrize("identity", ["../escape", "/absolute", "f" * 64 + "/../000001"])
def test_unsafe_job_id_cannot_read_outside_queue(tmp_path, identity):
    with pytest.raises(ValueError):
        result(tmp_path, identity)


def test_interrupted_receipt_commit_cannot_create_new_inference_attempt(tmp_path, monkeypatch):

    root = tmp_path / "queue"
    job = submit(root, body(), EXECUTOR)
    claim(root, OWNER, EXECUTOR)
    data, _ = packet(tmp_path, monkeypatch, job)
    original = queue.atomic_private

    def fail_receipt(path, value):
        if path.name == "receipt.json":
            raise OSError("simulated checkpoint interruption")
        original(path, value)

    monkeypatch.setattr(queue, "atomic_private", fail_receipt)
    with pytest.raises(OSError):
        receive(root, job, OWNER, sha(data), io.BytesIO(data))
    assert result(root, job) is None
    assert submit(root, body(), EXECUTOR) == job
    monkeypatch.setattr(queue, "atomic_private", original)
    assert receive(root, job, OWNER, sha(data), io.BytesIO(data))["all_evidence_files_verified"]
    assert result(root, job).error is None


def test_interrupted_submission_never_exposes_an_incomplete_job(tmp_path, monkeypatch):
    root = tmp_path / "queue"
    original = queue.atomic_private

    def fail_state(path, value):
        if path.name == "state.json":
            raise OSError("simulated submission interruption")
        original(path, value)

    monkeypatch.setattr(queue, "atomic_private", fail_state)
    with pytest.raises(OSError):
        submit(root, body(), EXECUTOR)
    assert not list(root.glob("*/*/state.json"))
    assert list(root.glob("*/.submission-*/request.json"))
    monkeypatch.setattr(queue, "atomic_private", original)
    job = submit(root, body(), EXECUTOR)
    assert claim(root, OWNER, EXECUTOR)[0]["job"] == job
