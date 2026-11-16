"""Interruptions and changed review context cannot skip a private admission return."""

import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from test_source_review_jobs import LocalClient

from html_to_markdown.content_inventory import atomic_private
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.mac_worker import worker_once
from html_to_markdown.models import PageMetadata
from html_to_markdown.policy_transport import installed_policy_pins
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
        if "export-policy-runtime" in args:
            return SimpleNamespace(
                stdout=json_bytes({"state": "policy_current", **installed_policy_pins()})
            )
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


def _source_capture(capture: Path, root: Path, source: str) -> Path:
    """Build complete source-specific fixture bytes for the real admission replay."""
    inventory = json.loads((capture / "inventory.json").read_bytes())
    item = inventory["documents"][0]
    metadata, body = split_document((capture / item["article_path"]).read_text())
    metadata.update(sourceId=source, url="https://" + source.replace("-", ".") + "/article")
    article = serialize_document(PageMetadata.model_validate(metadata), body).encode()
    item.update(
        identity=source + ":article",
        sourceId=source,
        url=metadata["url"],
        article_path="articles/content/" + source + "/article/index.md",
        article_sha256=sha(article),
        metadata=metadata,
    )
    inventory.update(binding={"source": source}, ended_at="2026-11-10T00:03:00Z")
    inventory["completeness"]["capture"] = True
    atomic_private(
        root / item["source_html_path"], (capture / item["source_html_path"]).read_bytes()
    )
    atomic_private(root / item["article_path"], article)
    atomic_private(root / "inventory.json", json_bytes(inventory))
    return root


@pytest.mark.parametrize("failure_kind", ["connect", "timeout", "status-503", "status-401"])
def test_curation_http_failure_returns_admissions_and_advances_other_source(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_kind: str
) -> None:
    """A real HTTP exception cannot prevent independently verified source returns."""
    sources = ["my-f5-com", "www-f5-com"]
    captures = {
        source: _source_capture(capture, tmp_path / ("source-" + source), source)
        for source in sources
    }
    monkeypatch.setattr("html_to_markdown.mac_worker.platform.system", lambda: "Darwin")
    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", LocalClient)
    returned = []
    attempted = []
    diagnostic = "private curation diagnostic"
    request = httpx.Request("GET", "https://example.invalid/revision.json")
    if failure_kind == "connect":
        failure = httpx.ConnectError(diagnostic, request=request)
    elif failure_kind == "timeout":
        failure = httpx.ReadTimeout(diagnostic, request=request)
    else:
        response = httpx.Response(int(failure_kind.split("-")[1]), request=request)
        failure = httpx.HTTPStatusError(diagnostic, request=request, response=response)

    def renew(source_capture: Path, *args: Any) -> dict:
        source = json.loads((source_capture / "inventory.json").read_bytes())["binding"]["source"]
        attempted.append(source)
        if source == sources[0]:
            raise failure
        return {"state": "no_curation_exceptions"}

    def run(args: list[str], **kwargs: Any):
        if "export-policy-runtime" in args:
            return SimpleNamespace(
                stdout=json_bytes({"state": "policy_current", **installed_policy_pins()})
            )
        if "list-review-queues" in args:
            return SimpleNamespace(
                stdout=json_bytes(
                    [
                        {
                            "queue": "run/" + source,
                            "source": source,
                            "fingerprint": sha(source),
                            "captured": 1,
                            "discovered": 1,
                        }
                        for source in sources
                    ]
                )
            )
        if "export-review-queue" in args:
            source = args[args.index("--queue") + 1].split("/")[1]
            pack_private_tree(captures[source], kwargs["stdout"])
        elif "receive-review-input" in args:
            queue = args[args.index("--queue") + 1]
            pin = args[args.index("--review-sha256") + 1]
            received = receive_source(
                tmp_path / "ubuntu", queue, pin, io.BytesIO(kwargs["stdin"].read())
            )
            receipt = verify_source(received, pin)
            assert receipt["review_complete"] and len(receipt["accepted_files"]) == 1
            returned.append(queue)
        else:
            return SimpleNamespace(stdout=b'{"state":"waiting_for_software"}')
        return SimpleNamespace(stdout=b"{}")

    monkeypatch.setattr("html_to_markdown.mac_worker.advance_renewals", renew)
    monkeypatch.setattr("html_to_markdown.mac_worker.subprocess.run", run)
    output = tmp_path / "worker"
    status = worker_once(
        host="worker.example.invalid",
        remote_executable="/private/cli",
        remote_queue="/private/queue",
        output=output,
        transport=Path("/private/transport"),
    )
    assert status["state"] == "idle" and attempted == sources
    assert returned == ["run/" + source for source in sources]
    assert status["queues"][0]["curation"] == {
        "state": "queued",
        "error_class": type(failure).__name__,
    }
    assert status["queues"][1]["curation"] == {"state": "no_curation_exceptions"}
    error = json.loads((output / "run/my-f5-com/curation-renewal-error.json").read_bytes())
    assert error["error"] == diagnostic
    assert diagnostic not in json.dumps(status)
    for source in sources:
        checkpoint = json.loads((output / "run" / source / "checkpoint.json").read_bytes())
        assert checkpoint["completed"] and checkpoint["returned_admission_sha256"]
