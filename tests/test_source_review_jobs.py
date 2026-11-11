"""Admission is reproducible from complete requests; changed sources cannot publish."""

import io
import json
from pathlib import Path

import pytest
from PIL import Image

from html_to_markdown.content_inventory import atomic_private
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.contracts import ResponseEvidence
from html_to_markdown.enrichment.gates import request_hash
from html_to_markdown.enrichment.media import all_frame_inputs
from html_to_markdown.source_review_jobs import prepare_reviews, run_reviews, verify_reviews


class LocalClient:
    reject = False

    def __init__(self, *args: object, **kwargs: object):
        pass

    def close(self) -> None:
        pass

    def execute(self, body: dict) -> ResponseEvidence:
        payload = json.loads(body["input"][0]["content"][0]["text"])
        assert "source_article_html" in payload
        output = {"identity": payload["identity"], "binding_sha256": payload["binding_sha256"]}
        phase = body["text"]["format"]["name"]
        if phase == "relevancedecision":
            output.update(
                disposition="include",
                reason="substantive cloud procedure",
                evidence_addresses=[block["address"] for block in payload["structural_addresses"]],
                extraction="complete",
                retirement="independent",
            )
        elif phase == "privacydecision":
            output.update(findings=[], values=[], text_changes=[], media_changes=[], unresolved=[])
        elif phase == "admissionvalidation":
            assert "candidate" in payload and "body" in payload
            output.update(
                output_sha256=payload["candidate"]["output_sha256"],
                approved=not self.reject,
                relevance_valid=True,
                privacy_valid=True,
                extraction_valid=True,
                technical_relationships_preserved=True,
                media_valid=True,
                retirement_valid=True,
                complete_document_reviewed=True,
                complete_media_reviewed=True,
                approved_safe_findings=[],
                failures=["protected fact lost"] if self.reject else [],
                unresolved=[],
            )
        else:
            raise AssertionError(phase)
        response = {
            "status": "completed",
            "model": body["model"],
            "output": [{"content": [{"type": "output_text", "text": json.dumps(output)}]}],
        }
        return ResponseEvidence(
            request_sha256=request_hash(body),
            response_sha256=sha(json.dumps(response, sort_keys=True, separators=(",", ":"))),
            model=body["model"],
            reasoning="high",
            prompt_version="source-admission/1",
            request=body,
            response=response,
            error=None,
            transport="codex_litellm",
            runtime_version="test",
        )


