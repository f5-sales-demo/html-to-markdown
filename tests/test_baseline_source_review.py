"""Retained baseline privacy is explicit, independently checked and never publication approval."""

import io
import json

import pytest
from PIL import Image

from html_to_markdown import baseline_source_review as source
from html_to_markdown.curation import json_bytes
from html_to_markdown.curation_renewal_dependencies import prepare_dependencies
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.contracts import ResponseEvidence
from html_to_markdown.enrichment.gates import request_hash
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    metadata = PageMetadata(
        sourceId="docs-cloud-f5-com",
        title="Cloud origin health",
        slug="origin-health",
        category="Guides",
        url="https://docs.cloud.f5.com/docs-v2/how-to/origin-health",
    )
    article = "content/docs-cloud-f5-com/how-to/origin-health/index.md"
    data = serialize_document(
        metadata,
        "# Cloud origin health\n\nConfigure the origin pool first. Use port 8443 and wait 30 seconds.\n",
    ).encode()
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "publication.json").write_text(json.dumps({"published_at": "2026-11-10T19:49:38Z"}))
    monkeypatch.setattr(source, "_verified_baseline", lambda *args: {article: data})
    output = tmp_path / "privacy"
    inventory = source.prepare_baseline_reviews(baseline, "a" * 64, [article], output)
    return baseline, output, output / inventory["documents"][0]["path"], article


class Client:
    def __init__(self, reject=False, approve_publication=False):
        self.reject = reject
        self.approve_publication = approve_publication
        self.calls = []

    def invalidate(self, response):
        pass

    def execute(self, body):
        self.calls.append(body)
        payload = json.loads(body["input"][0]["content"][0]["text"])
        value = {"identity": payload["identity"], "binding_sha256": payload["binding_sha256"]}
        kind = body["text"]["format"]["name"]
        if kind == "relevancedecision":
            value.update(
                disposition="include",
                extraction="complete",
                retirement="independent",
                reason="Complete relevant archived cloud procedure",
                evidence_addresses=["b:1"],
            )
        elif kind == "privacydecision":
            value.update(findings=[], values=[], text_changes=[], media_changes=[], unresolved=[])
        elif kind == "mediaobservations":
            value.update(observations=[], complete_media_reviewed=True, unresolved=[])
        elif kind == "observationvalidation":
            value.update(
                observations_sha256=payload["observations"]["observations_sha256"],
                approved_indices=[],
                complete_media_reviewed=True,
                failures=[],
                unresolved=[],
            )
        else:
            assert kind == "admissionvalidation"
            assert (
                payload["source_article_html"] == ""
                and payload["binding"]["input_kind"] == "retained_verified_baseline"
            )
            value.update(
                output_sha256=payload["candidate"]["output_sha256"],
                approved=self.approve_publication,
                retirement_valid=self.approve_publication,
                relevance_valid=True,
                privacy_valid=not self.reject,
                extraction_valid=True,
                technical_relationships_preserved=True,
                media_valid=True,
                complete_document_reviewed=True,
                complete_media_reviewed=True,
                approved_safe_findings=[],
                failures=["unsafe candidate"] if self.reject else [],
                unresolved=[],
            )
        response = {
            "status": "completed",
            "model": body["model"],
            "output": [{"content": [{"type": "output_text", "text": json.dumps(value)}]}],
        }
        return ResponseEvidence(
            request_sha256=request_hash(body),
            response_sha256=sha(json.dumps(response, sort_keys=True, separators=(",", ":"))),
            model=body["model"],
            reasoning="high",
            prompt_version=body["instructions"].rsplit("Prompt version: ", 1)[-1].strip(),
            request=body,
            response=response,
            error=None,
            transport="codex_litellm",
            runtime_version="test",
        )


