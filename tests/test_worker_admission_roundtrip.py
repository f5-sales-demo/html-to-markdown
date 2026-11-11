"""Interruptions and changed review context cannot skip a private admission return."""

import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_source_review_jobs import LocalClient
from test_source_review_jobs import capture as capture

from html_to_markdown.mac_worker import worker_once
from html_to_markdown.work_queue import pack_private_tree, receive_source


def test_return_interruption_retries_then_context_change_revalidates(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = json.loads((capture / "inventory.json").read_bytes())
    inventory.update(binding={"source": "my-f5-com"}, ended_at="2026-11-10T00:03:00Z")
    inventory["completeness"]["capture"] = True
    (capture / "inventory.json").write_text(json.dumps(inventory))
    monkeypatch.setattr("html_to_markdown.mac_worker.platform.system", lambda: "Darwin")
    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", LocalClient)
    returns = []
    fail = True

    def run(args: list[str], **kwargs: object):
        nonlocal fail
        if "list-review-queues" in args:
            return SimpleNamespace(
                stdout=json.dumps(
                    [
                        {
                            "queue": "run/my-f5-com",
                            "source": "my-f5-com",
                            "fingerprint": "a" * 64,
                            "captured": 1,
                            "discovered": 1,
                        }
                    ]
                ).encode()
            )
        if "export-review-queue" in args:
            pack_private_tree(capture, kwargs["stdout"])
        elif "receive-review-input" in args:
            if fail:
                fail = False
                raise subprocess.CalledProcessError(1, args)
            pin = args[args.index("--review-sha256") + 1]
            archive = io.BytesIO(kwargs["stdin"].read())
            destination = receive_source(tmp_path / "ubuntu", "run/my-f5-com", pin, archive)
            assert (destination / "content/my-f5-com/K000123456/index.md").exists()
            returns.append(pin)
        else:
            return SimpleNamespace(stdout=b'{"state":"waiting_for_software"}')
        return SimpleNamespace(stdout=b"{}")

    monkeypatch.setattr("html_to_markdown.mac_worker.subprocess.run", run)
    args = dict(
        host="worker.example.invalid",
        remote_executable="/private/html-to-markdown",
        remote_queue="/private/queue",
        output=tmp_path / "worker",
        transport=Path("/private/transport"),
    )
    with pytest.raises(subprocess.CalledProcessError):
        worker_once(**args)
    checkpoint = tmp_path / "worker/run/my-f5-com/checkpoint.json"
    assert not checkpoint.exists()
    assert worker_once(**args)["state"] == "idle"
    assert json.loads(checkpoint.read_bytes())["returned_admission_sha256"] == returns[0]
    worker_once(**args)
    assert len(returns) == 1
    monkeypatch.setattr("html_to_markdown.source_review.review_context_digest", lambda: "changed")
    monkeypatch.setattr("html_to_markdown.mac_worker.review_context_digest", lambda: "changed")
    worker_once(**args)
    assert len(returns) == 2 and returns[0] != returns[1]
