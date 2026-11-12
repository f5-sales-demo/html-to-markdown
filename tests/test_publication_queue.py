"""Publication progresses through source review and enrichment with visible gates."""

import json
from pathlib import Path

import pytest

from html_to_markdown.publication_queue import advance_publication, stage_publication


@pytest.fixture(autouse=True)
def deployed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "html_to_markdown.publication_queue.require_current_policy", lambda _: "a" * 40
    )
    monkeypatch.setattr(
        "html_to_markdown.publication_queue.deployed_revision",
        lambda: {"publication_sha256": "b" * 64},
    )


def test_private_coordinator_waits_for_source_then_mac_then_qualified_software(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("html_to_markdown.publication_queue.platform.system", lambda: "Linux")
    assert advance_publication(tmp_path)["state"] == "waiting_for_source_reviews"
    root = tmp_path / ".publication"
    job = root / "job-test"
    job.mkdir()
    (job / "publication-job.json").write_text(json.dumps({"baseline_receipt_sha256": "b" * 64}))
    (root / "current-job.json").write_text(json.dumps({"job": "job-test", "fingerprint": "a" * 64}))
    assert advance_publication(tmp_path)["state"] == "waiting_for_mac_enrichment"
    (job / "validated-enrichment.json").write_text("{}")
    assert advance_publication(tmp_path)["state"] == "waiting_for_qualified_software_commit"
    calls = []

    def stage(path: Path, source_commit: str) -> dict[str, str]:
        calls.append((path, source_commit))
        return {"tag": "content-test"}

    monkeypatch.setattr(
        "html_to_markdown.publication_queue.stage_publication",
        stage,
    )
    assert advance_publication(tmp_path, source_commit="a" * 40)["state"] == "staged"
    assert calls == [(job, "a" * 40)]


def test_superseded_job_is_retained_and_never_staged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("html_to_markdown.publication_queue.platform.system", lambda: "Linux")
    root = tmp_path / ".publication"
    job = root / "job-old"
    job.mkdir(parents=True)
    (root / "current-job.json").write_text(json.dumps({"job": "job-old"}))
    (job / "publication-job.json").write_text(json.dumps({"baseline_receipt_sha256": "a" * 64}))
    (job / "validated-enrichment.json").write_text("private preserved evidence")
    monkeypatch.setattr(
        "html_to_markdown.publication_queue.stage_publication",
        lambda *args, **kwargs: pytest.fail("superseded input must not stage"),
    )
    assert (
        advance_publication(tmp_path, source_commit="c" * 40)["state"]
        == "waiting_for_source_reviews"
    )
    assert (job / "superseded.json").exists()
    assert (job / "validated-enrichment.json").read_text() == "private preserved evidence"


def test_draft_publication_requires_enrichment_before_any_github_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("html_to_markdown.publication_queue.platform.system", lambda: "Linux")
    (tmp_path / "publication-job.json").write_text("{}")
    with pytest.raises(ValueError, match="initial enrichment"):
        stage_publication(tmp_path, source_commit="a" * 40)


def test_queued_publication_cannot_progress_after_policy_supersession(tmp_path, monkeypatch):
    monkeypatch.setattr("html_to_markdown.publication_queue.platform.system", lambda: "Linux")
    state = tmp_path / ".publication"
    job = state / "job-old"
    job.mkdir(parents=True)
    (state / "current-job.json").write_text(json.dumps({"job": "job-old"}))
    (job / "publication-job.json").write_text(json.dumps({"baseline_receipt_sha256": "b" * 64}))
    (job / "validated-enrichment.json").write_text("preserved")

    def superseded(_):
        raise ValueError("merged policy was superseded")

    monkeypatch.setattr("html_to_markdown.publication_queue.require_current_policy", superseded)
    monkeypatch.setattr(
        "html_to_markdown.publication_queue.stage_publication",
        lambda *a, **k: pytest.fail("stale policy must not stage"),
    )
    with pytest.raises(ValueError, match="superseded"):
        advance_publication(tmp_path, source_commit="a" * 40)
    assert (job / "validated-enrichment.json").read_text() == "preserved"


def test_publication_workflow_serializes_and_rechecks_latest_baseline() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github/workflows/publish-reviewed-content.yml"
    ).read_text()
    assert "group: content-snapshot" in workflow
    assert workflow.count('test "$(jq -er .publication_sha256') == 3
    assert "verify-publication" in workflow
    assert "--draft=false --latest=false" in workflow
    assert "retention-days: 90" in workflow
    assert "candidate/.admission" not in workflow