def test_review_and_offline_replay_preserve_complete_article(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "review"
    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", LocalClient)
    prepare_reviews(capture, output)
    report = run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    assert not report["exceptions"]
    assert report["documents"][0]["disposition"] == "accepted"
    verified = verify_reviews(capture, output)
    assert len(verified["accepted_files"]) == 1
    article = output / "reviewed/content/my-f5-com/K000123456/index.md"
    assert "`curl --retry 3` within 30 seconds" in article.read_text()
    article.write_text("tampered")
    with pytest.raises(ValueError, match="bytes changed"):
        verify_reviews(capture, output)


def test_failed_independent_review_keeps_candidate_queued(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "review"
    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", LocalClient)
    monkeypatch.setattr(LocalClient, "reject", True)
    prepare_reviews(capture, output)
    report = run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    assert report["exceptions"]
    assert report["documents"][0]["disposition"] == "queued"
    assert not (output / "reviewed").exists()


def test_mutated_capture_is_rejected_before_any_model_request(
    capture: Path, tmp_path: Path
) -> None:
    prepare_reviews(capture, tmp_path / "review")
    (capture / "captures/source.html").write_text("changed")
    with pytest.raises(ValueError, match="digest"):
        prepare_reviews(capture, tmp_path / "review")


@pytest.mark.parametrize("change", ["policy", "source_failure", "removed"])
def test_previous_admission_is_preserved_privately_and_queued_after_change(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    output = tmp_path / "review"
    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", LocalClient)
    prepare_reviews(capture, output)
    run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    article = next((output / "reviewed").rglob("*.md"))
    original = article.read_bytes()
    inventory = json.loads((capture / "inventory.json").read_bytes())
    if change == "policy":
        monkeypatch.setattr(
            "html_to_markdown.source_review.review_context_digest", lambda: "changed"
        )
    elif change == "source_failure":
        inventory["documents"][0].update(capture_complete=False, disposition="capture_error")
    else:
        inventory["documents"] = []
    (capture / "inventory.json").write_text(json.dumps(inventory))
    prepared = prepare_reviews(capture, output)
    assert not article.exists()
    assert (output / "history/published" / sha(original)).read_bytes() == original
    assert not verify_reviews(capture, output)["accepted_files"]
    if change == "policy":
        saved = json.loads((output / prepared["documents"][0]["path"]).read_bytes())
        assert saved["disposition"] == "queued" and not saved["phases"]


def test_failed_replay_revokes_accepted_status_without_losing_emitted_bytes(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "review"
    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", LocalClient)
    prepared = prepare_reviews(capture, output)
    run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    record = output / prepared["documents"][0]["path"]
    saved = json.loads(record.read_bytes())
    saved["phases"]["validation"]["error"] = "interrupted"
    record.write_text(json.dumps(saved))
    report = run_reviews(capture, output, phase="replay", journal=tmp_path / "journal")
    assert report["exceptions"] and report["documents"][0]["disposition"] == "queued"
    assert not verify_reviews(capture, output)["accepted_files"]
    assert list((output / "history/published").iterdir())


def test_admission_and_replay_cover_complete_media_and_immutable_extraction(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    output = tmp_path / "review"
    inventory = json.loads((capture / "inventory.json").read_bytes())
    item = inventory["documents"][0]
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), "blue").save(buffer, format="PNG")
    raw = buffer.getvalue()
    media_path = "articles/content/my-f5-com/K000123456/assets/" + sha(raw)
    atomic_private(capture / media_path, raw)
    article = (
        (capture / item["article_path"]).read_bytes().decode()
        + "\n![Diagram](assets/"
        + sha(raw)
        + ")\n"
    )
    item["published_path"] = item["article_path"]
    item["article_path"] = "extractions/" + sha(article) + "/index.md"
    atomic_private(capture / item["article_path"], article.encode())
    item["article_sha256"] = sha(article)
    item["media"] = [
        {
            "path": media_path,
            "sha256": sha(raw),
            "url": "https://cdn.f5.com/image.png",
            "media_type": "image/png",
        }
    ]
    (capture / "inventory.json").write_text(json.dumps(inventory))
    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", LocalClient)
    monkeypatch.setattr(
        "html_to_markdown.source_review_jobs.all_frame_inputs",
        lambda path, **kwargs: all_frame_inputs(path, perform_ocr=False),
    )
    prepare_reviews(capture, output)
    report = run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    assert not report["exceptions"]
    assert len(verify_reviews(capture, output)["accepted_files"]) == 2
    published = output / "reviewed/content/my-f5-com/K000123456/index.md"
    assert (
        output / "reviewed/content/my-f5-com/K000123456/assets" / (sha(raw) + ".png")
    ).read_bytes() == raw
    assert sha(raw) + ".png" in published.read_text()


def test_fresh_unchanged_article_reuses_decisions_but_changed_authored_source_does_not(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "review"
    inventory = json.loads((capture / "inventory.json").read_bytes())
    item = inventory["documents"][0]
    authored = "<main><h1>Configure service</h1><p>Run the service within 30 seconds.</p></main>"

    def recapture(wrapper: str, at: str) -> None:
        raw = wrapper.encode()
        path = "captures/" + sha(raw) + ".html"
        atomic_private(capture / path, raw)
        item.update(source_html_path=path, source_html_sha256=sha(raw), captured_at=at)
        (capture / "inventory.json").write_text(json.dumps(inventory))

    recapture(
        "<html>" + authored + "<footer>Old navigation</footer></html>", "2026-11-10T00:00:00Z"
    )
    calls = []
    execute = LocalClient.execute

    def record_execute(client: LocalClient, body: dict):
        calls.append(body)
        return execute(client, body)

    monkeypatch.setattr(LocalClient, "execute", record_execute)
    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", LocalClient)
    first = prepare_reviews(capture, output)
    run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    assert len(calls) == 3
    recapture(
        "<html><head><script>changed wrapper</script></head>"
        + authored
        + "<footer>New navigation</footer></html>",
        "2026-11-11T00:00:00Z",
    )
    fresh = prepare_reviews(capture, output)
    assert first["documents"][0]["binding_sha256"] == fresh["documents"][0]["binding_sha256"]
    result = run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    assert not result["exceptions"] and len(calls) == 3
    saved = json.loads((output / fresh["documents"][0]["path"]).read_bytes())
    assert saved["item"]["captured_at"] == "2026-11-11T00:00:00Z"
    assert len(verify_reviews(capture, output)["accepted_files"]) == 1
    recapture(authored.replace("30 seconds", "10 seconds"), "2026-11-12T00:00:00Z")
    changed = prepare_reviews(capture, output)
    saved = json.loads((output / changed["documents"][0]["path"]).read_bytes())
    assert saved["disposition"] == "queued" and not saved["phases"]
    assert not verify_reviews(capture, output)["accepted_files"]
