"""Current full admission resolves damaged retained prerequisites with immutable proofs."""

import json

import pytest
from test_combined_sources import admitted

from html_to_markdown import admitted_baseline_sources as fresh
from html_to_markdown import baseline_source_review as source
from html_to_markdown.admitted_baseline_sources import _admitted_choice
from html_to_markdown.curation import json_bytes
from html_to_markdown.curation_renewal_dependencies import prepare_dependencies
from html_to_markdown.enrichment.analysis import sha
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


@pytest.fixture
def replacement_lookup(tmp_path):
    article = "content/my-f5-com/K000111222/index.md"
    verified = {
        article: b'---\n{"sourceId":"my-f5-com","url":"https://my.f5.com/knowledge/"}\n---\nGuide.'
    }
    directory = tmp_path / "source"
    directory.mkdir()
    receipt = {
        "source": "my-f5-com",
        "documents": [{"published_path": article, "disposition": "queued"}],
    }
    return verified, article, directory, receipt


def _pinned_lookup(fixture):
    verified, article, directory, receipt = fixture
    data = json_bytes(receipt)
    (directory / fresh.MARKER).write_bytes(data)
    return verified, article, {"my-f5-com": (directory, sha(data))}


@pytest.mark.parametrize("disposition", [None, "queued", "policy_excluded", "capture_error"])
def test_pinned_negative_lookup_never_replays_or_approves(
    replacement_lookup, monkeypatch, disposition
):
    _, _, _, receipt = replacement_lookup
    if disposition is None:
        receipt["documents"] = []
    else:
        receipt["documents"][0]["disposition"] = disposition

    def forbidden_replay(*_args):
        pytest.fail("an unadmitted replacement must not replay the complete source")

    monkeypatch.setattr(fresh, "verify_source", forbidden_replay)
    assert _admitted_choice(*_pinned_lookup(replacement_lookup)) is None


def test_negative_lookup_rejects_changed_receipt_pin(replacement_lookup):
    verified, article, inputs = _pinned_lookup(replacement_lookup)
    directory, _ = inputs["my-f5-com"]
    with pytest.raises(ValueError, match="receipt digest"):
        _admitted_choice(verified, article, {"my-f5-com": (directory, "0" * 64)})


def test_negative_lookup_rejects_wrong_source(replacement_lookup):
    replacement_lookup[3]["source"] = "community-f5-com"
    with pytest.raises(ValueError, match="another source"):
        _admitted_choice(*_pinned_lookup(replacement_lookup))


@pytest.mark.parametrize("target", ["directory", "marker"])
def test_negative_lookup_rejects_symlinks(replacement_lookup, tmp_path, target):
    verified, article, inputs = _pinned_lookup(replacement_lookup)
    directory, pin = inputs["my-f5-com"]
    if target == "directory":
        linked = tmp_path / "linked"
        linked.symlink_to(directory, target_is_directory=True)
        inputs["my-f5-com"] = linked, pin
    else:
        marker = directory / fresh.MARKER
        saved = directory / "saved-receipt.json"
        marker.rename(saved)
        marker.symlink_to(saved)
    with pytest.raises(ValueError, match="unsafe"):
        _admitted_choice(verified, article, inputs)


def test_possible_accepted_replacement_requires_complete_replay(replacement_lookup, monkeypatch):
    replacement_lookup[3]["documents"][0]["disposition"] = "accepted"

    def rejected_replay(*_args):
        raise ValueError("complete source replay rejected")

    monkeypatch.setattr(fresh, "verify_source", rejected_replay)
    with pytest.raises(ValueError, match="complete source replay"):
        _admitted_choice(*_pinned_lookup(replacement_lookup))


def test_accepted_replacement_rejects_changed_original_before_preparation(dependency):
    baseline, output, article, bundle, pin, _, _ = dependency
    receipt = json.loads((bundle / fresh.MARKER).read_bytes())
    entry = next(d for d in receipt["documents"] if d.get("published_path") == article)
    record = json.loads((bundle / ".admission/review" / entry["record"]).read_bytes())
    original = bundle / ".admission/capture" / record["item"]["article_path"]
    original.write_text(original.read_text() + "\nChanged source instruction.\n")
    with pytest.raises(ValueError):
        fresh.prepare_replacement(baseline, "a" * 64, article, output, {"my-f5-com": (bundle, pin)})
