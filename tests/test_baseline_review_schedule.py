"""Resumable dependency cohorts preserve complete closure and scheduling fairness."""

import json

import pytest

from html_to_markdown import baseline_source_review as baseline
from html_to_markdown.baseline_review_schedule import review_quantum


def inventory(count):
    return {
        "baseline_receipt_sha256": "a" * 64,
        "documents": [
            {"identity": f"https://docs.cloud.f5.com/docs-v2/{i}", "path": f"documents/{i}.json"}
            for i in range(count)
        ],
    }


def test_two_hundred_dependencies_yield_and_queued_failures_do_not_starve(tmp_path, monkeypatch):
    calls = []

    def review(_output, selected, _journal):
        calls.append(selected["documents"])
        return {
            "history_integrity_verified": True,
            "documents": [
                {
                    **item,
                    "disposition": "queued"
                    if item["identity"].endswith("/0")
                    else "privacy_only_validated",
                }
                for item in selected["documents"]
            ],
        }

    monkeypatch.setattr(baseline, "review_baseline_batch", review)
    full = inventory(200)
    for _ in range(25):
        result = review_quantum(tmp_path, full, tmp_path / "journal", 8)
        assert result["reviewed"] == 8 and result["deferred"] == 192
        assert result["publication_approved"] is False
    visited = [item["identity"] for cohort in calls for item in cohort]
    assert visited == [item["identity"] for item in full["documents"]]
    assert json.loads((tmp_path / "review-schedule.json").read_bytes())["next_index"] == 0
    assert full["documents"] == inventory(200)["documents"]


def test_interrupted_cohort_resumes_without_advancing_its_cursor(tmp_path, monkeypatch):
    calls = []
    interrupted = False

    def review(_output, selected, _journal):
        calls.append([item["identity"] for item in selected["documents"]])
        if interrupted:
            raise OSError("interrupted before the complete cohort returned")
        return {"history_integrity_verified": True, "documents": selected["documents"]}

    monkeypatch.setattr(baseline, "review_baseline_batch", review)
    full = inventory(12)
    review_quantum(tmp_path, full, tmp_path / "journal", 4)
    before = (tmp_path / "review-schedule.json").read_bytes()
    interrupted = True
    with pytest.raises(OSError, match="interrupted"):
        review_quantum(tmp_path, full, tmp_path / "journal", 4)
    assert (tmp_path / "review-schedule.json").read_bytes() == before
    interrupted = False
    review_quantum(tmp_path, full, tmp_path / "journal", 4)
    assert calls[1] == calls[2] and calls[0] != calls[2]


def test_failed_history_check_cannot_commit_scheduling_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(
        baseline, "review_baseline_batch", lambda *args: {"history_integrity_verified": False}
    )
    with pytest.raises(ValueError, match="history integrity"):
        review_quantum(tmp_path, inventory(200), tmp_path / "journal", 8)
    assert not (tmp_path / "review-schedule.json").exists()


def test_inventory_change_preserves_prior_cursor_without_inheriting_approval(tmp_path, monkeypatch):
    calls = []

    def review(_output, selected, _journal):
        calls.append(selected["documents"])
        return {"history_integrity_verified": True}

    monkeypatch.setattr(baseline, "review_baseline_batch", review)
    first = inventory(12)
    review_quantum(tmp_path, first, tmp_path / "journal", 4)
    old = (tmp_path / "review-schedule.json").read_bytes()
    changed = {**first, "baseline_receipt_sha256": "b" * 64}
    result = review_quantum(tmp_path, changed, tmp_path / "journal", 4)
    assert calls[0] == calls[1] and not result["publication_approved"]
    assert [p.read_bytes() for p in (tmp_path / "scheduling-history").iterdir()] == [old]


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_limit_cannot_start_a_review(tmp_path, monkeypatch, limit):
    monkeypatch.setattr(
        baseline, "review_baseline_batch", lambda *args: pytest.fail("inference started")
    )
    with pytest.raises(ValueError, match="positive integer"):
        review_quantum(tmp_path, inventory(12), tmp_path / "journal", limit)


@pytest.mark.parametrize("saved", [[], {"next_index": 200}, {"version": "unknown"}])
def test_malformed_cursor_fails_before_review(tmp_path, monkeypatch, saved):
    (tmp_path / "review-schedule.json").write_text(json.dumps(saved))
    monkeypatch.setattr(
        baseline, "review_baseline_batch", lambda *args: pytest.fail("inference started")
    )
    with pytest.raises(ValueError, match="scheduling checkpoint"):
        review_quantum(tmp_path, inventory(12), tmp_path / "journal", 4)


def test_changed_request_context_starts_new_traversal(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        baseline,
        "review_baseline_batch",
        lambda _output, selected, _journal: (
            calls.append(selected["documents"]) or {"history_integrity_verified": True}
        ),
    )
    review_quantum(tmp_path, inventory(12), tmp_path / "journal", 4)
    monkeypatch.setattr(
        "html_to_markdown.baseline_review_schedule.review_context_digest", lambda: "b" * 64
    )
    review_quantum(tmp_path, inventory(12), tmp_path / "journal", 4)
    assert calls[0] == calls[1]
