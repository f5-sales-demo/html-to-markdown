"""Admission is reproducible from complete requests; changed sources cannot publish."""

import base64
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from html_to_markdown.content_inventory import atomic_private
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.contracts import ResponseEvidence
from html_to_markdown.enrichment.gates import request_hash
from html_to_markdown.enrichment.media import all_frame_inputs
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document, split_document
from html_to_markdown.source_review_jobs import (
    _media_context,
    prepare_reviews,
    run_reviews,
    verify_reviews,
)


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
            if payload["relevance_decision"]["disposition"] == "include":
                assert payload["curation_candidate"]["curation_sha256"]
                assert (
                    payload["curation_candidate"]["source_files_sha256"]
                    == payload["candidate"]["output_sha256"]
                )
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


@pytest.mark.parametrize("approved", [True, False])
def test_independent_exclusion_remains_unpublished_and_requires_every_check(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, approved: bool
) -> None:
    inventory = json.loads((capture / "inventory.json").read_bytes())
    item = inventory["documents"][0]
    metadata = PageMetadata.model_validate(item["metadata"])
    metadata.title = "Lifecycle notice"
    article = serialize_document(
        metadata, "# Lifecycle notice\n\nThis service has retired.\n"
    ).encode()
    source = b"<article><h1>Lifecycle notice</h1><p>This service has retired.</p></article>"
    atomic_private(capture / item["article_path"], article)
    atomic_private(capture / item["source_html_path"], source)
    item.update(
        article_sha256=sha(article),
        source_html_sha256=sha(source),
        metadata=metadata.model_dump(by_alias=True),
    )
    atomic_private(capture / "inventory.json", json_bytes(inventory))

    class ExcludingClient(LocalClient):
        def execute(self, body: dict) -> ResponseEvidence:
            response = super().execute(body)
            raw = response.response
            output = json.loads(raw["output"][0]["content"][0]["text"])
            if body["text"]["format"]["name"] == "relevancedecision":
                output.update(disposition="exclude", reason="complete source is a lifecycle notice")
            elif body["text"]["format"]["name"] == "admissionvalidation":
                payload = json.loads(body["input"][0]["content"][0]["text"])
                assert payload["relevance_decision"]["disposition"] == "exclude"
                output.update(
                    approved=approved,
                    privacy_valid=approved,
                    failures=[] if approved else ["unresolved privacy finding"],
                )
            raw["output"][0]["content"][0]["text"] = json.dumps(output)
            return response.model_copy(
                update={
                    "response_sha256": sha(json.dumps(raw, sort_keys=True, separators=(",", ":")))
                }
            )

    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", ExcludingClient)
    output = tmp_path / "review"
    prepare_reviews(capture, output)
    report = run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    assert report["documents"][0]["disposition"] == ("policy_excluded" if approved else "queued")
    assert bool(report["exceptions"]) is not approved
    assert not verify_reviews(capture, output)["accepted_files"]
    assert not (output / "reviewed").exists()


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


