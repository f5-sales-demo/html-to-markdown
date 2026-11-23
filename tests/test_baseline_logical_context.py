"""Logical source projections preserve complete baseline validation inputs."""

import io
import json

import pytest
from PIL import Image
from test_baseline_source_review import Client

from html_to_markdown import baseline_late_validation as late
from html_to_markdown import baseline_source_review as source
from html_to_markdown.curation_renewal import Proposal
from html_to_markdown.enrichment import media
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.contracts import ResponseEvidence
from html_to_markdown.models import PageMetadata
from html_to_markdown.reference_repair_review import source_request
from html_to_markdown.render import serialize_document
from html_to_markdown.source_boundary_review import source_logical_request
from html_to_markdown.source_input_projection import restore_source_payload


@pytest.fixture
def projected_baseline(tmp_path, monkeypatch):
    bitmap = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(bitmap, format="PNG")
    monkeypatch.setattr(media, "render_svg", lambda _raw: bitmap.getvalue())
    raw = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
        + '<path d="'
        + "M 0 0 L 1 1 " * 6000
        + '"/></svg>'
    ).encode()
    digest = sha(raw)
    article = "content/docs-cloud-f5-com/how-to/origin-health/index.md"
    asset = article.removesuffix("index.md") + "assets/" + digest + ".svg"
    metadata = PageMetadata(
        sourceId="docs-cloud-f5-com",
        title="Cloud origin health",
        slug="origin-health",
        category="Guides",
        url="https://docs.cloud.f5.com/docs-v2/how-to/origin-health",
    )
    body = (
        "# Cloud origin health\n\nUse port 8443 and wait 30 seconds.\n\n![Controls](assets/"
        + digest
        + ".svg)\n"
    )
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "publication.json").write_text(json.dumps({"published_at": "2026-11-10T19:49:38Z"}))
    monkeypatch.setattr(
        source,
        "_verified_baseline",
        lambda *args: {article: serialize_document(metadata, body).encode(), asset: raw},
    )
    output = tmp_path / "privacy"
    inventory = source.prepare_baseline_reviews(baseline, "a" * 64, [article], output)
    record = output / inventory["documents"][0]["path"]
    saved = source.review_baseline_privacy(output, record, Client())
    return output, record, saved


def test_complete_projected_baseline_context_replays_original_media(projected_baseline):
    output, record, saved = projected_baseline
    original = record.read_bytes()
    for phase in ("relevance", "privacy"):
        evidence = ResponseEvidence.model_validate(saved["phases"][phase])
        assert evidence.source_boundary["version"] == "source-input-projection/1"
        logical = source_logical_request(evidence)
        assert logical != evidence.request
        before = json.loads(logical["input"][0]["content"][0]["text"])
        physical = json.loads(evidence.request["input"][0]["content"][0]["text"])
        assert before["media"] != physical["media"]
    validator = Client()
    checked = late.validate_existing_baseline(output, record, validator)
    assert checked["status"] == "privacy_only_validated"
    assert checked["phases"] == saved["phases"]
    assert checked["privacy_feedback"] == saved["privacy_feedback"]
    assert [body["text"]["format"]["name"] for body in validator.calls] == ["admissionvalidation"]
    assert (output / "history" / (sha(original) + ".json")).read_bytes() == original


@pytest.mark.parametrize("damage", ["request", "logical", "source"])
def test_projected_context_still_rejects_changed_complete_inputs(projected_baseline, damage):
    output, record, saved = projected_baseline
    if damage == "source":
        path = output / saved["source"] / saved["item"]["article_path"]
        path.write_text(path.read_text().replace("8443", "443"))
    else:
        entry = saved["phases"]["relevance"]
        if damage == "request":
            entry["request"]["input"][0]["content"][0]["text"] += "changed"
        else:
            entry["source_boundary"]["logical_request"]["model"] = "changed-model"
        record.write_text(json.dumps(saved))
    validator = Client()
    with pytest.raises(ValueError):
        late.validate_existing_baseline(output, record, validator)
    assert not validator.calls


def test_reference_repair_consumes_complete_logical_source(projected_baseline):

    _, _, saved = projected_baseline
    evidence = ResponseEvidence.model_validate(saved["phases"]["privacy"])
    logical = source_logical_request(evidence)
    complete = json.loads(logical["input"][0]["content"][0]["text"])
    proposal = Proposal(
        identity=saved["identity"],
        binding_sha256="b" * 64,
        topics=[],
        unresolved=["Preserve the exact media-reference repair"],
    )
    data = {
        "binding_sha256": "b" * 64,
        "current_candidate": {"output_sha256": "c" * 64, "findings": []},
    }
    body = source_request(data, {}, proposal, saved, logical["input"][0]["content"][1:])
    payload = json.loads(body["input"][0]["content"][0]["text"])
    assert restore_source_payload(payload["source"]) == complete
    assert len(body["input"][0]["content"][0]["text"]) < 1048576
    assert "source.fields" in body["instructions"]
    assert len(body["input"][0]["content"]) == len(logical["input"][0]["content"])