def test_baseline_requires_independent_privacy_and_preserves_source_dates(prepared):
    _, output, record, _ = prepared
    initial = json.loads(record.read_bytes())
    assert initial["status"] == "privacy_review_required" and not initial["fresh_capture"]
    assert initial["source_published_at"] == "2026-11-10T19:49:38Z"
    client = Client()
    result = source.review_baseline_privacy(output, record, client)
    assert result["status"] == "privacy_only_validated" and result["retirement_review_required"]
    assert result["enforcement_findings"] == 0
    assert "Use port 8443 and wait 30 seconds." in result["candidate"]["body"]
    assert len(client.calls) == 3
    before = record.read_bytes()
    assert source.review_baseline_privacy(output, record) == result
    assert record.read_bytes() == before


@pytest.mark.parametrize("case", ["privacy", "publication"])
def test_failed_privacy_or_invented_publication_approval_never_emits_inputs(prepared, case):
    _, output, record, _ = prepared
    with pytest.raises(ValueError, match="exhausted"):
        source.review_baseline_privacy(
            output,
            record,
            Client(reject=case == "privacy", approve_publication=case == "publication"),
        )
    assert not (output / "reviewed").exists()
    assert (
        len(list((output / "attempts" / record.stem).glob("attempt-*/failure.private.json"))) == 3
    )
    assert len(json.loads(record.read_bytes())["privacy_feedback"]) == 3


def test_baseline_cannot_reuse_changed_original_or_frame_inputs(prepared):
    _, output, record, _ = prepared
    saved = json.loads(record.read_bytes())
    path = output / saved["source"] / saved["item"]["article_path"]
    path.write_bytes(path.read_bytes().replace(b"30 seconds", b"60 seconds"))
    with pytest.raises(ValueError, match="stale or unsafe"):
        source.review_baseline_privacy(output, record, Client())
    assert not (output / "reviewed").exists()


def test_dependency_preparation_requires_privacy_evidence_and_rejects_tampering(prepared, tmp_path):
    baseline, _, _, article = prepared
    output = tmp_path / "renewal"
    first = prepare_dependencies(baseline, "a" * 64, [article], output)
    item = first["documents"][0]
    assert "context_path" not in item
    privacy = output / "dependency-privacy"
    record = output / item["source_privacy_path"]
    source.review_baseline_privacy(privacy, record, Client())
    ready = prepare_dependencies(baseline, "a" * 64, [article], output)
    context = json.loads((output / ready["documents"][0]["context_path"]).read_bytes())
    source.verify_baseline_prerequisite(output, context)
    assert "Use port 8443 and wait 30 seconds." in context["body"]
    saved = json.loads(record.read_bytes())
    saved["candidate"]["body"] = saved["candidate"]["body"].replace("30 seconds", "60 seconds")
    record.write_bytes(json_bytes(saved))
    with pytest.raises(ValueError, match="evidence changed"):
        source.verify_baseline_prerequisite(output, context)


def test_native_media_prerequisite_covers_full_frame_and_replays(tmp_path, monkeypatch):
    stream = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(stream, format="PNG")
    raw = stream.getvalue()
    digest = sha(raw)
    article = "content/docs-cloud-f5-com/how-to/origin-health/index.md"
    asset = article.removesuffix("index.md") + "assets/" + digest + ".png"
    meta = PageMetadata(
        sourceId="docs-cloud-f5-com",
        title="Cloud origin health",
        slug="origin-health",
        category="Guides",
        url="https://docs.cloud.f5.com/docs-v2/how-to/origin-health",
    )
    body = (
        "# Cloud origin health\n\nWait 30 seconds.\n\n![Current controls](assets/"
        + digest
        + ".png)\n"
    )
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "publication.json").write_text(json.dumps({"published_at": "2026-11-10T19:49:38Z"}))
    monkeypatch.setattr(
        source,
        "_verified_baseline",
        lambda *a: {article: serialize_document(meta, body).encode(), asset: raw},
    )
    output = tmp_path / "privacy"
    inventory = source.prepare_baseline_reviews(baseline, "a" * 64, [article], output)
    record = output / inventory["documents"][0]["path"]
    client = Client()
    result = source.review_baseline_privacy(output, record, client)
    assert result["candidate"]["media"][digest]["frame_count"] == 1
    assert len(client.calls) == 5
    assert source.review_baseline_privacy(output, record) == result