def test_admission_validator_sees_curation_but_source_receipt_retains_privacy_input(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = json.loads((capture / "inventory.json").read_bytes())
    item = inventory["documents"][0]
    metadata = PageMetadata.model_validate(item["metadata"])
    body = (
        "# Configure service\n\nRun `curl --retry 3` within 30 seconds.\n\n"
        "See https://docs.cloud.f5.com/docs-v2/api/http-loadbalancer#Create.\n\n"
        "| Source field | Value |\n| --- | --- |\n| Optional mapping | |\n"
    )
    article = serialize_document(metadata, body).encode()
    atomic_private(capture / item["article_path"], article)
    item["article_sha256"] = sha(article)
    atomic_private(capture / "inventory.json", json_bytes(inventory))
    observed = []

    class CurationClient(LocalClient):
        def execute(self, body: dict) -> ResponseEvidence:
            if body["text"]["format"]["name"] == "admissionvalidation":
                payload = json.loads(body["input"][0]["content"][0]["text"])
                assert "docs.cloud.f5.com/docs-v2/api" in payload["candidate"]["body"]
                assert "docs.cloud.f5.com/docs-v2/api" not in payload["curation_candidate"]["body"]
                assert "| Optional mapping | |" in payload["curation_candidate"]["body"]
                observed.append(payload["curation_candidate"]["curation_sha256"])
            return super().execute(body)

    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", CurationClient)
    output = tmp_path / "review"
    prepare_reviews(capture, output)
    report = run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    assert not report["exceptions"] and len(observed) == 1
    reviewed = output / "reviewed/content/my-f5-com/K000123456/index.md"
    assert reviewed.read_bytes() == article
    record = json.loads(next((output / "documents").glob("*.json")).read_bytes())
    assert record["curation_sha256"] == observed[0]
    verify_reviews(capture, output)


def test_independent_retirement_failure_creates_curation_queue_without_publishing(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RetirementClient(LocalClient):
        def execute(self, body: dict) -> ResponseEvidence:
            response = super().execute(body)
            if body["text"]["format"]["name"] == "admissionvalidation":
                raw = response.response
                decision = json.loads(raw["output"][0]["content"][0]["text"])
                decision.update(
                    approved=False, retirement_valid=False, failures=["retired workflow remains"]
                )
                raw["output"][0]["content"][0]["text"] = json.dumps(decision)
                return response.model_copy(
                    update={
                        "response_sha256": sha(
                            json.dumps(raw, sort_keys=True, separators=(",", ":"))
                        )
                    }
                )
            return response

    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", RetirementClient)
    output = tmp_path / "review"
    prepare_reviews(capture, output)
    report = run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    assert report["documents"][0]["disposition"] == "queued"
    assert report["documents"][0]["reason"] == "source_curation_requires_review"
    assert (output / "curation-exceptions.json").is_file()
    assert not verify_reviews(capture, output)["accepted_files"]


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


@pytest.mark.parametrize("mutation", ["encoding", "pixel", "crop", "missing", "timing"])
def test_native_frame_replay_ignores_lossless_encoding_and_rejects_changed_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    source = tmp_path / "capture"
    output = tmp_path / "review"
    raw = io.BytesIO()
    Image.new("RGB", (30, 20), "blue").save(raw, format="PNG")
    path = source / "image.png"
    atomic_private(path, raw.getvalue())
    item = {"media": [{"path": "image.png", "sha256": sha(raw.getvalue())}]}
    monkeypatch.setattr(
        "html_to_markdown.source_review_jobs.all_frame_inputs",
        lambda path, **kwargs: all_frame_inputs(path, perform_ocr=False),
    )
    _media_context(source, output, item)
    cache = next((output / "media").glob("*.json"))
    frames = json.loads(cache.read_bytes())
    if mutation == "missing":
        frames = []
    elif mutation == "timing":
        frames[0][1]["duration"] = 100
    else:
        with Image.open(io.BytesIO(base64.b64decode(frames[0][0].split(",", 1)[1]))) as original:
            image = original.copy()
        if mutation == "pixel":
            image.putpixel((0, 0), (255, 0, 0))
        if mutation == "crop":
            image = image.crop((0, 0, 10, 10))
        data = io.BytesIO()
        image.save(data, format="PNG", compress_level=0)
        frames[0][0] = "data:image/png;base64," + base64.b64encode(data.getvalue()).decode()
        frames[0][1]["visual_input_sha256"] = sha(data.getvalue())
    cache.write_bytes(json_bytes(frames))
    if mutation == "encoding":
        _media_context(source, output, item)
    else:
        with pytest.raises(ValueError, match="cached"):
            _media_context(source, output, item)


def test_batches_prioritize_published_articles_and_keep_semantic_exceptions_visible(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = json.loads((capture / "inventory.json").read_bytes())
    original = inventory["documents"][0]
    _, body = split_document((capture / original["article_path"]).read_text())
    for number in (123457, 123458):
        item = json.loads(json.dumps(original))
        identifier = "K000" + str(number)
        item.update(
            identity="my-f5-com:" + identifier,
            url="https://my.f5.com/manage/s/article/" + identifier,
            article_path="articles/content/my-f5-com/" + identifier + "/index.md",
        )
        meta = PageMetadata.model_validate(item["metadata"])
        meta.url = item["url"]
        meta.slug = identifier
        data = serialize_document(meta, body).encode()
        item.update(article_sha256=sha(data), metadata=meta.model_dump(by_alias=True))
        atomic_private(capture / item["article_path"], data)
        if number == 123458:
            item["origins"] = ["previous_publication"]
        inventory["documents"].append(item)
    (capture / "inventory.json").write_text(json.dumps(inventory))
    output = tmp_path / "review"
    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", LocalClient)
    prepare_reviews(capture, output)
    first = run_reviews(capture, output, phase="all", journal=tmp_path / "journal", limit=1)
    assert first["processed"] == 1 and first["pending"] == 2
    assert next(d for d in first["documents"] if d["disposition"] == "accepted")["url"].endswith(
        "K000123458"
    )
    monkeypatch.setattr(LocalClient, "reject", True)
    second = run_reviews(capture, output, phase="all", journal=tmp_path / "journal", limit=1)
    assert len(second["exceptions"]) == 1 and second["pending"] == 1
    monkeypatch.setattr(LocalClient, "reject", False)
    third = run_reviews(capture, output, phase="all", journal=tmp_path / "journal", limit=1)
    assert third["processed"] == 1 and third["pending"] == 0 and len(third["exceptions"]) == 1
    assert (
        run_reviews(capture, output, phase="all", journal=tmp_path / "journal", limit=1)[
            "processed"
        ]
        == 0
    )
    assert len(verify_reviews(capture, output)["accepted_files"]) == 2


def test_invalid_privacy_proposal_gets_precise_feedback_before_independent_validation(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = []

    class CorrectingClient(LocalClient):
        def execute(self, body: dict) -> ResponseEvidence:
            response = super().execute(body)
            if body["text"]["format"]["name"] == "privacydecision":
                payload = json.loads(body["input"][0]["content"][0]["text"])
                requests.append(payload)
                assert body["text"]["format"]["schema"]["properties"]["findings"]["maxItems"] == 0
                if not payload["validation_feedback"]:
                    raw = response.response
                    data = json.loads(raw["output"][0]["content"][0]["text"])
                    data["findings"] = [
                        {
                            "finding_id": "unknown",
                            "disposition": "documentation_example",
                            "reason": "bad proposal",
                        }
                    ]
                    raw["output"][0]["content"][0]["text"] = json.dumps(data)
                    response = response.model_copy(
                        update={
                            "response_sha256": sha(
                                json.dumps(raw, sort_keys=True, separators=(",", ":"))
                            )
                        }
                    )
            return response

    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", CorrectingClient)
    output = tmp_path / "review"
    prepare_reviews(capture, output)
    report = run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    assert not report["exceptions"] and len(requests) == 2
    assert "classify every finding" in requests[1]["validation_feedback"][0]
    assert len(verify_reviews(capture, output)["accepted_files"]) == 1


@pytest.mark.parametrize("transient", [False, True])
def test_only_transient_model_failures_are_retried_in_later_batches(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transient: bool
) -> None:
    calls = []

    class FailingClient(LocalClient):
        def execute(self, body: dict) -> ResponseEvidence:
            calls.append(body)
            response = super().execute(body)
            if len(calls) == 1:
                response = response.model_copy(
                    update={"error": "network timeout" if transient else "invalid response schema"}
                )
            return response

    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", FailingClient)
    output = tmp_path / "review"
    prepare_reviews(capture, output)
    first = run_reviews(capture, output, phase="all", journal=tmp_path / "journal", limit=1)
    assert len(first["exceptions"]) == 1 and first["pending"] == int(transient)
    second = run_reviews(capture, output, phase="all", journal=tmp_path / "journal", limit=1)
    if transient:
        assert not second["exceptions"] and second["documents"][0]["disposition"] == "accepted"
        assert len(calls) == 4
    else:
        assert len(second["exceptions"]) == 1 and second["processed"] == 0 and len(calls) == 1


def test_independent_privacy_rejection_returns_precise_feedback_to_editor(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feedback = []
    validations = []

    class CorrectingValidatorClient(LocalClient):
        def execute(self, body: dict) -> ResponseEvidence:
            response = super().execute(body)
            phase = body["text"]["format"]["name"]
            if phase == "privacydecision":
                payload = json.loads(body["input"][0]["content"][0]["text"])
                feedback.append(payload["validation_feedback"])
            elif phase == "admissionvalidation":
                validations.append(body)
                if len(validations) == 1:
                    raw = response.response
                    output = json.loads(raw["output"][0]["content"][0]["text"])
                    output.update(
                        approved=False,
                        privacy_valid=False,
                        failures=["privacy classification does not cover the complete source"],
                    )
                    raw["output"][0]["content"][0]["text"] = json.dumps(output)
                    response = response.model_copy(
                        update={
                            "response_sha256": sha(
                                json.dumps(raw, sort_keys=True, separators=(",", ":"))
                            )
                        }
                    )
            return response

    monkeypatch.setattr(
        "html_to_markdown.source_review_jobs.CodexClient", CorrectingValidatorClient
    )
    output = tmp_path / "review"
    prepare_reviews(capture, output)
    report = run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    assert not report["exceptions"] and len(validations) == 2 and len(feedback) == 2
    assert "privacy classification does not cover the complete source" in feedback[1][0]
    assert len(verify_reviews(capture, output)["accepted_files"]) == 1


def test_document_replay_does_not_copy_unrelated_media_analysis(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "review"
    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", LocalClient)
    prepare_reviews(capture, output)
    run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    atomic_private(output / "media/unrelated-native-2.json", b"unrelated private analysis")

    def bulk_copy(*args: object, **kwargs: object) -> None:
        pytest.fail("document replay must not copy the full corpus media cache")

    monkeypatch.setattr("shutil.copytree", bulk_copy)
    assert len(verify_reviews(capture, output)["accepted_files"]) == 1
