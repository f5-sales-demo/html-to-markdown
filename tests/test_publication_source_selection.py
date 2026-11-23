"""Source selection preserves verification, fallbacks and visible historical inputs."""

import copy
import json
from pathlib import Path

import pytest

from html_to_markdown import publication_source_selection as selection
from html_to_markdown import source_inputs
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.source_inputs import MARKER, read_source_metadata


def source_input(
    root: Path, queue: str, when: str, *, source: str = "my-f5-com", settled: int = 1
) -> tuple[Path, str, dict]:
    inventory = {
        "binding": {"source": source},
        "ended_at": when,
        "completeness": {"enumeration": True, "capture": True},
    }
    captured = json_bytes(inventory)
    receipt = {
        "schema_version": 1,
        "source": source,
        "capture_binding": inventory["binding"],
        "inventory_sha256": sha(captured),
        "ended_at": when,
        "enumeration_complete": True,
        "capture_complete": True,
        "documents": [{"disposition": "accepted"}] * settled + [{"disposition": "queued"}],
        "test_marker": queue,
    }
    payload = json_bytes(receipt)
    pin = sha(payload)
    directory = root / ".reviewed" / queue / source / pin
    (directory / ".admission/capture").mkdir(parents=True)
    (directory / ".admission/capture/inventory.json").write_bytes(captured)
    (directory / MARKER).write_bytes(payload)
    return directory, pin, receipt


def verifier(monkeypatch, rejected=()):
    calls = []

    def verify(directory, digest):
        calls.append(digest)
        assert sha((directory / MARKER).read_bytes()) == digest
        if digest in rejected:
            raise ValueError("independent source admission failed")
        return json.loads((directory / MARKER).read_bytes())

    monkeypatch.setattr(selection, "verify_source", verify)
    return calls


def test_obsolete_history_never_replays_after_newer_input_passes(tmp_path, monkeypatch):
    old = source_input(tmp_path, "old", "2026-11-01T00:00:00Z")
    current = source_input(tmp_path, "current", "2026-11-23T00:00:00Z")
    calls = verifier(monkeypatch, rejected=[old[1]])
    selected, rejected, superseded = selection.select_source_inputs(tmp_path)
    assert selected == {"my-f5-com": current[:2]}
    assert calls == [current[1]]
    assert rejected == []
    assert superseded == [
        {
            "input_sha256": old[1],
            "queue": "old/my-f5-com",
            "reason": "source_input_superseded",
            "superseding_input_sha256": current[1],
        }
    ]
    assert (old[0] / MARKER).exists()
    assert selection.select_source_inputs(tmp_path) == (selected, rejected, superseded)


def test_invalid_newer_input_does_not_hide_verified_fallback(tmp_path, monkeypatch):
    older = source_input(tmp_path, "older", "2026-11-01T00:00:00Z")
    fallback = source_input(tmp_path, "fallback", "2026-11-20T00:00:00Z")
    current = source_input(tmp_path, "current", "2026-11-23T00:00:00Z")
    calls = verifier(monkeypatch, rejected=[current[1]])
    selected, rejected, superseded = selection.select_source_inputs(tmp_path)
    assert selected == {"my-f5-com": fallback[:2]}
    assert calls == [current[1], fallback[1]]
    assert rejected[0]["input_sha256"] == current[1]
    assert rejected[0]["reason"] == "source_input_verification_failed"
    assert superseded[0]["input_sha256"] == older[1]


def test_sources_progress_independently_and_ties_preserve_first_path(tmp_path, monkeypatch):
    first = source_input(tmp_path, "a", "2026-11-23T00:00:00Z", settled=2)
    source_input(tmp_path, "z", "2026-11-23T00:00:00Z", settled=2)
    source_input(tmp_path, "newer-fewer", "2026-11-23T00:00:00Z", settled=1)
    docs = source_input(tmp_path, "docs", "2026-11-22T00:00:00Z", source="docs-cloud-f5-com")
    calls = verifier(monkeypatch)
    selected, rejected, superseded = selection.select_source_inputs(tmp_path)
    assert selected == {"my-f5-com": first[:2], "docs-cloud-f5-com": docs[:2]}
    assert calls == [docs[1], first[1]]
    assert rejected == [] and len(superseded) == 2


@pytest.mark.parametrize("field", ["source", "ended_at", "documents"])
def test_changed_full_verification_metadata_cannot_win_selection(tmp_path, monkeypatch, field):
    fallback = source_input(tmp_path, "fallback", "2026-11-01T00:00:00Z")
    current = source_input(tmp_path, "current", "2026-11-23T00:00:00Z")

    def changed(directory, digest):
        receipt = json.loads((directory / MARKER).read_bytes())
        if digest == current[1]:
            receipt[field] = {
                "source": "docs-cloud-f5-com",
                "ended_at": "2026-11-24T00:00:00Z",
                "documents": [{"disposition": "queued"}],
            }[field]
        return receipt

    monkeypatch.setattr(selection, "verify_source", changed)
    selected, rejected, superseded = selection.select_source_inputs(tmp_path)
    assert selected == {"my-f5-com": fallback[:2]}
    assert rejected[0]["input_sha256"] == current[1]
    assert superseded == []


