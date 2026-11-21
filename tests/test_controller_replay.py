"""Qualified upgrades replay complete source evidence without another model call."""

import json
from pathlib import Path

import pytest
from test_source_review_jobs import LocalClient

import html_to_markdown.source_review_jobs as jobs
from html_to_markdown import review_controller_compatibility as compatible
from html_to_markdown import source_review
from html_to_markdown.policy_activation import controller_digest
from html_to_markdown.source_review_jobs import prepare_reviews, run_reviews, verify_reviews


def test_qualified_controller_upgrade_replays_complete_original_requests_without_inference(
    capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    output = tmp_path / "review"
    old_context = "a" * 64
    current_digest = source_review.review_context_digest
    monkeypatch.setattr(source_review, "review_context_digest", lambda: old_context)
    monkeypatch.setattr(jobs, "CodexClient", LocalClient)
    prepared = prepare_reviews(capture, output)
    first = run_reviews(capture, output, phase="all", journal=tmp_path / "journal")
    assert not first["exceptions"]
    record = output / prepared["documents"][0]["path"]
    before = record.read_bytes()
    old = json.loads(before)
    hashes = {key: value["request_sha256"] for key, value in old["phases"].items()}
    monkeypatch.setattr(source_review, "review_context_digest", current_digest)
    registry = {
        "schema_version": 1,
        "controller_sha256": controller_digest(),
        "previous": [
            {
                "source_commit": "b" * 40,
                "software_tag": "v1.14.33",
                "wheel_sha256": "c" * 64,
                "controller_sha256": "d" * 64,
                "request_context_sha256": old_context,
            }
        ],
    }
    monkeypatch.setattr(compatible, "read_registry", lambda: registry)
    monkeypatch.setattr(
        LocalClient,
        "execute",
        lambda *_: pytest.fail("a compatible complete source needs no new inference"),
    )
    verify_reviews(capture, output)
    assert record.read_bytes() == before
    assert (
        prepare_reviews(capture, output)["documents"][0]["binding_sha256"] == old["binding_sha256"]
    )
    assert record.read_bytes() == before
    assert {
        key: value["request_sha256"]
        for key, value in json.loads(record.read_bytes())["phases"].items()
    } == hashes
