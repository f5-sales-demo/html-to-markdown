"""Conflict inventories preserve complete provenance and grant no approval."""

import json

import pytest
from test_baseline_global_ledgers import cloned, exhaust

from html_to_markdown.baseline_ledger_inventory import inventory_record
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha

pytest_plugins = ["test_baseline_source_review"]


def test_all_strongest_conflicts_are_recorded_without_granting_allowance(prepared, tmp_path):
    root = tmp_path / "worker"
    first, donor = cloned(prepared, root, "first")
    second, record = cloned(prepared, root, "second")
    saved = exhaust(first, donor)
    data = donor.read_bytes()
    history = first / "history" / (sha(data) + ".json")
    history.parent.mkdir(exist_ok=True)
    history.write_bytes(data)
    later = json.loads(data)
    later["phases"]["privacy"]["request_sha256"] = "b" * 64
    donor.write_bytes(json_bytes(later))
    originals = {path: path.read_bytes() for path in [donor, history, record]}
    result = inventory_record(second, record, root)
    assert result["conflicted"] and result["strongest_feedback_entries"] == 3
    assert len(result["strongest_records"]) == 2
    assert result["new_editor_proposals"] == 0
    assert result["article_approved"] is False
    assert all(path.read_bytes() == data for path, data in originals.items())
    assert all(
        item["feedback_entries"] == len(saved["privacy_feedback"])
        for item in result["strongest_records"]
    )


def test_weaker_current_record_does_not_hide_exhausted_archive(prepared, tmp_path):
    root = tmp_path / "worker"
    first, donor = cloned(prepared, root, "first")
    second, record = cloned(prepared, root, "second")
    exhaust(first, donor)
    data = donor.read_bytes()
    path = first / "history" / (sha(data) + ".json")
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    donor.unlink()
    result = inventory_record(second, record, root)
    assert result["strongest_feedback_entries"] == 3
    assert len(result["strongest_records"]) == 1
    assert result["strongest_records"][0]["record_sha256"] == sha(data)


def test_new_matching_history_changes_the_bound_inventory(prepared, tmp_path):
    root = tmp_path / "worker"
    first, donor = cloned(prepared, root, "first")
    second, record = cloned(prepared, root, "second")
    exhaust(first, donor)
    before = inventory_record(second, record, root)
    later = json.loads(donor.read_bytes())
    later["phases"]["privacy"]["request_sha256"] = "b" * 64
    data = json_bytes(later)
    path = first / "history" / (sha(data) + ".json")
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    after = inventory_record(second, record, root)
    assert after["inventory_sha256"] != before["inventory_sha256"]
    assert after["conflicted"]


def test_changed_source_custody_fails_inventory(prepared, tmp_path):
    root = tmp_path / "worker"
    first, donor = cloned(prepared, root, "first")
    second, record = cloned(prepared, root, "second")
    saved = exhaust(first, donor)
    source = first / saved["source"] / saved["item"]["article_path"]
    source.write_bytes(source.read_bytes() + b" changed")
    with pytest.raises(ValueError, match="source bytes changed"):
        inventory_record(second, record, root)
