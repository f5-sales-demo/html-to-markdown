"""Real source replay remains mandatory while local payload delivery shares storage."""

import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from test_source_review_jobs import LocalClient

import html_to_markdown.source_delivery as delivery
from html_to_markdown.content_inventory import atomic_private
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.mac_worker import _return_source
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document, split_document
from html_to_markdown.source_inputs import seal_source, verify_source
from html_to_markdown.source_review_jobs import run_reviews
from html_to_markdown.work_queue import pack_private_tree, receive_source


@pytest.fixture
def source_batches(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[Path, str]]:
    inventory = json.loads((capture / "inventory.json").read_bytes())
    first = inventory["documents"][0]
    payload = b"large captured evidence " * (1024 * 128)
    html_path = "captures/" + sha(payload) + ".html"
    atomic_private(capture / html_path, payload)
    first.update(source_html_path=html_path, source_html_sha256=sha(payload))
    second = json.loads(json.dumps(first))
    _, body = split_document((capture / first["article_path"]).read_text())
    metadata = PageMetadata.model_validate(second["metadata"])
    metadata.url = "https://my.f5.com/manage/s/article/K000123457"
    metadata.slug = "K000123457"
    article = serialize_document(metadata, body).encode()
    second.update(
        identity="my-f5-com:K000123457",
        url=metadata.url,
        article_path="articles/content/my-f5-com/K000123457/index.md",
        article_sha256=sha(article),
        metadata=metadata.model_dump(by_alias=True),
    )
    atomic_private(capture / second["article_path"], article)
    inventory.update(binding={"source": "my-f5-com"}, ended_at="2026-11-10T00:03:00Z")
    inventory["completeness"]["capture"] = True
    inventory["documents"].append(second)
    atomic_private(capture / "inventory.json", json_bytes(inventory))
    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", LocalClient)
    review = tmp_path / "review"
    run_reviews(capture, review, phase="prepare", journal=tmp_path / "journal")
    run_reviews(capture, review, phase="all", limit=1, journal=tmp_path / "journal")
    early = tmp_path / "source-first"
    seal_source(capture, review, early)
    run_reviews(capture, review, phase="all", limit=1, journal=tmp_path / "journal")
    complete = tmp_path / "source-complete"
    seal_source(capture, review, complete)
    return [
        (root, sha((root / "source-review-receipt.json").read_bytes()))
        for root in [early, complete]
    ]


def test_two_real_batches_share_complete_capture_bytes_and_preserve_old_replay(
    source_batches: list[tuple[Path, str]], tmp_path: Path
) -> None:
    root = tmp_path / "queue"
    outputs: list[Path] = []
    for source, pin in source_batches:
        received = delivery.receive_source_tree(root, "run/my-f5-com", pin, source)
        assert verify_source(received, pin) == verify_source(source, pin)
        for path in source.rglob("*"):
            if path.is_file():
                target = received / path.relative_to(source)
                assert target.read_bytes() == path.read_bytes()
                assert target.stat().st_ino == path.stat().st_ino
                assert target.stat().st_mode == path.stat().st_mode
        outputs.append(received)
    first, complete = outputs[0], outputs[1]
    assert not verify_source(first, source_batches[0][1])["review_complete"]
    assert verify_source(complete, source_batches[1][1])["review_complete"]
    payloads = [next((path / ".admission/capture/captures").glob("*.html")) for path in outputs]
    assert payloads[0].stat().st_ino == payloads[1].stat().st_ino
    assert first != complete and not list(root.rglob("*.tar.gz"))
    assert not list(first.parent.glob(".receiving-source-*"))


