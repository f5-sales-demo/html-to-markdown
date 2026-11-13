"""Whole-corpus dependency closure requires exact complete baseline and topic evidence."""

import io
import json
from copy import deepcopy

import pytest
from PIL import Image

from html_to_markdown.curation import (
    CurationPolicy,
    curation_validation_errors,
    json_bytes,
    load_curation_policy,
    validate_curation,
)
from html_to_markdown.curation_renewal_dependencies import prepare_dependencies, review_dependencies
from html_to_markdown.curation_renewal_policy import DependencyReviewRequired
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document


def files():
    pixels = io.BytesIO()
    Image.new("RGB", (16, 16), "blue").save(pixels, format="PNG")
    pin = sha(pixels.getvalue())
    metadata = PageMetadata(
        sourceId="my-f5-com",
        title="Cloud services",
        slug="cloud-services",
        category="knowledge",
        url="https://my.f5.com/manage/s/article/K000123456",
    )
    article = "content/my-f5-com/K000123456/index.md"
    asset = "content/my-f5-com/K000123456/assets/" + pin + ".png"
    body = "# Current cloud services\n\nUse port 8443.\n\n![Diagram](assets/" + pin + ".png)\n"
    return article, {article: serialize_document(metadata, body).encode(), asset: pixels.getvalue()}


def test_baseline_dependencies_bind_complete_inputs_but_require_privacy_before_curation(
    tmp_path, monkeypatch
):
    article, baseline = files()
    monkeypatch.setattr(
        "html_to_markdown.baseline_source_review._verified_baseline", lambda *a: baseline
    )
    (tmp_path / "publication.json").write_text(json.dumps({"published_at": "2026-11-10T19:49:38Z"}))
    out = tmp_path / "review"
    inv = prepare_dependencies(tmp_path, "a" * 64, [article], out)
    item = inv["documents"][0]
    assert "context_path" not in item and inv["source_privacy_required"]
    record = json.loads((out / item["source_privacy_path"]).read_bytes())
    assert record["binding"]["input_kind"] == "retained_verified_baseline"
    assert record["binding"]["baseline_receipt_sha256"] == "a" * 64
    inputs = out / "dependency-privacy" / record["source"]
    body = (inputs / record["item"]["article_path"]).read_text()
    assert "Use port 8443" in body and len(record["item"]["media"]) == 1
    assert not record["fresh_capture"]
    assert inv == prepare_dependencies(tmp_path, "a" * 64, [article], out)
    other = prepare_dependencies(tmp_path, "b" * 64, [article], out)
    record2 = json.loads((out / other["documents"][0]["source_privacy_path"]).read_bytes())
    assert record2["binding_sha256"] != record["binding_sha256"]


@pytest.mark.parametrize(
    "article", ["../content/other/index.md", "/content/other/index.md", "content/unknown/index.md"]
)
def test_dependencies_cannot_use_unverified_or_unsafe_baseline_paths(
    tmp_path, monkeypatch, article
):
    _, baseline = files()
    monkeypatch.setattr(
        "html_to_markdown.baseline_source_review._verified_baseline", lambda *a: baseline
    )
    with pytest.raises(ValueError, match="verified baseline"):
        prepare_dependencies(tmp_path, "a" * 64, [article], tmp_path / "out")


def test_dependencies_reject_unavailable_media(tmp_path, monkeypatch):
    article, baseline = files()
    (tmp_path / "publication.json").write_text(json.dumps({"published_at": "2026-11-10T19:49:38Z"}))
    baseline = {article: baseline[article]}
    monkeypatch.setattr(
        "html_to_markdown.baseline_source_review._verified_baseline", lambda *a: baseline
    )
    with pytest.raises(ValueError, match="unavailable"):
        prepare_dependencies(tmp_path, "a" * 64, [article], tmp_path / "out")


def test_dependency_enumeration_reports_every_article_without_accepting_a_topic_approval(tmp_path):
    article, baseline = files()
    raw = deepcopy(load_curation_policy().raw)
    topic = next(t for t in raw["topics"] if t["id"] == "terraform-provider-current")
    for identifier in ["K000123456", "K000123457"]:
        for name, data in baseline.items():
            dest = tmp_path / name.replace("K000123456", identifier)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data.replace(b"K000123456", identifier.encode()))
        topic["documents"].append(
            {
                "url": "https://my.f5.com/manage/s/article/" + identifier,
                "disposition": "keep",
                "input_sha256": "f" * 64,
                "reason": "reviewed document",
            }
        )
    topic["baseline_reviewed_urls"] = [
        "https://my.f5.com/manage/s/article/" + identifier
        for identifier in ["K000123456", "K000123457"]
    ]
    policy = CurationPolicy(raw, sha(json_bytes(raw)))
    errors = curation_validation_errors(tmp_path, policy)
    assert len(errors) == 2 and all(
        "unreviewed Terraform media" in reason for reason in errors.values()
    )
    with pytest.raises(ValueError, match="unreviewed Terraform media"):
        validate_curation(tmp_path, policy=policy)


def test_unresolved_dependency_stops_promotion_and_preserves_queue(tmp_path, monkeypatch):
    def fail(*args):
        raise DependencyReviewRequired(["content/my-f5-com/K000123456/index.md"])

    monkeypatch.setattr("html_to_markdown.curation_renewal_dependencies.qualify_policy", fail)
    monkeypatch.setattr(
        "html_to_markdown.curation_renewal_dependencies.prepare_dependencies",
        lambda *a: {"documents": []},
    )
    monkeypatch.setattr(
        "html_to_markdown.curation_renewal_dependencies.review_batch", lambda *a: {"exceptions": 1}
    )
    with pytest.raises(ValueError, match="remain queued"):
        review_dependencies(tmp_path, [], tmp_path, "a" * 64, tmp_path / "journal")
