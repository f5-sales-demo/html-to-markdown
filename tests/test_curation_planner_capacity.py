"""Actual-planner requests retain complete source and stage bodies within native limits."""

import json
from copy import deepcopy

import pytest
from test_curation_retained_notices import source_context, source_proposal

from html_to_markdown.curation_renewal import compile_proposal
from html_to_markdown.curation_renewal_evidence import restore_planner_candidate
from html_to_markdown.curation_renewal_publication import planner_request
from html_to_markdown.curation_renewal_review import ReviewCapacityRequired, fact_blocks


def planner_inputs(size=120000):
    source = source_context("# Mesh connectivity\n\n" + "x" * size + "\n")
    compiled = compile_proposal(source, source_proposal(source))
    reviewed = {"candidate": compiled, "independently_validated": True, "preservation": {}}
    final = {
        "identity": source["identity"],
        "binding_sha256": source["binding_sha256"],
        "metadata": source["metadata"],
        "body": compiled["body"],
        "disposition": "keep",
        "media": {},
        "findings": [],
    }
    qualification = {"qualification_sha256": "c" * 64, "post_planner_validation_required": True}
    return source, reviewed, qualification, final


def test_oversized_actual_planner_input_is_complete_losslessly_reconstructible():
    source, reviewed, qualification, candidate = planner_inputs()
    original = deepcopy(reviewed)
    body = planner_request(source, reviewed, qualification, candidate, [])
    text = body["input"][0]["content"][0]["text"]
    assert len(text) <= 1048576
    payload = json.loads(text)
    assert payload["source"] == source
    assert payload["candidate"] == candidate
    assert reviewed == original

    assert restore_planner_candidate(payload) == reviewed["candidate"]
    facts = fact_blocks(source["blocks"])
    assert len(payload["source_fact_blocks"]) == len(facts)
    for index, entry in enumerate(payload["source_fact_blocks"]):
        assert entry["source_block_index"] == index
        assert source["blocks"][index]["text"] == facts[index]["text"]
        assert entry["sha256"] == facts[index]["sha256"]
    mutated = deepcopy(payload)
    digest = next(iter(mutated["planner_candidate_stage_bodies"]))
    mutated["planner_candidate_stage_bodies"][digest] += "changed"
    with pytest.raises(ValueError, match="digest"):
        restore_planner_candidate(mutated)
    mutated = deepcopy(payload)
    mutated["reviewed_proposal"]["candidate"]["stages"][0]["input_body"] = {
        "stage_body_sha256": "0" * 64
    }
    with pytest.raises(ValueError, match="reference"):
        restore_planner_candidate(mutated)


def test_normal_planner_request_keeps_full_original_representations():
    source, reviewed, qualification, candidate = planner_inputs(40)
    payload = json.loads(
        planner_request(source, reviewed, qualification, candidate, [])["input"][0]["content"][0][
            "text"
        ]
    )
    assert payload["reviewed_proposal"] == reviewed
    assert payload["source_fact_blocks"] == fact_blocks(source["blocks"])
    assert "planner_candidate_stage_bodies" not in payload


def test_a_complete_planner_input_that_still_exceeds_capacity_fails_visibly():
    source, reviewed, qualification, candidate = planner_inputs(1500000)
    with pytest.raises(ReviewCapacityRequired):
        planner_request(source, reviewed, qualification, candidate, [])
