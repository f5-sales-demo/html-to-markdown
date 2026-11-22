"""Pinned OCR must replay independently of mutable caches without losing native checks."""

import io
import json

import pytest
from PIL import Image
from test_baseline_source_review import Client

from html_to_markdown import baseline_late_validation as late
from html_to_markdown import baseline_source_review as source
from html_to_markdown.baseline_native_context import native_context
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.gates import request_hash
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document


@pytest.fixture
def native_prepared(tmp_path, monkeypatch):
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
    checked = source.review_baseline_privacy(output, record, Client())
    return output, record, checked


def test_complete_original_ocr_replay_ignores_changed_shared_cache(native_prepared):
    output, record, saved = native_prepared
    capture = output / saved["source"]
    expected = native_context(capture, output, saved)
    for cache in (output / "media").glob("*.json"):
        frames = json.loads(cache.read_bytes())
        for frame in frames:
            frame[1]["text"] = "A different OCR transcription."
        cache.write_bytes(json_bytes(frames))
    assert native_context(capture, output, saved) == expected
    original = record.read_bytes()
    client = Client()
    result = late.validate_existing_baseline(output, record, client)
    assert result["status"] == "privacy_only_validated"
    assert result["phases"] == saved["phases"]
    assert result["privacy_feedback"] == saved["privacy_feedback"]
    assert [call["text"]["format"]["name"] for call in client.calls] == ["admissionvalidation"]
    assert (output / "history" / (sha(original) + ".json")).read_bytes() == original


def test_original_ocr_replay_requires_no_shared_cache_files(native_prepared):
    output, _, saved = native_prepared
    capture = output / saved["source"]
    before = native_context(capture, output, saved)
    for cache in (output / "media").glob("*.json"):
        cache.unlink()
    assert native_context(capture, output, saved) == before
    assert not list((output / "media").glob("*.json"))


@pytest.mark.parametrize(
    "damage", ["source_bytes", "request", "missing_image", "missing_frame", "unsafe_path"]
)
def test_original_ocr_does_not_bypass_source_or_native_evidence(native_prepared, damage):
    output, _, saved = native_prepared
    saved = json.loads(json_bytes(saved))
    capture = output / saved["source"]
    if damage == "source_bytes":
        path = capture / saved["item"]["media"][0]["path"]
        path.write_bytes(path.read_bytes() + b" altered")
    elif damage == "request":
        saved["phases"]["relevance"]["request_sha256"] = "b" * 64
    elif damage == "unsafe_path":
        saved["item"]["media"][0]["path"] = "../outside.png"
    else:
        prior = saved["phases"]["relevance"]
        if damage == "missing_image":
            prior["request"]["input"][0]["content"] = [
                item
                for item in prior["request"]["input"][0]["content"]
                if item["type"] != "input_image"
            ]
        else:
            text = prior["request"]["input"][0]["content"][0]
            context = json.loads(text["text"])
            next(iter(context["media"].values()))["frames"] = []
            text["text"] = json.dumps(context)
        # This exercises frame guards in addition to the external receipt guard.

        prior["request_sha256"] = request_hash(prior["request"])
    with pytest.raises(ValueError):
        native_context(capture, output, saved)


def test_ocr_replay_keeps_independent_privacy_validation(native_prepared):
    output, record, saved = native_prepared
    original = record.read_bytes()
    with pytest.raises(ValueError, match="independent"):
        late.validate_existing_baseline(output, record, Client(reject=True))
    assert record.read_bytes() == original
    assert saved["retirement_review_required"]
