"""Interruptions and changed review context cannot skip a private admission return."""

import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_source_review_jobs import LocalClient

from html_to_markdown.content_inventory import atomic_private
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.mac_worker import worker_once
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document, split_document
from html_to_markdown.source_inputs import seal_source, verify_source
from html_to_markdown.source_review_jobs import prepare_reviews, run_reviews
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

    def run(args: list[str], **kwargs: Any):
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
    args: dict[str, Any] = dict(  # pylint: disable=use-dict-literal
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


def test_partial_admission_snapshots_remain_verified_after_later_batches(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = json.loads((capture / "inventory.json").read_bytes())
    inventory.update(binding={"source": "my-f5-com"}, ended_at="2026-11-10T00:03:00Z")
    inventory["completeness"]["capture"] = True
    item = json.loads(json.dumps(inventory["documents"][0]))
    _, body = split_document((capture / item["article_path"]).read_text())
    item.update(
        identity="my-f5-com:K000123457",
        url="https://my.f5.com/manage/s/article/K000123457",
        article_path="articles/content/my-f5-com/K000123457/index.md",
    )
    metadata = PageMetadata.model_validate(item["metadata"])
    metadata.url, metadata.slug = item["url"], "K000123457"
    article = serialize_document(metadata, body).encode()
    item.update(article_sha256=sha(article), metadata=metadata.model_dump(by_alias=True))
    atomic_private(capture / item["article_path"], article)
    inventory["documents"].append(item)
    atomic_private(capture / "inventory.json", json_bytes(inventory))
    review = tmp_path / "review"
    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", LocalClient)
    prepare_reviews(capture, review)
    first = run_reviews(capture, review, phase="all", journal=tmp_path / "journal", limit=1)
    assert first["pending"] == 1
    sealed = tmp_path / "sealed-first"
    receipt = seal_source(capture, review, sealed)
    pin = sha((sealed / "source-review-receipt.json").read_bytes())
    assert not receipt["review_complete"] and len(receipt["accepted_files"]) == 1
    second = run_reviews(capture, review, phase="all", journal=tmp_path / "journal", limit=1)
    assert second["pending"] == 0
    assert verify_source(sealed, pin) == receipt
    complete = seal_source(capture, review, tmp_path / "sealed-second")
    assert complete["review_complete"] and len(complete["accepted_files"]) == 2
    stream = io.BytesIO()
    pack_private_tree(sealed, stream)
    received = receive_source(
        tmp_path / "ubuntu", "run/my-f5-com", pin, io.BytesIO(stream.getvalue())
    )
    assert verify_source(received, pin) == receipt
    with pytest.raises(ValueError, match="source differs"):
        receive_source(tmp_path / "ubuntu", "run/www-f5-com", pin, io.BytesIO(stream.getvalue()))


def test_snapshot_rejects_symlinked_directory_before_creating_output(
    capture: Path, tmp_path: Path
) -> None:
    review = tmp_path / "review"
    review.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    (capture / "external").symlink_to(private, target_is_directory=True)
    output = tmp_path / "sealed"
    with pytest.raises(ValueError, match="symlinks"):
        seal_source(capture, review, output)
    assert not output.exists()
