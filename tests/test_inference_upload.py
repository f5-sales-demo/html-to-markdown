"""Interrupted uploads never hold the broker or bypass immutable evidence custody."""

# These regression probes intentionally exercise internal upload and lock boundaries.
# pylint: disable=protected-access

import io
import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_codex_worker import body
from test_inference_queue import EXECUTOR, OWNER, packet

import html_to_markdown.inference_queue as queue
import html_to_markdown.inference_upload as upload
from html_to_markdown.enrichment.analysis import sha


def claimed(tmp_path):
    root = tmp_path / "queue"
    job = queue.submit(root, body(), EXECUTOR)
    queue.claim(root, OWNER, EXECUTOR)
    return root, job


def test_stalled_pipe_allows_claims_and_complete_matching_return(tmp_path, monkeypatch):
    root, job = claimed(tmp_path)
    data, evidence = packet(tmp_path, monkeypatch, job)
    prefix_read = threading.Event()
    original = upload._chunks

    def observed(stream):
        for chunk in original(stream):
            prefix_read.set()
            yield chunk

    monkeypatch.setattr(upload, "_chunks", observed)
    reader, writer = os.pipe()
    with os.fdopen(reader, "rb", buffering=0) as stream, ThreadPoolExecutor(3) as pool:
        partial = pool.submit(queue.receive, root, job, OWNER, sha(data), stream)
        try:
            os.write(writer, data[:64])
            assert prefix_read.wait(2)
            request = body()
            request["input"][0]["content"][0]["text"] += " second candidate"
            added = pool.submit(queue.submit, root, request, EXECUTOR).result(timeout=2)
            claims = pool.submit(queue.claim, root, OWNER, EXECUTOR).result(timeout=2)
            assert added in {item["job"] for item in claims}
            receipt = pool.submit(
                queue.receive, root, job, OWNER, sha(data), io.BytesIO(data)
            ).result(timeout=2)
            assert receipt["all_evidence_files_verified"]
            assert queue.result(root, job) == evidence
        finally:
            os.close(writer)
        with pytest.raises(ValueError, match="Incomplete or changed"):
            partial.result(timeout=2)
    assert sorted(p.stat().st_size for p in (root / job / "uploads").glob("*.tar.gz")) == [
        64,
        len(data),
    ]


@pytest.mark.parametrize("prefix", [b"", b"retained partial bytes"])
def test_pipe_inactivity_preserves_bytes_and_claim_without_receipt(tmp_path, monkeypatch, prefix):
    root, job = claimed(tmp_path)
    monkeypatch.setattr(upload, "UPLOAD_IDLE_TIMEOUT_SECONDS", 0.02)
    reader, writer = os.pipe()
    try:
        if prefix:
            os.write(writer, prefix)
        with (
            os.fdopen(reader, "rb", buffering=0) as stream,
            pytest.raises(TimeoutError, match="inactive"),
        ):
            queue.receive(root, job, OWNER, sha(prefix + b"missing"), stream)
    finally:
        os.close(writer)
    archive = next((root / job / "uploads").glob("*.tar.gz"))
    assert archive.read_bytes() == prefix
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert queue.result(root, job) is None
    assert not (root / job / "receipt.json").exists()
    assert queue.submit(root, body(), EXECUTOR) == job
    assert queue.claim(root, OWNER, EXECUTOR)[0]["job"] == job


def test_small_pipe_chunks_complete_without_waiting_for_full_read_buffer(tmp_path, monkeypatch):
    root, job = claimed(tmp_path)
    data, evidence = packet(tmp_path, monkeypatch, job)
    first_read = threading.Event()
    original = upload._chunks

    def observed(stream):
        for chunk in original(stream):
            first_read.set()
            yield chunk

    monkeypatch.setattr(upload, "_chunks", observed)
    reader, writer = os.pipe()
    with os.fdopen(reader, "rb", buffering=0) as stream, ThreadPoolExecutor(1) as pool:
        future = pool.submit(queue.receive, root, job, OWNER, sha(data), stream)
        try:
            os.write(writer, data[:32])
            assert first_read.wait(2)
            remaining = memoryview(data)[32:]
            while remaining:
                remaining = remaining[os.write(writer, remaining) :]
        finally:
            os.close(writer)
        assert future.result(timeout=2)["all_evidence_files_verified"]
    assert queue.result(root, job) == evidence


def test_concurrent_matching_uploads_converge_on_one_verified_receipt(tmp_path, monkeypatch):
    root, job = claimed(tmp_path)
    data, evidence = packet(tmp_path, monkeypatch, job)
    barrier = threading.Barrier(2)

    class Synchronized(io.BytesIO):
        def read(self, size=-1):
            if self.tell() == 0:
                barrier.wait(timeout=2)
            return super().read(size)

    with ThreadPoolExecutor(2) as pool:
        futures = [
            pool.submit(queue.receive, root, job, OWNER, sha(data), Synchronized(data))
            for _ in range(2)
        ]
        receipts = [future.result(timeout=4) for future in futures]
    assert receipts[0] == receipts[1]
    assert queue.result(root, job) == evidence
    assert len(list((root / job / "uploads").glob("*.tar.gz"))) == 2


@pytest.mark.parametrize(
    "change",
    ["owner", "executor_sha256", "request_sha256", "job", "state", "request_bytes", "invalidation"],
)
def test_custody_changes_during_upload_reject_commit_and_preserve_attempt(
    tmp_path, monkeypatch, change
):
    root, job = claimed(tmp_path)
    data, _ = packet(tmp_path, monkeypatch, job)

    class Changed(io.BytesIO):
        def read(self, size=-1):
            if self.tell() == 0:
                with queue._lock(root):
                    if change == "request_bytes":
                        path = root / job / "request.json"
                        path.write_bytes(path.read_bytes() + b" ")
                    elif change == "invalidation":
                        (root / job / "invalidated.json").write_text("{}")
                    else:
                        path = root / job / "state.json"
                        state = json.loads(path.read_bytes())
                        state[change] = "changed"
                        queue._write(path, state)
            return super().read(size)

    with pytest.raises(ValueError, match="custody changed|invalidated|unclaimed"):
        queue.receive(root, job, OWNER, sha(data), Changed(data))
    assert not (root / job / "receipt.json").exists()
    assert next((root / job / "uploads").glob("*.tar.gz")).read_bytes() == data


def test_bounded_transfer_failure_preserves_bytes_and_no_ack(tmp_path, monkeypatch):
    root, job = claimed(tmp_path)
    monkeypatch.setattr(queue, "MAX_TRANSFER_BYTES", 3)
    with pytest.raises(ValueError, match="bounded spool"):
        queue.receive(root, job, OWNER, sha(b"1234"), io.BytesIO(b"1234"))
    assert next((root / job / "uploads").glob("*.tar.gz")).read_bytes() == b"1234"
    assert not (root / job / "receipt.json").exists()


def test_upload_symlink_cannot_write_outside_private_job(tmp_path):
    root, job = claimed(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / job / "uploads").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        queue.receive(root, job, OWNER, sha(b"payload"), io.BytesIO(b"payload"))
    assert not list(outside.iterdir())