@pytest.mark.parametrize("field", ["source", "ended_at", "capture_binding", "capture_complete"])
def test_rank_metadata_must_match_digest_bound_capture(tmp_path, field):
    directory, _, receipt = source_input(tmp_path, "current", "2026-11-23T00:00:00Z")
    receipt[field] = {
        "source": "docs-cloud-f5-com",
        "ended_at": "2026-11-24T00:00:00Z",
        "capture_binding": {"source": "docs-cloud-f5-com"},
        "capture_complete": False,
    }[field]
    raw = json_bytes(receipt)
    (directory / MARKER).write_bytes(raw)
    with pytest.raises(ValueError, match="capture metadata changed"):
        read_source_metadata(directory, sha(raw))


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("source", "unknown"),
        ("documents", {}),
        ("ended_at", "later"),
    ],
)
def test_malformed_metadata_cannot_create_a_rank(tmp_path, field, value):
    directory, _, receipt = source_input(tmp_path, "current", "2026-11-23T00:00:00Z")
    receipt[field] = value
    raw = json_bytes(receipt)
    (directory / MARKER).write_bytes(raw)
    with pytest.raises(ValueError):
        read_source_metadata(directory, sha(raw))


@pytest.mark.parametrize("member", [MARKER, ".admission/capture/inventory.json"])
def test_metadata_links_are_rejected(tmp_path, member):
    directory, pin, _ = source_input(tmp_path, "current", "2026-11-23T00:00:00Z")
    original = directory / member
    outside = tmp_path / "outside.json"
    original.rename(outside)
    original.symlink_to(outside)
    with pytest.raises(ValueError, match="link"):
        read_source_metadata(directory, pin)


def test_missing_capture_and_duplicate_metadata_never_grant_selection(tmp_path, monkeypatch):
    directory, _, _ = source_input(tmp_path, "missing", "2026-11-23T00:00:00Z")
    (directory / ".admission/capture/inventory.json").unlink()
    calls = verifier(monkeypatch)
    selected, rejected, superseded = selection.select_source_inputs(tmp_path)
    assert selected == {} and len(rejected) == 1 and superseded == [] and calls == []
    raw = b'{"schema_version":1,"schema_version":1}'
    (directory / MARKER).write_bytes(raw)
    with pytest.raises(ValueError, match="duplicate"):
        read_source_metadata(directory, sha(raw))


def test_input_directory_pin_and_ancestor_links_cannot_be_bypassed(tmp_path, monkeypatch):
    directory, _, _ = source_input(tmp_path, "current", "2026-11-23T00:00:00Z")
    verifier(monkeypatch)
    altered = directory.with_name("0" * 64)
    directory.rename(altered)
    with pytest.raises(ValueError, match="directory does not match"):
        selection.select_source_inputs(tmp_path)
    actual = altered.parent
    outside = tmp_path / "outside"
    actual.rename(outside)
    actual.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="link"):
        selection.select_source_inputs(tmp_path)


def test_unselected_receipt_metadata_still_must_be_grounded(tmp_path, monkeypatch):
    directory, _, receipt = source_input(tmp_path, "old", "2026-11-01T00:00:00Z")
    current = source_input(tmp_path, "current", "2026-11-23T00:00:00Z")
    forged = copy.deepcopy(receipt)
    forged["ended_at"] = "2026-11-02T00:00:00Z"
    raw = json_bytes(forged)
    (directory / MARKER).write_bytes(raw)
    directory.rename(directory.with_name(sha(raw)))
    calls = verifier(monkeypatch)
    selected, rejected, superseded = selection.select_source_inputs(tmp_path)
    assert selected == {"my-f5-com": current[:2]} and calls == [current[1]]
    assert rejected[0]["queue"] == "old/my-f5-com" and superseded == []


def test_oversized_metadata_is_preserved_without_admission(tmp_path, monkeypatch):
    directory, pin, _ = source_input(tmp_path, "current", "2026-11-23T00:00:00Z")
    original = (directory / MARKER).read_bytes()
    monkeypatch.setattr(source_inputs, "MAX_METADATA_BYTES", len(original) - 1)
    with pytest.raises(ValueError, match="size bound"):
        read_source_metadata(directory, pin)
    assert (directory / MARKER).read_bytes() == original


def test_all_failed_inputs_stay_visible_without_selecting_unverified_content(tmp_path, monkeypatch):
    first = source_input(tmp_path, "first", "2026-11-01T00:00:00Z")
    second = source_input(tmp_path, "second", "2026-11-23T00:00:00Z")
    calls = verifier(monkeypatch, rejected=[first[1], second[1]])
    selected, rejected, superseded = selection.select_source_inputs(tmp_path)
    assert selected == {} and superseded == []
    assert {entry["input_sha256"] for entry in rejected} == {first[1], second[1]}
    assert calls == [second[1], first[1]]
