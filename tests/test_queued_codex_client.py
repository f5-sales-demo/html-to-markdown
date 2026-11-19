"""Ubuntu clients wait on durable jobs; they cannot invoke an Ubuntu Codex."""

import pytest
from test_codex_worker import body

from html_to_markdown.enrichment.queued_client import QueuedClient
from html_to_markdown.inference_queue import claim


def test_ubuntu_client_requires_explicit_queue_and_never_reads_mac_credentials(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("HTML_TO_MARKDOWN_INFERENCE_QUEUE", raising=False)
    with pytest.raises(ValueError):
        QueuedClient(tmp_path / "journal")


def test_pending_mac_job_keeps_one_exact_request_across_ubuntu_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "html_to_markdown.enrichment.queued_client.platform.system", lambda: "Linux"
    )
    root = tmp_path / "inference"
    monkeypatch.setenv("HTML_TO_MARKDOWN_INFERENCE_QUEUE", str(root))
    worker = QueuedClient(tmp_path / "journal")
    calls = []

    def wait(_delay):
        calls.append(True)
        jobs = claim(root, "a" * 32, worker.executor_sha256)
        assert len(jobs) == 1 and jobs[0]["request"] == body()
        raise InterruptedError("simulate Ubuntu worker interruption")

    monkeypatch.setattr("html_to_markdown.enrichment.queued_client.time.sleep", wait)
    with pytest.raises(InterruptedError):
        worker.execute(body())
    worker.close()
    job_ids = [p.parent.relative_to(root).as_posix() for p in root.glob("*/*/request.json")]
    resumed = QueuedClient(tmp_path / "journal")
    with pytest.raises(InterruptedError):
        resumed.execute(body())
    resumed.close()
    assert job_ids == [p.parent.relative_to(root).as_posix() for p in root.glob("*/*/request.json")]
    assert len(calls) == 2
