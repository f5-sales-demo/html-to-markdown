"""Current full admission resolves damaged retained prerequisites with immutable proofs."""

import json

import pytest
from test_combined_sources import admitted

from html_to_markdown import admitted_baseline_sources as fresh
from html_to_markdown import baseline_source_review as source
from html_to_markdown.curation import json_bytes
from html_to_markdown.curation_renewal_dependencies import prepare_dependencies
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document, split_document


@pytest.fixture
def dependency(capture, tmp_path, monkeypatch):
    inventory_path = capture / "inventory.json"
    inventory = json.loads(inventory_path.read_bytes())
    for item in inventory["documents"]:
        item["captured_at"] = "2026-11-10T00:00:00Z"
    inventory_path.write_bytes(json_bytes(inventory))
    bundle, pin = admitted(capture, tmp_path, monkeypatch)
    receipt = json.loads((bundle / "source-review-receipt.json").read_bytes())
    entry = next(d for d in receipt["documents"] if d["disposition"] == "accepted")
    article = entry["published_path"]
    metadata, body = split_document((bundle / article).read_text())
    original = {
        p.relative_to(bundle).as_posix(): p.read_bytes()
        for p in (bundle / "content").rglob("*")
        if p.is_file()
    }
    original[article] = serialize_document(
        PageMetadata.model_validate(metadata),
        body + "\n```yaml\napiVersion: v1 spec: collapsed: true\n```\n",
    ).encode()
    original["manifest.json"] = json_bytes(
        {
            "documents": [
                {"path": article, "provenance": {"last_success_at": "2026-11-01T00:00:00Z"}}
            ]
        }
    )
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "publication.json").write_bytes(
        json_bytes({"published_at": "2026-11-01T00:00:00Z"})
    )
    monkeypatch.setattr(source, "_verified_baseline", lambda *a: original)
    monkeypatch.setattr(fresh, "_verified_baseline", lambda *a: original)
    output = tmp_path / "renewals"
    before = prepare_dependencies(baseline, "a" * 64, [article], output)
    record = output / before["documents"][0]["source_privacy_path"]
    saved = json.loads(record.read_bytes())
    saved["privacy_feedback"] = ["first rejection", "second rejection", "third rejection"]
    record.write_bytes(json_bytes(saved))
    return baseline, output, article, bundle, pin, record, original


def test_only_full_admitted_fresh_article_and_media_resolve_retained_dependency(dependency):
    baseline, output, article, bundle, pin, record, _ = dependency
    before = record.read_bytes()
    inventory = prepare_dependencies(
        baseline, "a" * 64, [article], output, {"my-f5-com": (bundle, pin)}
    )
    item = inventory["documents"][0]
    assert item["reason"] == "independently_admitted_current_source_dependency"
    context = json.loads((output / item["context_path"]).read_bytes())
    assert context["evidence"]["input_kind"] == "independently_admitted_current_source"
    assert "collapsed: true" not in context["body"]
    assert context["evidence"]["admitted_source"]["sha256"] == pin
    assert context["evidence"]["baseline_receipt_sha256"] == "a" * 64
    assert record.read_bytes() == before, (
        "fresh admission must not reset the retained privacy ledger"
    )
    source.verify_baseline_prerequisite(output, context)
    copied = output / context["evidence"]["admitted_source"]["path"]
    assert (copied / article).stat().st_ino == (bundle / article).stat().st_ino


@pytest.mark.parametrize("change", ["receipt", "intermediate", "original"])
def test_replacement_rejects_tampered_source_intermediate_and_old_custody(dependency, change):
    baseline, output, article, bundle, pin, record, _ = dependency
    inventory = prepare_dependencies(
        baseline, "a" * 64, [article], output, {"my-f5-com": (bundle, pin)}
    )
    context = json.loads((output / inventory["documents"][0]["context_path"]).read_bytes())
    if change == "receipt":
        path = (
            output / context["evidence"]["admitted_source"]["path"] / "source-review-receipt.json"
        )
        path.write_text("changed")
    elif change == "intermediate":
        context["body"] += "\nInvented instruction.\n"
    else:
        saved = json.loads(record.read_bytes())
        path = output / "dependency-privacy" / saved["source"] / saved["item"]["article_path"]
        path.write_text(path.read_text() + "\nChanged retained source.\n")
    with pytest.raises(ValueError):
        source.verify_baseline_prerequisite(output, context)


def test_missing_or_superseded_current_admission_keeps_source_prerequisite_queued(
    dependency, monkeypatch
):
    baseline, output, article, bundle, pin, _, prior = dependency
    inventory = prepare_dependencies(baseline, "a" * 64, [article], output, {})
    assert "context_path" not in inventory["documents"][0]
    with pytest.raises(ValueError, match="receipt digest"):
        prepare_dependencies(
            baseline, "a" * 64, [article], output, {"my-f5-com": (bundle, "0" * 64)}
        )
    prior["manifest.json"] = json_bytes(
        {
            "documents": [
                {"path": article, "provenance": {"last_success_at": "2026-12-01T00:00:00Z"}}
            ]
        }
    )
    with pytest.raises(ValueError, match="superseded"):
        prepare_dependencies(baseline, "a" * 64, [article], output, {"my-f5-com": (bundle, pin)})