def test_interrupted_local_clone_resumes_same_transaction_and_keeps_original_bytes(
    source_batches: list[tuple[Path, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, pin = source_batches[0]
    root = tmp_path / "queue"
    original = os.link
    calls: list[tuple[str, str]] = []

    def fail_once(origin: str, target: str, **kwargs: Any) -> None:
        calls.append((str(origin), str(target)))
        if len(calls) == 3:
            raise OSError("simulated interrupted filesystem operation")
        original(origin, target, **kwargs)

    monkeypatch.setattr("html_to_markdown.source_delivery.os.link", fail_once)
    with pytest.raises(OSError):
        delivery.receive_source_tree(root, "run/my-f5-com", pin, source)
    parent = root / ".reviewed/run/my-f5-com"
    assert not (parent / pin).exists()
    staging = next(parent.glob(".receiving-source-*"))
    assert json.loads((staging / "transfer.json").read_bytes())["state"] == "failed"
    assert verify_source(source, pin)
    received = delivery.receive_source_tree(root, "run/my-f5-com", pin, source)
    assert verify_source(received, pin) == verify_source(source, pin)
    assert len(calls) == len([p for p in source.rglob("*") if p.is_file()]) + 1
    assert not staging.exists()


@pytest.mark.parametrize("transport", ["local", "archive"])
def test_concurrent_identical_receives_are_verified_and_idempotent(
    source_batches: list[tuple[Path, str]], tmp_path: Path, transport: str
) -> None:
    source, pin = source_batches[0]
    root = tmp_path / "queue"
    stream = io.BytesIO()
    pack_private_tree(source, stream)

    def receive_other() -> Path:
        if transport == "local":
            return delivery.receive_source_tree(root, "run/my-f5-com", pin, source)
        return receive_source(root, "run/my-f5-com", pin, io.BytesIO(stream.getvalue()))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(delivery.receive_source_tree, root, "run/my-f5-com", pin, source)
        second = pool.submit(receive_other)
        assert first.result() == second.result()
    assert verify_source(first.result(), pin) == verify_source(source, pin)
    assert delivery.receive_source_tree(root, "run/my-f5-com", pin, source) == first.result()


@pytest.mark.parametrize("unsafe", ["source-link", "source-parent", "destination-link", "fifo"])
def test_unsafe_source_or_destination_cannot_receive_admission(
    source_batches: list[tuple[Path, str]], tmp_path: Path, unsafe: str
) -> None:
    source, pin = source_batches[0]
    root = tmp_path / "queue"
    if unsafe == "source-link":
        (source / "outside").symlink_to(tmp_path / "private-secret")
    elif unsafe == "source-parent":
        alias = tmp_path / "alias"
        alias.symlink_to(source.parent, target_is_directory=True)
        source = alias / source.name
    elif unsafe == "destination-link":
        parent = root / ".reviewed/run"
        parent.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (parent / "my-f5-com").symlink_to(outside, target_is_directory=True)
    else:
        os.mkfifo(source / "unsafe-pipe")
    with pytest.raises(ValueError):
        delivery.receive_source_tree(root, "run/my-f5-com", pin, source)
    assert not (root / ".reviewed/run/my-f5-com" / pin).exists()


def test_tampered_input_is_rejected_before_payload_promotion(
    source_batches: list[tuple[Path, str]], tmp_path: Path
) -> None:
    source, pin = source_batches[0]
    path = next((source / "content").rglob("*.md"))
    path.write_text("tampered source")
    root = tmp_path / "queue"
    with pytest.raises(ValueError):
        delivery.receive_source_tree(root, "run/my-f5-com", pin, source)
    assert not (root / ".reviewed/run/my-f5-com" / pin).exists()


def test_failed_final_replay_returns_candidate_to_private_staging(
    source_batches: list[tuple[Path, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, pin = source_batches[0]
    root = tmp_path / "queue"
    destination = root / ".reviewed/run/my-f5-com" / pin
    original = verify_source

    def fail_promoted(directory: Path, digest: str) -> dict[str, Any]:
        if directory == destination:
            raise ValueError("final independent replay failed")
        return original(directory, digest)

    monkeypatch.setattr(delivery, "verify_source", fail_promoted)
    with pytest.raises(ValueError, match="final independent replay"):
        delivery.receive_source_tree(root, "run/my-f5-com", pin, source)
    assert not destination.exists()
    assert list(destination.parent.glob(".receiving-source-*/source"))
    assert verify_source(source, pin)
    monkeypatch.setattr(delivery, "verify_source", original)
    assert delivery.receive_source_tree(root, "run/my-f5-com", pin, source) == destination
    assert list(destination.parent.glob(".failed-source-*/source"))


def test_worker_returns_local_admission_without_archive_or_ssh(
    source_batches: list[tuple[Path, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, pin = source_batches[0]
    root = tmp_path / "queue"

    def unexpected(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("local source admission must not serialize or transfer an archive")

    monkeypatch.setattr("html_to_markdown.mac_worker.pack_private_tree", unexpected)
    monkeypatch.setattr("html_to_markdown.mac_worker.subprocess.run", unexpected)
    monkeypatch.setattr("html_to_markdown.mac_worker.platform.system", lambda: "Linux")
    _return_source(
        Path("/transport"), "local", "/executable", str(root), "run/my-f5-com", source, pin
    )
    assert verify_source(root / ".reviewed/run/my-f5-com" / pin, pin) == verify_source(source, pin)


def test_unjournaled_interruption_is_preserved_and_restarted_safely(
    source_batches: list[tuple[Path, str]], tmp_path: Path
) -> None:
    source, pin = source_batches[0]
    root = tmp_path / "queue"
    parent = root / ".reviewed/run/my-f5-com"
    staging = parent / (".receiving-source-" + pin)
    staging.mkdir(parents=True)
    (staging / "original-attempt.bin").write_bytes(b"preserved incomplete transfer")
    received = delivery.receive_source_tree(root, "run/my-f5-com", pin, source)
    assert verify_source(received, pin) == verify_source(source, pin)
    saved = next(parent.glob(".failed-source-*/original-attempt.bin"))
    assert saved.read_bytes() == b"preserved incomplete transfer"


@pytest.mark.parametrize(
    "identity",
    ["../my-f5-com", "/run/my-f5-com", "run/my-f5-com/extra", "run//my-f5-com", "run/unknown"],
)
def test_received_queue_identity_is_closed_before_directory_creation(
    tmp_path: Path, identity: str
) -> None:
    root = tmp_path / "queue"
    with pytest.raises(ValueError, match="queue identity"):
        delivery.source_destination(root, identity, "a" * 64)
    assert not root.exists()
