"""Catalog changes cannot invalidate exact privacy work or preserve stale admission."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_source_review_jobs import LocalClient

from html_to_markdown.source_phase_bindings import privacy_context, reusable_privacy
from html_to_markdown.source_review_jobs import prepare_reviews, run_reviews, verify_reviews


def test_catalog_update_reuses_exact_privacy_but_requires_new_relevance_and_validation(
    capture: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    calls = []

    class Client(LocalClient):
        def execute(self, body):
            calls.append(body["text"]["format"]["name"])
            return super().execute(body)

    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", Client)
    output = tmp_path / "review"
    prepare_reviews(capture, output)
    first = run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    assert not first["exceptions"]
    record = next((output / "documents").glob("*.json"))
    original = json.loads(record.read_bytes())
    privacy = original["phases"]["privacy"]
    assert privacy["prompt_version"] == "source-privacy/2"
    monkeypatch.setattr(
        "html_to_markdown.source_review.load_curation_policy",
        lambda: SimpleNamespace(sha256="changed-policy"),
    )
    prepare_reviews(capture, output)
    pending = json.loads(record.read_bytes())
    assert pending["disposition"] == "queued"
    assert set(pending["phases"]) == {"privacy"}
    assert pending["phases"]["privacy"] == privacy
    monkeypatch.setattr(
        "html_to_markdown.source_review.load_curation_policy",
        lambda: SimpleNamespace(sha256="changed-policy-again"),
    )
    prepare_reviews(capture, output)
    pending_again = json.loads(record.read_bytes())
    assert pending_again["phases"]["privacy"] == privacy
    second = run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    assert not second["exceptions"]
    assert calls == [
        "relevancedecision",
        "privacydecision",
        "admissionvalidation",
        "relevancedecision",
        "admissionvalidation",
    ]
    assert len(verify_reviews(capture, output)["accepted_files"]) == 1
    assert json.loads(record.read_bytes())["phases"]["privacy"] == privacy


@pytest.mark.parametrize("field", ["article_sha256", "metadata", "media", "request_context_sha256"])
def test_changed_privacy_inputs_do_not_reuse_approvals(field, capture, tmp_path, monkeypatch):
    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", LocalClient)
    output = tmp_path / "review"
    prepare_reviews(capture, output)
    run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    old = json.loads(next((output / "documents").glob("*.json")).read_bytes())
    base = old["binding"]
    assert reusable_privacy(old, {**base, "curation_policy_sha256": "new"})
    assert not reusable_privacy(old, {**base, field: "changed"})
    assert not reusable_privacy(
        {**old, "phases": {"privacy": {"prompt_version": "source-admission/1", "error": None}}},
        base,
    )
    assert not reusable_privacy(
        {**old, "phases": {"privacy": {"prompt_version": "source-privacy/2", "error": "failed"}}},
        base,
    )


def test_unvalidated_proposal_is_not_reused_as_a_completed_privacy_decision():
    base = {"identity": "article", "curation_policy_sha256": "old", "article_sha256": "a"}
    pending = {
        "binding": base,
        "phases": {"privacy": {"prompt_version": "source-privacy/2", "error": None}},
    }
    assert not reusable_privacy(pending, {**base, "curation_policy_sha256": "new"})


def test_independent_rejection_invalidates_failed_response_caches(capture, tmp_path, monkeypatch):
    invalidated = []

    class RejectedClient(LocalClient):
        reject = True

        def invalidate(self, evidence):
            invalidated.append(evidence.request_sha256)

    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", RejectedClient)
    output = tmp_path / "review"
    prepare_reviews(capture, output)
    report = run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    assert report["documents"][0]["disposition"] == "queued"
    failed = json.loads(next((output / "documents").glob("*.json")).read_bytes())
    assert not reusable_privacy(failed, {**failed["binding"], "curation_policy_sha256": "new"})
    assert set(invalidated) == {
        failed["phases"][p]["request_sha256"] for p in ("privacy", "validation")
    }


def test_catalog_update_cannot_reset_exhausted_privacy_corrections(capture, tmp_path, monkeypatch):
    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", LocalClient)
    output = tmp_path / "review"
    prepare_reviews(capture, output)
    run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    record = next((output / "documents").glob("*.json"))
    failed = json.loads(record.read_bytes())
    failed["phases"].pop("validation")
    failed["privacy_feedback"] = ["first rejected correction", "second rejected correction"]
    failed["disposition"] = "queued"
    record.write_text(json.dumps(failed))
    monkeypatch.setattr(
        "html_to_markdown.source_review.load_curation_policy",
        lambda: SimpleNamespace(sha256="changed-policy"),
    )
    prepare_reviews(capture, output)
    current = json.loads(record.read_bytes())
    assert current["privacy_feedback"] == failed["privacy_feedback"]
    assert current["privacy_exhausted"] is True
    assert not current["phases"]
    monkeypatch.setattr(
        "html_to_markdown.source_review.load_curation_policy",
        lambda: SimpleNamespace(sha256="changed-again"),
    )
    prepare_reviews(capture, output)
    assert json.loads(record.read_bytes())["privacy_exhausted"] is True

    class NoMoreInference(LocalClient):
        def execute(self, body):
            pytest.fail("catalog changes cannot buy additional privacy proposals")

    monkeypatch.setattr("html_to_markdown.source_review_jobs.CodexClient", NoMoreInference)
    report = run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    assert report["documents"][0]["disposition"] == "queued"


def test_privacy_context_excludes_curation_state_but_keeps_complete_source():
    original = {
        "identity": "article",
        "binding_sha256": "global",
        "binding": {
            "identity": "article",
            "curation_policy_sha256": "old",
            "api_policy_sha256": "api",
            "article_sha256": "body",
        },
        "body": "whole source",
        "metadata": {"title": "title"},
        "media": {"digest": {"frames": []}},
        "retirement_rules": [{"version": 1}],
        "relevance_decision": {"retirement": "requires_curation"},
    }
    first = privacy_context(original)
    changed = {
        **original,
        "binding": {**original["binding"], "curation_policy_sha256": "new"},
        "retirement_rules": [{"version": 2}],
        "relevance_decision": {"retirement": "independent"},
    }
    assert privacy_context(changed) == first
    assert first["body"] == original["body"] and first["media"] == original["media"]
    assert "retirement_rules" not in first and "relevance_decision" not in first


def test_catalog_update_finishes_pending_validation_without_an_extra_privacy_proposal(
    capture, tmp_path, monkeypatch
):
    # pylint: disable=import-outside-toplevel
    import html_to_markdown.source_review_jobs as jobs  # pylint: disable=import-outside-toplevel
    from html_to_markdown.source_curation import (
        CurationRequired,  # pylint: disable=import-outside-toplevel
    )

    calls = []

    class Client(LocalClient):
        def execute(self, body):
            calls.append(body["text"]["format"]["name"])
            return super().execute(body)

    monkeypatch.setattr(jobs, "CodexClient", Client)
    output = tmp_path / "review"
    prepare_reviews(capture, output)
    record = next((output / "documents").glob("*.json"))
    pending = json.loads(record.read_bytes())
    pending["privacy_feedback"] = ["first correction", "second correction"]
    record.write_text(json.dumps(pending))
    original_preview = jobs.preview_curation

    def missing_catalog(*_):
        raise CurationRequired(CurationRequired.reason)

    monkeypatch.setattr(jobs, "preview_curation", missing_catalog)
    first = run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    assert first["documents"][0]["reason"] == CurationRequired.reason
    pending = json.loads(record.read_bytes())
    proposal = pending["phases"]["privacy"]
    assert "validation" not in pending["phases"]
    monkeypatch.setattr(
        "html_to_markdown.source_review.load_curation_policy",
        lambda: SimpleNamespace(sha256="changed-policy"),
    )
    monkeypatch.setattr(jobs, "preview_curation", original_preview)
    prepare_reviews(capture, output)
    current = json.loads(record.read_bytes())
    assert current["privacy_exhausted"] is False
    assert current["privacy_proposal_pending_validation"] is True
    assert current["phases"]["privacy"] == proposal
    monkeypatch.setattr(
        "html_to_markdown.source_review.load_curation_policy",
        lambda: SimpleNamespace(sha256="changed-again"),
    )
    prepare_reviews(capture, output)
    assert json.loads(record.read_bytes())["phases"]["privacy"] == proposal
    second = run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    assert not second["exceptions"]
    assert calls.count("privacydecision") == 1
    assert calls.count("admissionvalidation") == 1
    assert len(verify_reviews(capture, output)["accepted_files"]) == 1
