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
def test_changed_privacy_inputs_do_not_reuse_approvals(field):
    base = {
        "identity": "article",
        "article_sha256": "a",
        "metadata": {},
        "media": [],
        "request_context_sha256": "code",
        "curation_policy_sha256": "old",
    }
    old = {
        "binding": base,
        "phases": {"privacy": {"prompt_version": "source-privacy/2", "error": None}},
    }
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
