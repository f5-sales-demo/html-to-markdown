"""Prepared context renewal preserves exact proposals and never adds editor rounds."""

import json

import pytest
from test_baseline_source_review import Client

from html_to_markdown import baseline_late_validation as late
from html_to_markdown import baseline_source_context as context
from html_to_markdown import baseline_source_review as source
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha

pytest_plugins = ["test_baseline_source_review"]


def test_context_upgrade_revalidates_original_proposal_without_new_editor(prepared, monkeypatch):
    baseline, output, record, article = prepared
    first = source.review_baseline_privacy(output, record, Client())
    original = record.read_bytes()
    monkeypatch.setattr(source, "review_context_digest", lambda: "e" * 64)
    monkeypatch.setattr(context, "review_context_digest", lambda: "e" * 64)
    monkeypatch.setattr(late, "review_context_digest", lambda: "e" * 64)
    source.prepare_baseline_reviews(baseline, "a" * 64, [article], output)
    current = json.loads(record.read_bytes())
    assert current["privacy_feedback"] == first["privacy_feedback"]
    assert current["phases"]["privacy"] == first["phases"]["privacy"]
    assert current["status"] == "privacy_review_required"
    assert current["existing_proposal_pending"]
    client = Client()
    result = source.review_baseline_privacy(output, record, client)
    assert result["status"] == "privacy_only_validated"
    assert result["phases"] == first["phases"]
    assert [body["text"]["format"]["name"] for body in client.calls] == ["admissionvalidation"]
    assert result["privacy_feedback"] == first["privacy_feedback"]
    assert original in [p.read_bytes() for p in (output / "history").glob("*.json")]
    assert source.review_baseline_privacy(output, record) == result
    source.prepare_baseline_reviews(baseline, "a" * 64, [article], output)
    assert json.loads(record.read_bytes()) == result


def test_current_context_cannot_use_older_fixed_relevance_checkpoint(prepared, monkeypatch):
    baseline, output, record, article = prepared
    first = Client()
    first.tamper_phase = "relevance_binding"
    with pytest.raises(ValueError):
        source.review_baseline_privacy(output, record, first)
    monkeypatch.setattr(source, "review_context_digest", lambda: "e" * 64)
    monkeypatch.setattr(context, "review_context_digest", lambda: "e" * 64)
    monkeypatch.setattr(late, "review_context_digest", lambda: "e" * 64)
    source.prepare_baseline_reviews(baseline, "a" * 64, [article], output)
    client = Client()
    result = source.review_baseline_privacy(output, record, client)
    assert result["status"] == "privacy_only_validated"
    assert client.calls[0]["text"]["format"]["name"] == "relevancedecision"
    assert len(list((output / "request-checkpoints").rglob("*.json"))) >= 2


def test_rejected_current_validation_does_not_grant_editor_corrections(prepared, monkeypatch):
    baseline, output, record, article = prepared
    first = source.review_baseline_privacy(output, record, Client())
    monkeypatch.setattr(source, "review_context_digest", lambda: "e" * 64)
    monkeypatch.setattr(context, "review_context_digest", lambda: "e" * 64)
    monkeypatch.setattr(late, "review_context_digest", lambda: "e" * 64)
    source.prepare_baseline_reviews(baseline, "a" * 64, [article], output)
    client = Client(reject=True)
    with pytest.raises(ValueError, match="independent existing-proposal"):
        source.review_baseline_privacy(output, record, client)
    current = json.loads(record.read_bytes())
    assert current["privacy_feedback"] == first["privacy_feedback"]
    assert current["phases"]["privacy"] == first["phases"]["privacy"]
    assert all(body["text"]["format"]["name"] != "privacydecision" for body in client.calls)
    assert current["status"] != "privacy_only_validated"


def upgrade(monkeypatch):
    monkeypatch.setattr(source, "review_context_digest", lambda: "e" * 64)
    monkeypatch.setattr(context, "review_context_digest", lambda: "e" * 64)
    monkeypatch.setattr(late, "review_context_digest", lambda: "e" * 64)


