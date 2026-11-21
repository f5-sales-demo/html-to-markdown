"""Immutable old replay provenance composes only through exact released registry pins."""

import json
from copy import deepcopy

import pytest
from test_source_review_jobs import LocalClient

from html_to_markdown import review_controller_compatibility as compatibility
from html_to_markdown import source_provenance_chain as chain
from html_to_markdown import source_review, source_review_jobs
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.source_inputs import seal_source, verify_source


@pytest.fixture
def history(tmp_path, monkeypatch):
    old_context = "a" * 64
    active_old_context = "b" * 64
    old_code = "1" * 64
    new_code = "2" * 64
    prior = {
        "source_commit": "3" * 40,
        "software_tag": "v1.0.0",
        "wheel_sha256": "4" * 64,
        "controller_sha256": "5" * 64,
        "request_context_sha256": old_context,
    }
    selected = {
        "source_commit": "6" * 40,
        "software_tag": "v1.1.0",
        "wheel_sha256": "7" * 64,
        "controller_sha256": old_code,
        "request_context_sha256": active_old_context,
    }
    old_registry = {"schema_version": 1, "controller_sha256": old_code, "previous": [prior]}
    current_registry = {
        "schema_version": 1,
        "controller_sha256": new_code,
        "previous": [prior, selected],
    }
    old_bytes = json_bytes(old_registry)
    stored = {
        "schema_version": 1,
        "registries": [
            {**selected, "registry_sha256": sha(old_bytes), "registry_text": old_bytes.decode()}
        ],
    }
    registry_path = tmp_path / compatibility.REGISTRY
    registry_path.write_bytes(json_bytes(current_registry))
    (tmp_path / chain.HISTORY).write_bytes(json_bytes(stored))
    monkeypatch.setattr(
        compatibility, "__file__", str(tmp_path / "review_controller_compatibility.py")
    )
    monkeypatch.setattr(chain, "__file__", str(tmp_path / "source_provenance_chain.py"))
    monkeypatch.setattr(chain, "controller_digest", lambda: new_code)
    contexts = {old_context, active_old_context}
    original = {
        "registry_sha256": sha(old_bytes),
        "controller_sha256": old_code,
        "prior_request_context_sha256": [old_context],
    }
    current = {
        "registry_sha256": sha(registry_path.read_bytes()),
        "controller_sha256": new_code,
        "prior_request_context_sha256": sorted(contexts),
    }
    return original, current, contexts, stored, tmp_path


def test_exact_historical_provenance_is_preserved_in_the_new_transition(history):
    original, current, contexts, _, _ = history
    before = deepcopy(original)
    result = chain.verify_provenance_transition(original, current, contexts, "f" * 64)
    assert original == before and result["original"] == original and result["current"] == current
    assert result["source_receipt_sha256"] == "f" * 64
    assert result["source_and_decisions_unchanged"] is True


@pytest.mark.parametrize(
    "change",
    [
        "old_digest",
        "old_code",
        "old_context",
        "current_digest",
        "current_code",
        "current_context",
        "unknown_wheel",
        "changed_registry",
        "unknown_history",
    ],
)
def test_changed_or_unqualified_provenance_cannot_compose(history, change):
    original, current, contexts, stored, root = history
    if change == "old_digest":
        original["registry_sha256"] = "d" * 64
    elif change == "old_code":
        original["controller_sha256"] = "d" * 64
    elif change == "old_context":
        original["prior_request_context_sha256"] = ["d" * 64]
    elif change == "current_digest":
        current["registry_sha256"] = "d" * 64
    elif change == "current_code":
        current["controller_sha256"] = "d" * 64
    elif change == "current_context":
        current["prior_request_context_sha256"] = ["d" * 64]
    elif change == "unknown_wheel":
        stored["registries"][0]["wheel_sha256"] = "d" * 64
    elif change == "changed_registry":
        stored["registries"][0]["registry_text"] += " "
    else:
        stored["registries"] = []
    (root / chain.HISTORY).write_bytes(json_bytes(stored))
    with pytest.raises(ValueError, match="provenance|registry|qualification"):
        chain.verify_provenance_transition(original, current, contexts, "f" * 64)


def test_current_provenance_is_still_exactly_validated(history):
    _, current, contexts, _, _ = history
    assert not chain.verify_provenance_transition(current, current, contexts, "f" * 64)
    current["registry_sha256"] = "d" * 64
    with pytest.raises(ValueError, match="provenance"):
        chain.verify_provenance_transition(current, current, contexts, "f" * 64)


