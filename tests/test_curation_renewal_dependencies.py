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


def test_baseline_dependencies_bind_complete_article_media_receipt_and_policy(
    tmp_path, monkeypatch
):
    article, baseline = files()
    monkeypatch.setattr(
        "html_to_markdown.curation_renewal_dependencies._verified_baseline", lambda *a: baseline
    )
    monkeypatch.setattr(
        "html_to_markdown.curation_renewal_dependencies._media_context",
        lambda source, output, item: ([], {}, {item["media"][0]["sha256"]: item["media"][0]}),
    )
    out = tmp_path / "review"
    inv = prepare_dependencies(tmp_path, "a" * 64, [article], out)
    item = inv["documents"][0]
    context = json.loads((out / item["context_path"]).read_bytes())
    assert context["evidence"]["input_kind"] == "retained_verified_baseline"
    assert context["evidence"]["baseline_receipt_sha256"] == "a" * 64
    assert "Use port 8443" in context["body"] and len(context["media"]) == 1
    preserved = {
        p.relative_to(out / item["binding_sha256"] / "inputs").as_posix(): sha(p.read_bytes())
        for p in (out / item["binding_sha256"] / "inputs").rglob("*")
        if p.is_file()
    }
    assert sha(json_bytes(preserved)) == context["source_files_sha256"]
    assert inv == prepare_dependencies(tmp_path, "a" * 64, [article], out)
    other = prepare_dependencies(tmp_path, "b" * 64, [article], out)
    assert other["documents"][0]["binding_sha256"] != item["binding_sha256"]
    baseline[article] = baseline[article].replace(b"8443", b"9443")
    changed = prepare_dependencies(tmp_path, "a" * 64, [article], out)
    assert changed["documents"][0]["binding_sha256"] != item["binding_sha256"]


@pytest.mark.parametrize(
    "article", ["../content/other/index.md", "/content/other/index.md", "content/unknown/index.md"]
)
def test_dependencies_cannot_use_unverified_or_unsafe_baseline_paths(
    tmp_path, monkeypatch, article
):
    _, baseline = files()
    monkeypatch.setattr(
        "html_to_markdown.curation_renewal_dependencies._verified_baseline", lambda *a: baseline
    )
    with pytest.raises(ValueError, match="verified baseline"):
        prepare_dependencies(tmp_path, "a" * 64, [article], tmp_path / "out")


def test_dependencies_reject_unavailable_media(tmp_path, monkeypatch):
    article, baseline = files()
    baseline = {article: baseline[article]}
    monkeypatch.setattr(
        "html_to_markdown.curation_renewal_dependencies._verified_baseline", lambda *a: baseline
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
