"""A dynamically expanding dependency set must share one renewal allowance."""

import json

import pytest

from html_to_markdown import curation_renewal_dependencies as deps
from html_to_markdown.curation_renewal_policy import DependencyReviewRequired


def test_expanding_dependency_closure_cannot_start_a_second_cohort(tmp_path, monkeypatch):
    original = [f"content/docs-cloud-f5-com/{i}/index.md" for i in range(8)]
    later = ["content/docs-cloud-f5-com/later/index.md"]
    qualified = []

    def qualify(*args):
        qualified.append(True)
        raise DependencyReviewRequired(original if len(qualified) == 1 else later)

    monkeypatch.setattr(deps, "qualify_policy", qualify)
    prepared = []
    private = tmp_path / "dependency-privacy"
    private.mkdir()
    ready = []
    for i in range(8):
        pin = f"{i:064x}"
        directory = tmp_path / pin
        directory.mkdir()
        (directory / "context.json").write_text(json.dumps({"binding_sha256": pin}))
        ready.append(
            {"identity": str(i), "binding_sha256": pin, "context_path": pin + "/context.json"}
        )

    def prepare(*args):
        prepared.append(True)
        docs = (
            ready
            if len(prepared) == 2
            else [{"identity": str(i)} for i in range(8 if len(prepared) == 1 else 1)]
        )
        (private / "inventory.json").write_text(json.dumps({"documents": docs}))
        return {"documents": docs}

    monkeypatch.setattr(deps, "prepare_dependencies", prepare)
    calls = []

    def quantum(_output, inv, _journal, limit):
        calls.append(limit)
        return {"reviewed": min(limit, len(inv["documents"])), "deferred": 0}

    monkeypatch.setattr("html_to_markdown.baseline_review_schedule.review_quantum", quantum)
    monkeypatch.setattr(
        deps,
        "review_batch",
        lambda _out, inv, *_args: {
            "documents": [
                {**d, "disposition": "independently_validated"} for d in inv["documents"]
            ],
            "exceptions": 0,
        },
    )
    with pytest.raises(deps.DependencyReviewPending) as failure:
        deps.review_dependencies(tmp_path, [], tmp_path, "a" * 64, tmp_path / "journal", limit=8)
    assert calls == [8]
    assert failure.value.remaining == 1 and failure.value.reviewed == 8
