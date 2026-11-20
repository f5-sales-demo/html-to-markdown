"""Retained relevance exclusion needs complete independent proof and governed removal."""

import io
import json

import pytest
from PIL import Image

from html_to_markdown import baseline_source_review as source
from html_to_markdown.baseline_dispositions import replay_exclusion
from html_to_markdown.curation import json_bytes, load_curation_policy
from html_to_markdown.curation_renewal_dependencies import prepare_dependencies
from html_to_markdown.curation_renewal_policy import qualify_policy, reviewed_policy
from html_to_markdown.curation_renewal_publication import validate_planner
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.contracts import ResponseEvidence
from html_to_markdown.enrichment.gates import request_hash
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document


class Client:
    def __init__(self, defect=None, extraction="complete"):
        self.defect, self.extraction, self.calls = defect, extraction, []

    def invalidate(self, response):
        pass

    def execute(self, body):
        self.calls.append(body)
        payload = json.loads(body["input"][0]["content"][0]["text"])
        kind = body["text"]["format"]["name"]
        value = {"identity": payload["identity"], "binding_sha256": payload["binding_sha256"]}
        if kind == "relevancedecision":
            value.update(
                disposition="exclude",
                extraction=self.extraction,
                retirement="independent",
                reason="Only duplicate links and a heading, no substantive standalone article",
                evidence_addresses=[payload["structural_addresses"][0]["address"]],
            )
        else:
            assert kind == "exclusionvalidation"
            assert "RELEVANCE exclusion" in body["instructions"]
            assert payload["published_candidate"]["body"] == ""
            value.update(
                approved=True,
                complete_article=True,
                complete_media=True,
                extraction_valid=True,
                relevance_valid=True,
                safe_provenance=True,
                checked_addresses=[b["address"] for b in payload["structural_addresses"]],
                checked_media_sha256=sorted(payload["media"]),
                checked_frames=[
                    f"media/{digest}/{index}"
                    for digest, media in sorted(payload["media"].items())
                    for index in range(media["frame_count"])
                ],
                reason="Complete independently checked duplicate-link shell exclusion",
                failures=[],
                unresolved=[],
            )
            if self.defect in value:
                value[self.defect] = False
            elif self.defect == "missing_span":
                value["checked_addresses"].pop()
            elif self.defect == "duplicate_span":
                value["checked_addresses"].append(value["checked_addresses"][0])
            elif self.defect == "missing_frame":
                value["checked_frames"].pop()
            elif self.defect == "missing_media":
                value["checked_media_sha256"].pop()
            elif self.defect == "wrong_binding":
                value["binding_sha256"] = "f" * 64
            elif self.defect == "unresolved":
                value["unresolved"] = ["complete article review is uncertain"]
        response = {
            "status": "completed",
            "model": body["model"],
            "output": [{"content": [{"type": "output_text", "text": json.dumps(value)}]}],
        }
        if self.defect == "refusal" and kind == "exclusionvalidation":
            response["output"] = [{"content": [{"type": "refusal", "refusal": "refused"}]}]
        return ResponseEvidence(
            request_sha256=request_hash(body),
            response_sha256=sha(json.dumps(response, sort_keys=True, separators=(",", ":"))),
            model=body["model"],
            reasoning="high",
            request=body,
            response=response,
            prompt_version=body["instructions"].rsplit("Prompt version: ", 1)[-1].strip(),
            error=None,
            transport="codex_litellm",
            runtime_version="test",
        )


@pytest.fixture
def retained(tmp_path, monkeypatch):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "publication.json").write_bytes(
        json_bytes({"published_at": "2026-11-10T19:49:38Z"})
    )
    article = "content/docs-cloud-f5-com/reference/api-ref/index.md"
    metadata = PageMetadata(
        sourceId="docs-cloud-f5-com",
        title="API reference",
        slug="api-ref",
        category="Reference",
        url="https://docs.cloud.f5.com/docs-v2/reference/api-ref",
    )
    data = serialize_document(
        metadata,
        "# API reference\n\n[Catalog](https://f5-sales-demo.github.io/api-specs-enriched/)\n\n[Catalog](https://f5-sales-demo.github.io/api-specs-enriched/)\n",
    ).encode()
    files = {article: data, "manifest.json": json_bytes({"documents": [{"path": article}]})}
    monkeypatch.setattr(source, "_verified_baseline", lambda *a: files)
    monkeypatch.setattr(
        "html_to_markdown.curation_renewal_policy._verified_baseline", lambda *a: files
    )
    output = tmp_path / "renewal"
    inv = prepare_dependencies(baseline, "a" * 64, [article], output)
    record = output / inv["documents"][0]["source_privacy_path"]
    return baseline, output, record, article


