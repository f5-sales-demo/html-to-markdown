"""Unapproved compilation retains exact original envelopes and every source guard."""

# Exercise the shared unapproved rendering stage directly.
# pylint: disable=protected-access

import copy
import json

import pytest
from test_source_existing_context import call, prepared

from html_to_markdown import source_candidate as jobs
from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.source_existing_context import compile_source_proposal
from html_to_markdown.source_review_jobs import _compile


def test_checked_original_compiles_without_rebinding_or_granting_admission(capture, tmp_path):
    current, original, old, context = prepared(capture, tmp_path)
    qualified = call(capture, current, original)
    originals = current.read_bytes(), original.read_bytes(), copy.deepcopy(qualified)
    candidate, _, proposal, files = compile_source_proposal(
        capture, tmp_path / "candidate", current, original, qualified
    )
    assert proposal.binding_sha256 == old["binding_sha256"]
    assert candidate["output_sha256"] == sha(json_bytes(candidate["file_hashes"]))
    assert {name: sha(data) for name, data in files.items()} == candidate["file_hashes"]
    assert qualified["admission_approved"] is False
    assert qualified["new_editor_allowances"] == 0
    assert (current.read_bytes(), original.read_bytes(), qualified) == originals
    assert not (tmp_path / "candidate/reviewed").exists()
    with pytest.raises(ValueError, match="stale"):
        _compile(capture, tmp_path / "ordinary", old, context, proposal)


@pytest.mark.parametrize(
    "field", ["original_proposal", "source_binding", "input_controller_sha256"]
)
def test_qualification_claims_cannot_be_changed_before_compile(capture, tmp_path, field):
    current, original, _, _ = prepared(capture, tmp_path)
    qualified = call(capture, current, original)
    qualified[field] = {} if field != "input_controller_sha256" else "0" * 64
    with pytest.raises(ValueError, match="qualification"):
        compile_source_proposal(capture, tmp_path / "candidate", current, original, qualified)


def test_changed_input_during_compile_cannot_produce_a_candidate(capture, tmp_path, monkeypatch):
    current, original, _, _ = prepared(capture, tmp_path)
    qualified = call(capture, current, original)
    render = jobs.render_source_proposal

    def mutate(*args, **kwargs):
        result = render(*args, **kwargs)
        original.write_bytes(original.read_bytes() + b" ")
        return result

    monkeypatch.setattr(jobs, "render_source_proposal", mutate)
    with pytest.raises(ValueError, match="record digest"):
        compile_source_proposal(capture, tmp_path / "candidate", current, original, qualified)


def test_real_transformation_guards_still_apply(capture, tmp_path):
    current, original, _, _ = prepared(capture, tmp_path)
    value = json.loads(original.read_bytes())
    ev = value["phases"]["privacy"]
    decision = json.loads(ev["response"]["output"][0]["content"][0]["text"])
    decision["text_changes"] = [
        {
            "field": "body",
            "start": 0,
            "end": 5,
            "source_sha256": "f" * 64,
            "replacement": "Example",
            "reason": "Exact protected span",
        }
    ]
    ev["response"]["output"][0]["content"][0]["text"] = json.dumps(decision)
    ev["response_sha256"] = sha(json.dumps(ev["response"], sort_keys=True, separators=(",", ":")))
    original.write_bytes(json_bytes(value))
    qualified = call(capture, current, original)
    with pytest.raises(ValueError, match="digest guard"):
        compile_source_proposal(capture, tmp_path / "candidate", current, original, qualified)


def test_missing_original_cannot_be_bypassed_by_saved_qualification(capture, tmp_path):
    current, original, _, _ = prepared(capture, tmp_path)
    qualified = call(capture, current, original)
    original.unlink()
    with pytest.raises(FileNotFoundError):
        compile_source_proposal(capture, tmp_path / "candidate", current, original, qualified)
