"""Every strongest original is visible; custody changes invalidate sealed inputs."""

# Inspect immutable input qualification and its streaming guard directly.
# pylint: disable=protected-access

import json
from pathlib import Path

import pytest
from test_source_existing_context import prepared

from html_to_markdown import source_existing_selection as selection
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha


def history(original, directory):
    directory.mkdir(parents=True, exist_ok=True)
    data = original.read_bytes()
    path = directory / (sha(data) + ".json")
    path.write_bytes(data)
    return path


def seal(capture, tmp_path, *, second=False):
    current, original, _, _ = prepared(capture, tmp_path)
    root = tmp_path / "histories"
    donor = history(original, root)
    if second:
        changed = json.loads(original.read_bytes())
        changed["privacy_feedback"][-1] = "A separate original validator failure"
        raw = json_bytes(changed)
        (root / (sha(raw) + ".json")).write_bytes(raw)
    pin = sha(current.read_bytes())
    output = tmp_path / "selection"
    path = selection.prepare_source_selection(capture, current, pin, [root], output)
    return path, current, donor, root


def test_all_strongest_histories_are_sealed_without_changing_records(capture, tmp_path):
    path, current, original, root = seal(capture, tmp_path, second=True)
    originals = {p: p.read_bytes() for p in [current, *root.glob("*.json")]}
    saved = selection.verify_source_selection(path)
    strongest = [row for row in saved["candidates"] if row["strongest"]]
    assert len(strongest) == 2
    summaries = saved["qualifications"]
    assert sum(s["disposition"] == "inputs_qualified" for s in summaries) == 2
    assert any(s["record_sha256"] == sha(original.read_bytes()) for s in summaries)
    assert saved["new_editor_allowances"] == 0
    assert saved["admission_approved"] is False
    assert originals == {p: p.read_bytes() for p in originals}
    assert (
        selection.prepare_source_selection(
            capture, current, sha(current.read_bytes()), [root], path.parent.parent
        )
        == path
    )
    assert not any("input_image" in key for key in saved)


@pytest.mark.parametrize("damage", ["new", "remove", "changed", "symlink", "current", "controller"])
def test_every_changed_custody_or_controller_invalidates_selection(
    capture, tmp_path, monkeypatch, damage
):
    path, current, original, root = seal(capture, tmp_path)
    before = path.read_bytes()
    if damage == "new":
        value = json.loads(original.read_bytes())
        value["privacy_feedback"].append("Additional retained correction")
        raw = json_bytes(value)
        (root / (sha(raw) + ".json")).write_bytes(raw)
    elif damage == "remove":
        original.unlink()
    elif damage == "changed":
        original.write_bytes(original.read_bytes() + b" ")
    elif damage == "symlink":
        preserved = root.parent / "preserved.json"
        original.rename(preserved)
        original.symlink_to(preserved)
    elif damage == "current":
        current.write_bytes(current.read_bytes() + b" ")
    else:
        monkeypatch.setattr(selection, "VERSION", "future-controller")
    with pytest.raises((ValueError, FileNotFoundError)):
        selection.verify_source_selection(path)
    assert path.read_bytes() == before


def test_missing_or_incomplete_strongest_proposal_stays_visible(capture, tmp_path):
    current, original, _, _ = prepared(capture, tmp_path)
    value = json.loads(original.read_bytes())
    value["phases"] = {}
    original.write_bytes(json_bytes(value))
    root = tmp_path / "histories"
    history(original, root)
    path = selection.prepare_source_selection(
        capture, current, sha(current.read_bytes()), [root], tmp_path / "selection"
    )
    saved = selection.verify_source_selection(path)
    rejected = [s for s in saved["qualifications"] if s["disposition"] == "inputs_rejected"]
    assert len(rejected) == 1 and rejected[0]["error_class"] == "KeyError"
    assert saved["admission_approved"] is False


def test_weak_completed_original_does_not_renew_strongest_allowances(capture, tmp_path):
    current, original, _, _ = prepared(capture, tmp_path)
    value = json.loads(current.read_bytes())
    value["privacy_feedback"] = ["Failure one", "Failure two", "Historical boundary failure"]
    current.write_bytes(json_bytes(value))
    root = tmp_path / "histories"
    history(original, root)
    path = selection.prepare_source_selection(
        capture, current, sha(current.read_bytes()), [root], tmp_path / "selection"
    )
    saved = selection.verify_source_selection(path)
    weak = next(
        s for s in saved["qualifications"] if s["record_sha256"] == sha(original.read_bytes())
    )
    assert weak["disposition"] == "superseded_by_stronger_ledger"
    assert saved["strongest_feedback_entries"] == 3


def test_unrelated_large_payload_is_streamed_without_loading_its_record(
    capture, tmp_path, monkeypatch
):
    path, _, _, root = seal(capture, tmp_path)
    unrelated = {
        "binding": {"identity": "another", "article_sha256": "0" * 64},
        "payload": "x" * (2 * 1024 * 1024),
    }
    raw = json_bytes(unrelated)
    other = root / (sha(raw) + ".json")
    other.write_bytes(raw)
    current = Path(json.loads(path.read_bytes())["current_record"])
    checked = selection._record

    def matching_only(record, digest):
        assert record != other
        return checked(record, digest)

    monkeypatch.setattr(selection, "_record", matching_only)
    inputs = selection.source_selection_inputs(capture, current, sha(current.read_bytes()), [root])
    assert any(row["path"] == str(other) for row in inputs["history_census"])
    assert all(str(other) not in row["custody"] for row in inputs["candidates"])