def ready(retained, client):
    baseline, output, record, article = retained
    source.review_baseline_privacy(output / "dependency-privacy", record, client)
    inventory = prepare_dependencies(baseline, "a" * 64, [article], output)
    return json.loads((output / inventory["documents"][0]["context_path"]).read_bytes())


def test_complete_exclusion_replays_through_governed_planner_without_retirement_claim(retained):
    baseline, output, record, article = retained
    client = Client()
    before = json.loads(record.read_bytes())
    before["privacy_feedback"] = [
        "first rejected privacy edit",
        "second rejected privacy edit",
        "third rejected privacy edit",
    ]
    record.write_bytes(json_bytes(before))
    context = ready(retained, client)
    saved = json.loads(record.read_bytes())
    assert saved["status"] == "source_exclusion_validated"
    assert saved["privacy_feedback"] == before["privacy_feedback"]
    assert not saved["published_output"] and "privacy" not in saved["phases"]
    result = replay_exclusion(context, output)
    assert result["candidate"]["disposition"] == "remove"
    policy = reviewed_policy(output, [context])
    decision = next(d for t in policy.topics for d in t["documents"] if d.get("review_kind"))
    assert decision["reason"] == "independently_validated_source_relevance_exclusion"
    assert not policy.excludes(context["identity"]), (
        "source relevance must not become a blanket retirement exclusion"
    )
    qualified = qualify_policy(output, [context], baseline, "a" * 64)
    assert qualified["candidates"][article]["disposition"] == "remove"
    validated = validate_planner(output, [context], baseline, "a" * 64, client)
    assert validated["post_planner_validated"]
    assert len(client.calls) == 3
    assert validate_planner(output, [context], baseline, "a" * 64) == validated
    assert load_curation_policy().sha256 != policy.sha256


@pytest.mark.parametrize(
    "defect",
    [
        "approved",
        "complete_article",
        "complete_media",
        "extraction_valid",
        "relevance_valid",
        "safe_provenance",
        "missing_span",
        "duplicate_span",
        "wrong_binding",
        "unresolved",
        "refusal",
    ],
)
def test_failed_or_incomplete_independent_exclusion_stays_queued(retained, defect):
    _, output, record, _ = retained
    with pytest.raises(ValueError):
        source.review_baseline_privacy(output / "dependency-privacy", record, Client(defect))
    assert json.loads(record.read_bytes())["status"] != "source_exclusion_validated"
    assert not (output / "reviewed").exists()


def test_malformed_retained_extraction_cannot_be_excluded_or_invented_complete(retained):
    _, output, record, _ = retained
    client = Client(extraction="defect")
    with pytest.raises(ValueError, match="extraction remains unresolved"):
        source.review_baseline_privacy(output / "dependency-privacy", record, client)
    assert len(client.calls) == 1
    assert json.loads(record.read_bytes())["status"] == "source_review_required"


def test_exclusion_rejects_original_and_validation_tampering(retained):
    _, output, record, _ = retained
    context = ready(retained, Client())
    saved = json.loads(record.read_bytes())
    original = output / "dependency-privacy" / saved["source"] / saved["item"]["article_path"]
    original.write_text(original.read_text() + "\nNew substantive API instructions.\n")
    with pytest.raises(ValueError, match="stale or unsafe"):
        replay_exclusion(context, output)


@pytest.mark.parametrize("defect", [None, "missing_frame", "missing_media"])
def test_whole_source_exclusion_requires_every_animated_media_frame(retained, defect):
    baseline, output, record, article = retained
    files = source._verified_baseline(baseline, "a" * 64)
    stream = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(
        stream,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (16, 16), "blue")],
        duration=[10, 20],
        loop=0,
    )
    data = stream.getvalue()
    reference = "assets/" + sha(data) + ".gif"
    metadata, body = source.split_document(files[article].decode())
    files[article] = serialize_document(
        PageMetadata.model_validate(metadata), body + "\n![Decoration](" + reference + ")\n"
    ).encode()
    files[str(__import__("pathlib").Path(article).parent / reference)] = data
    prepare_dependencies(baseline, "a" * 64, [article], output)
    client = Client(defect)
    if defect:
        with pytest.raises(ValueError, match="exclusion remains unresolved"):
            source.review_baseline_privacy(output / "dependency-privacy", record, client)
    else:
        context = ready(retained, client)
        result = replay_exclusion(context, output)
        assert len(result["preservation"]["checked_frames"]) == 2
        assert len(result["preservation"]["checked_media_sha256"]) == 1
        assert not result["candidate"]["media"]
