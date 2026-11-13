"""Complete overlapping source spans must fit without losing guarded payloads."""

import copy
import json

import pytest
from test_curation_renewal import context, proposal
from test_curation_renewal_publication import Client

from html_to_markdown import curation_renewal_jobs as jobs
from html_to_markdown.curation_renewal import TOPICS, compile_proposal, proposal_context
from html_to_markdown.curation_renewal_review import (
    ReviewCapacityRequired,
    fact_blocks,
    review_request,
)
from html_to_markdown.enrichment.analysis import sha


def oversized_source():
    source = context()
    body = (
        "```sh\ncurl 'https://example.com/$EXAMPLE_PASSWORD'\n```\n\n"
        + "Required limit 8443. " * 1800
    )
    source = proposal_context(source["metadata"], body, {}, "c" * 64, source["policy_sha256"])
    decision = {
        "identity": source["identity"],
        "binding_sha256": source["binding_sha256"],
        "topics": [
            {
                "topic": topic,
                "disposition": "keep",
                "reason": "Independent command and limit remain necessary.",
                "removals": [],
                "media": [],
                "reference_edits": [],
            }
            for topic in TOPICS
        ],
        "unresolved": [],
    }
    candidate = compile_proposal(source, decision)
    source["blocks"] = [
        {
            "address": "## Procedure@1:section",
            "sha256": sha(body),
            "kind": "section",
            "start": 0,
            "end": len(body),
            "text": body,
        }
        for _ in range(12)
    ]
    return source, candidate


def test_oversized_validator_indexes_every_complete_coincident_source_span():
    source, candidate = oversized_source()
    before = copy.deepcopy((source, candidate))
    visual = [{"type": "input_text", "text": "Image digest: " + "a" * 64}]
    body = review_request(source, visual, candidate)
    text = body["input"][0]["content"][0]["text"]
    assert (
        len(
            "\n\n".join(c["text"] for c in body["input"][0]["content"] if c["type"] == "input_text")
        )
        <= 1048576
    )
    payload = json.loads(text)
    expected = fact_blocks(source["blocks"])
    assert len(payload["source_fact_blocks"]) == len(expected)
    for index, (actual, original) in enumerate(
        zip(payload["source_fact_blocks"], expected, strict=True)
    ):
        restored = {k: v for k, v in actual.items() if k != "source_block_index"}
        restored["text"] = payload["blocks"][actual["source_block_index"]]["text"]
        assert actual["source_block_index"] == index
        assert restored == original
        assert sha(restored["text"]) == restored["sha256"]
    assert payload["body"] == source["body"]
    assert payload["candidate"] == candidate
    assert body["input"][0]["content"][1:] == visual
    assert (source, candidate) == before


def test_normal_requests_keep_historical_complete_fact_payload():
    source = context()
    candidate = compile_proposal(source, proposal(source))
    body = review_request(source, [], candidate)
    payload = json.loads(body["input"][0]["content"][0]["text"])
    assert payload["source_fact_blocks"] == fact_blocks(source["blocks"])
    assert all("source_block_index" not in block for block in payload["source_fact_blocks"])


def test_capacity_failure_never_truncates_a_single_large_source():
    source, candidate = oversized_source()
    source["evidence"] = {"source_article_html": "Necessary source character. " * 60000}
    with pytest.raises(ValueError, match="complete curation.*capacity"):
        review_request(source, [], candidate)


def test_oversized_indexing_rejects_changed_source_digest():
    source, candidate = oversized_source()
    source["blocks"][0]["sha256"] = "e" * 64
    with pytest.raises(ValueError, match="source fact digest"):
        review_request(source, [], candidate)


def test_capacity_queue_preserves_attempt_and_does_not_spend_semantic_corrections(
    tmp_path, monkeypatch
):
    source = context()
    decision = proposal(source)
    client = Client(source, decision)
    original = jobs.review_request

    def capacity_request(context, visual, candidate=None, feedback=None):
        if candidate is not None:
            raise ReviewCapacityRequired("complete curation review exceeds native input capacity")
        return original(context, visual, candidate, feedback)

    monkeypatch.setattr(jobs, "review_request", capacity_request)
    with pytest.raises(ReviewCapacityRequired):
        jobs.run_renewal(source, [], tmp_path, client)
    state = json.loads((tmp_path / source["binding_sha256"] / "state.json").read_bytes())
    assert state["attempt"] == 0 and state["feedback"] == []
    assert state["status"] == "queued" and state["retryable"] is False
    assert len(client.calls) == 1
    assert (tmp_path / source["binding_sha256"] / "attempt-000/editor.json").is_file()
