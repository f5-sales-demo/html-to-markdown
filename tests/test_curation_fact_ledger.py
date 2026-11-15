"""Lossless ledger indexing must preserve every independently checked decision."""

from copy import deepcopy

import pytest
import test_curation_renewal as fixtures

from html_to_markdown.curation import json_bytes
from html_to_markdown.curation_fact_ledger import index_ledger, restore_ledger, sizing_ledger
from html_to_markdown.curation_renewal import compile_proposal
from html_to_markdown.curation_renewal_review import candidate_fact_blocks, fact_blocks
from html_to_markdown.enrichment.analysis import sha


def inputs():
    context = fixtures.context()
    candidate = compile_proposal(context, fixtures.proposal(context))
    facts = fixtures.validation(context, candidate)["facts"]
    payload = {
        "blocks": context["blocks"],
        "candidate_blocks": candidate_fact_blocks(candidate["body"]),
    }
    return context, candidate, facts, payload


def test_round_trip_preserves_order_removals_decisions_and_exact_witnesses():
    _, _, facts, payload = inputs()
    before = deepcopy(payload)
    index_ledger(payload, facts)
    assert restore_ledger(payload) == facts
    assert payload["blocks"] == before["blocks"]
    assert payload["candidate_blocks"] == before["candidate_blocks"]
    assert payload["independent_fact_review_ledger_sha256"] == sha(json_bytes(facts))
    assert len(json_bytes(payload["independent_fact_review_ledger"])) < len(json_bytes(facts))
    assert any(not f["necessary"] and not f["candidate_addresses"] for f in facts)


@pytest.mark.parametrize("side", ["source", "candidate"])
def test_index_rejects_changed_exact_fact_digest(side):
    _, _, facts, payload = inputs()
    if side == "source":
        facts[0]["source_sha256"] = "f" * 64
    else:
        facts[0]["candidate_sha256"][0] = "f" * 64
    with pytest.raises(ValueError, match="digest changed"):
        index_ledger(payload, facts)


@pytest.mark.parametrize("change", ["digest", "decision", "source", "candidate", "order"])
def test_restore_rejects_tampering_without_rebinding_the_original_digest(change):
    _, _, facts, payload = inputs()
    index_ledger(payload, facts)
    if change == "digest":
        payload["independent_fact_review_ledger_sha256"] = "f" * 64
    elif change == "decision":
        payload["independent_fact_review_ledger"][0]["necessary"] = False
    elif change == "order":
        payload["independent_fact_review_ledger"].reverse()
    else:
        field = "source_block_index" if change == "source" else "candidate_block_indices"
        entry = payload["independent_fact_review_ledger"][0]
        blocks = (
            fact_blocks(payload["blocks"]) if change == "source" else payload["candidate_blocks"]
        )
        position = entry[field] if change == "source" else entry[field][0]
        blocks[position]["sha256"] = "f" * 64
        if change == "source":
            # fact_blocks resolves coincident addresses without mutating the supplied blocks.
            payload["blocks"][position]["sha256"] = "f" * 64
    with pytest.raises(ValueError, match="reconstruction digest changed"):
        restore_ledger(payload)


@pytest.mark.parametrize("index", [-1, 999, True, "0", 1.5])
@pytest.mark.parametrize("side", ["source", "candidate"])
def test_restore_rejects_noninteger_negative_and_out_of_range_indices(index, side):
    _, _, facts, payload = inputs()
    index_ledger(payload, facts)
    entry = payload["independent_fact_review_ledger"][0]
    entry["source_block_index" if side == "source" else "candidate_block_indices"] = (
        index if side == "source" else [index]
    )
    with pytest.raises(ValueError, match="block index"):
        restore_ledger(payload)


def test_duplicate_candidate_witnesses_are_rejected_before_indexing():
    _, _, facts, payload = inputs()
    fact = facts[0]
    fact["candidate_addresses"] *= 2
    fact["candidate_sha256"] *= 2
    with pytest.raises(ValueError, match="duplicated"):
        index_ledger(payload, facts)


def test_representation_version_must_be_known():
    _, _, facts, payload = inputs()
    index_ledger(payload, facts)
    payload["independent_fact_review_ledger_representation"] = "unknown/999"
    with pytest.raises(ValueError, match="representation"):
        restore_ledger(payload)


def test_duplicate_indexed_witnesses_cannot_be_approved_by_a_recomputed_digest():
    _, _, facts, payload = inputs()
    index_ledger(payload, facts)
    payload["independent_fact_review_ledger"][0]["candidate_block_indices"] *= 2
    facts[0]["candidate_addresses"] *= 2
    facts[0]["candidate_sha256"] *= 2
    payload["independent_fact_review_ledger_sha256"] = sha(json_bytes(facts))
    with pytest.raises(ValueError, match="duplicated"):
        restore_ledger(payload)


def test_sizing_is_complete_unapproved_and_includes_all_exact_matches():
    context, candidate, _, payload = inputs()
    sizing = sizing_ledger(context, candidate)
    assert len(sizing) == 5
    assert all(not f["necessary"] and not f["preserved"] for f in sizing)
    index_ledger(payload, sizing)
    assert restore_ledger(payload) == sizing
    for fact in sizing:
        assert len(fact["candidate_addresses"]) == len(fact["candidate_sha256"])