# Keep the original receipt, registry epochs and unchanged-source assertions in one transaction.
# pylint: disable-next=too-many-locals
def test_complete_original_receipt_replays_across_two_qualified_upgrades(
    capture, tmp_path, monkeypatch
):
    old_context = "a" * 64
    old_code = "d" * 64
    next_code = "e" * 64
    final_code = "f" * 64
    active_context = "b" * 64
    prior = {
        "source_commit": "1" * 40,
        "software_tag": "v1.0.0",
        "wheel_sha256": "2" * 64,
        "controller_sha256": "3" * 64,
        "request_context_sha256": old_context,
    }
    old_registry = {"schema_version": 1, "controller_sha256": old_code, "previous": [prior]}
    inventory_path = capture / "inventory.json"
    inventory = json.loads(inventory_path.read_bytes())
    inventory["binding"]["source"] = "my-f5-com"
    inventory["completeness"] = {"enumeration": True, "capture": True}
    inventory["ended_at"] = "2026-11-21T00:00:00Z"
    inventory_path.write_bytes(json_bytes(inventory))
    registry_dir = tmp_path / "registries"
    registry_dir.mkdir()
    registry_path = registry_dir / compatibility.REGISTRY
    registry_path.write_bytes(json_bytes(old_registry))
    monkeypatch.setattr(
        compatibility, "__file__", str(registry_dir / "review_controller_compatibility.py")
    )
    monkeypatch.setattr(chain, "__file__", str(registry_dir / "source_provenance_chain.py"))
    monkeypatch.setattr(compatibility, "controller_digest", lambda: old_code)
    monkeypatch.setattr(chain, "controller_digest", lambda: old_code)
    actual_context = source_review.review_context_digest
    monkeypatch.setattr(source_review, "review_context_digest", lambda: old_context)
    monkeypatch.setattr(source_review_jobs, "CodexClient", LocalClient)
    review = tmp_path / "review"
    source_review_jobs.prepare_reviews(capture, review)
    report = source_review_jobs.run_reviews(
        capture, review, phase="all", journal=tmp_path / "journal"
    )
    assert not report["exceptions"]
    sealed = tmp_path / "sealed"
    receipt = seal_source(capture, review, sealed)
    marker = sealed / "source-review-receipt.json"
    original = marker.read_bytes()
    before = {
        p.relative_to(sealed).as_posix(): sha(p.read_bytes())
        for p in sealed.rglob("*")
        if p.is_file()
    }
    qualified = {
        "source_commit": "4" * 40,
        "software_tag": "v1.1.0",
        "wheel_sha256": "5" * 64,
        "controller_sha256": old_code,
        "request_context_sha256": active_context,
    }
    history = {
        "schema_version": 1,
        "registries": [
            {
                **qualified,
                "registry_sha256": sha(json_bytes(old_registry)),
                "registry_text": json_bytes(old_registry).decode(),
            }
        ],
    }
    (registry_dir / chain.HISTORY).write_bytes(json_bytes(history))
    monkeypatch.setattr(source_review, "review_context_digest", actual_context)
    monkeypatch.setattr(
        LocalClient,
        "execute",
        lambda *_: pytest.fail("Qualified original evidence must replay without inference"),
    )
    for code in (next_code, final_code):
        registry = {"schema_version": 1, "controller_sha256": code, "previous": [prior, qualified]}
        registry_path.write_bytes(json_bytes(registry))
        monkeypatch.setattr(compatibility, "controller_digest", lambda code=code: code)
        monkeypatch.setattr(chain, "controller_digest", lambda code=code: code)
        result = verify_source(sealed, sha(original))
        assert result["accepted_files"] == receipt["accepted_files"]
        transition = result["review_compatibility_transition"]
        assert transition["original"] == receipt["review_compatibility"]
        assert transition["current"]["controller_sha256"] == code
        assert marker.read_bytes() == original
    assert before == {
        p.relative_to(sealed).as_posix(): sha(p.read_bytes())
        for p in sealed.rglob("*")
        if p.is_file()
    }
    bad = json.loads((registry_dir / chain.HISTORY).read_bytes())
    bad["registries"][0]["registry_text"] += " "
    (registry_dir / chain.HISTORY).write_bytes(json_bytes(bad))
    with pytest.raises(ValueError, match="provenance digest"):
        verify_source(sealed, sha(original))