def test_exhausted_original_proposal_can_be_validated_without_a_fourth_editor(
    prepared, monkeypatch
):
    baseline, output, record, article = prepared
    with pytest.raises(ValueError, match="exhausted"):
        source.review_baseline_privacy(output, record, Client(reject=True))
    original = json.loads(record.read_bytes())
    assert len(original["privacy_feedback"]) == 3
    upgrade(monkeypatch)
    source.prepare_baseline_reviews(baseline, "a" * 64, [article], output)
    client = Client()
    reviewed = source.review_baseline_privacy(output, record, client)
    assert reviewed["privacy_feedback"] == original["privacy_feedback"]
    assert reviewed["phases"]["privacy"] == original["phases"]["privacy"]
    assert [c["text"]["format"]["name"] for c in client.calls] == ["admissionvalidation"]


@pytest.mark.parametrize("damage", ["missing", "changed", "prepared_guard", "budget", "source"])
def test_pending_origin_or_prepared_guard_tampering_blocks_all_inference(
    prepared, monkeypatch, damage
):
    baseline, output, record, article = prepared
    source.review_baseline_privacy(output, record, Client())
    upgrade(monkeypatch)
    source.prepare_baseline_reviews(baseline, "a" * 64, [article], output)
    saved = json.loads(record.read_bytes())
    origin = (
        output / "history" / (saved["existing_proposal_pending"]["source_record_sha256"] + ".json")
    )
    if damage == "missing":
        origin.unlink()
    elif damage == "changed":
        origin.write_bytes(origin.read_bytes() + b" ")
    elif damage == "prepared_guard":
        pending = saved["existing_proposal_pending"]
        pending["prepared_binding"]["source_privacy_controller_sha256"] = "f" * 64
        pending["prepared_binding_sha256"] = sha(json_bytes(pending["prepared_binding"]))
        record.write_bytes(json_bytes(saved))
    elif damage == "budget":
        saved["privacy_feedback"].append("Unapproved extra round")
        record.write_bytes(json_bytes(saved))
    else:
        path = output / saved["source"] / saved["item"]["article_path"]
        path.write_bytes(path.read_bytes() + b"Changed instruction")
    client = Client()
    with pytest.raises(ValueError):
        source.review_baseline_privacy(output, record, client)
    assert not client.calls


def test_history_recovers_proposals_from_the_older_reset_envelope(prepared, monkeypatch):
    baseline, output, record, article = prepared
    reviewed = source.review_baseline_privacy(output, record, Client())
    original = record.read_bytes()
    history = output / "history" / (sha(original) + ".json")
    history.parent.mkdir(exist_ok=True)
    history.write_bytes(original)
    reset = dict(reviewed)
    reset["phases"] = {}
    reset.pop("candidate")
    record.write_bytes(json_bytes(reset))
    upgrade(monkeypatch)
    source.prepare_baseline_reviews(baseline, "a" * 64, [article], output)
    selected = json.loads(record.read_bytes())
    assert selected["phases"] == reviewed["phases"]
    client = Client()
    source.review_baseline_privacy(output, record, client)
    assert all(c["text"]["format"]["name"] != "privacydecision" for c in client.calls)
    assert history.read_bytes() == original


def test_interrupted_current_validation_resumes_exact_request_and_ledger(prepared, monkeypatch):
    baseline, output, record, article = prepared
    source.review_baseline_privacy(output, record, Client())
    upgrade(monkeypatch)
    source.prepare_baseline_reviews(baseline, "a" * 64, [article], output)
    before = record.read_bytes()

    class Interrupted(Client):
        def execute(self, body):
            evidence = super().execute(body)
            return evidence.model_copy(update={"error": "HTTP 503 temporarily unavailable"})

    with pytest.raises(ValueError):
        source.review_baseline_privacy(output, record, Interrupted())
    assert record.read_bytes() == before
    client = Client()
    reviewed = source.review_baseline_privacy(output, record, client)
    assert reviewed["privacy_feedback"] == json.loads(before)["privacy_feedback"]
    assert len(client.calls) == 1
    assert client.calls[0]["text"]["format"]["name"] == "admissionvalidation"
