"""Late independent checks preserve exhausted proposals and all historical attempts."""

import json
from pathlib import Path

import pytest
import test_baseline_source_review as fixtures

from html_to_markdown import baseline_late_validation as late
from html_to_markdown import baseline_source_review as source
from html_to_markdown.enrichment.analysis import sha

Client = fixtures.Client


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    return fixtures.prepared.__wrapped__(tmp_path, monkeypatch)


def test_validation_only_does_not_repropose_or_reset_exhausted_ledger(prepared):
    _, output, record, _ = prepared
    rejected = Client(reject=True)
    with pytest.raises(ValueError, match="exhausted"):
        source.review_baseline_privacy(output, record, rejected)
    before = json.loads(record.read_bytes())
    assert len(before["privacy_feedback"]) == 3
    original_proposal = before["phases"]["privacy"]
    attempts = {str(p): sha(p.read_bytes()) for p in (output / "attempts").rglob("*.json")}
    validator = Client()
    result = late.validate_existing_baseline(output, record, validator)
    assert result["status"] == "privacy_only_validated"
    assert result["privacy_feedback"] == before["privacy_feedback"]
    assert result["phases"]["privacy"] == original_proposal
    assert [call["text"]["format"]["name"] for call in validator.calls] == ["admissionvalidation"]
    assert all(sha(Path(name).read_bytes()) == digest for name, digest in attempts.items())
    raw = record.read_bytes()
    assert source.review_baseline_privacy(output, record) == result
    assert record.read_bytes() == raw
    assert late.validate_existing_baseline(output, record) == result


def test_actual_late_privacy_rejection_keeps_the_original_record(prepared):
    _, output, record, _ = prepared
    with pytest.raises(ValueError, match="exhausted"):
        source.review_baseline_privacy(output, record, Client(reject=True))
    original = record.read_bytes()
    with pytest.raises(ValueError, match="independent"):
        late.validate_existing_baseline(output, record, Client(reject=True))
    assert record.read_bytes() == original


def test_late_validation_cannot_accept_modified_source_or_proposal(prepared):
    _, output, record, _ = prepared
    with pytest.raises(ValueError, match="exhausted"):
        source.review_baseline_privacy(output, record, Client(reject=True))
    saved = json.loads(record.read_bytes())
    path = output / saved["source"] / saved["item"]["article_path"]
    path.write_text(path.read_text().replace("8443", "443"))
    client = Client()
    with pytest.raises(ValueError, match="source"):
        late.validate_existing_baseline(output, record, client)
    assert not client.calls


def test_selected_validation_survives_prepare_only_for_same_source_and_execution(
    prepared, monkeypatch
):
    baseline, output, record, article = prepared
    with pytest.raises(ValueError, match="exhausted"):
        source.review_baseline_privacy(output, record, Client(reject=True))
    selected = late.validate_existing_baseline(output, record, Client())
    original = record.read_bytes()
    monkeypatch.setattr(source, "_controller", lambda: "b" * 64)
    source.prepare_baseline_reviews(baseline, "a" * 64, [article], output)
    assert record.read_bytes() == original
    assert (
        source.baseline_context(output, output, selected)[0]["body"]
        == selected["candidate"]["body"]
    )
    monkeypatch.setattr(late, "_execution_digest", lambda: "c" * 64)
    with pytest.raises(ValueError, match="stale"):
        source.review_baseline_privacy(output, record)
    source.prepare_baseline_reviews(baseline, "a" * 64, [article], output)
    assert json.loads(record.read_bytes())["status"] == "privacy_review_required"
    assert json.loads(record.read_bytes())["privacy_feedback"] == selected["privacy_feedback"]


def test_modified_original_proposal_cannot_be_selected(prepared):
    _, output, record, _ = prepared
    with pytest.raises(ValueError, match="exhausted"):
        source.review_baseline_privacy(output, record, Client(reject=True))
    data = json.loads(record.read_bytes())
    data["phases"]["privacy"]["request_sha256"] = "d" * 64
    record.write_text(json.dumps(data))
    client = Client()
    with pytest.raises(ValueError):
        late.validate_existing_baseline(output, record, client)
    assert not client.calls
