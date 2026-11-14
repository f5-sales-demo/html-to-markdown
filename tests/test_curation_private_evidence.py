"""Independent review needs private rationale and every exact candidate stage."""

import copy
import json

import pytest
from test_curation_input_capacity import oversized_source
from test_curation_renewal import context, proposal, validation

from html_to_markdown.curation import json_bytes, load_curation_policy
from html_to_markdown.curation_renewal import compile_proposal, proposal_context
from html_to_markdown.curation_renewal_evidence import restore_candidate_stages
from html_to_markdown.curation_renewal_policy import policy_files, reviewed_policy
from html_to_markdown.curation_renewal_review import review_request, verify_validation


def private_proposal():
    original = context()
    source = proposal_context(
        original["metadata"],
        original["body"],
        {"a" * 64: {}},
        "b" * 64,
        load_curation_policy().sha256,
    )
    decision = proposal(source)
    for topic in decision["topics"]:
        topic["reason"] = "PRIVATE_RATIONALE_SENTINEL_197: " + topic["topic"]
        topic["media"] = [
            {
                "sha256": "a" * 64,
                "disposition": "keep",
                "reason": "PRIVATE_MEDIA_RATIONALE_197: retain necessary port and topology evidence.",
            }
        ]
        for removal in topic["removals"]:
            removal["reason"] = "PRIVATE_REMOVAL_RATIONALE_197: only the retired procedure."
    return source, decision


def test_validator_receives_exact_original_private_proposal():
    source, decision = private_proposal()
    candidate = compile_proposal(source, decision)
    payload = json.loads(review_request(source, [], candidate)["input"][0]["content"][0]["text"])
    assert candidate["proposal"] == decision
    assert payload["candidate"]["proposal"] == decision
    assert candidate["independent_validation_required"] is True


def test_private_rationale_never_enters_public_catalogs(tmp_path, monkeypatch):
    source, decision = private_proposal()
    candidate = compile_proposal(source, decision)
    directory = tmp_path / source["binding_sha256"]
    directory.mkdir()
    (directory / "visual.json").write_bytes(json_bytes([]))
    monkeypatch.setattr(
        "html_to_markdown.curation_renewal_policy.replay_renewal",
        lambda *args: {"candidate": candidate},
    )
    policy = reviewed_policy(tmp_path, [source])
    files = policy_files(policy.raw)
    assert files
    assert all(b"PRIVATE_" not in data for data in files.values())


def test_changing_private_rationale_invalidates_candidate_digest():
    source, decision = private_proposal()
    candidate = compile_proposal(source, decision)
    approved = validation(source, candidate)
    candidate["proposal"]["topics"][0]["reason"] += " changed"
    with pytest.raises(ValueError, match="candidate digest changed"):
        verify_validation(source, candidate, approved)


def test_oversized_stage_bodies_restore_every_character_and_private_reason():
    source, candidate = oversized_source()
    candidate["proposal"] = {"reason": "PRIVATE_COMPLETE_REASON_197 " * 4000}
    before = copy.deepcopy(candidate)
    request = review_request(source, [], candidate)
    payload = json.loads(request["input"][0]["content"][0]["text"])
    assert len(request["input"][0]["content"][0]["text"]) <= 1048576
    assert "candidate_stage_bodies" in payload
    for stage in payload["candidate"]["stages"]:
        for key in ("input_body", "output_body"):
            reference = stage[key]
            stage[key] = payload["candidate_stage_bodies"][reference["stage_body_sha256"]]
    assert payload["candidate"] == before
    assert candidate == before


def test_stale_stage_body_reference_is_rejected():
    payload = {
        "candidate": {
            "stages": [{"input_body": {"stage_body_sha256": "a" * 64}, "output_body": ""}]
        },
        "candidate_stage_bodies": {"a" * 64: "changed source body"},
    }
    with pytest.raises(ValueError, match="stage body digest"):
        restore_candidate_stages(payload)


def test_missing_stage_body_reference_is_rejected():
    payload = {
        "candidate": {
            "stages": [{"input_body": {"stage_body_sha256": "a" * 64}, "output_body": ""}]
        },
        "candidate_stage_bodies": {},
    }
    with pytest.raises(ValueError, match="stage body reference"):
        restore_candidate_stages(payload)
