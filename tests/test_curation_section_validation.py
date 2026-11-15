"""Complete scoped evidence cannot waive final document or exact payload validation."""

import json
from copy import deepcopy

import pytest
import test_curation_renewal as fixtures

from html_to_markdown.curation_renewal import compile_proposal, proposal_context
from html_to_markdown.curation_renewal_jobs import replay_renewal, run_renewal
from html_to_markdown.curation_renewal_review import review_request
from html_to_markdown.curation_section_validation import assignments, validate_sections
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.contracts import ResponseEvidence
from html_to_markdown.enrichment.gates import request_hash
from html_to_markdown.errors import TransportFailure


class Client:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    def execute(self, body):
        self.calls.append(body)
        payload = json.loads(body["input"][0]["content"][0]["text"])
        if body["metadata"]["content_review_role"] == "editor":
            return self.evidence(body, fixtures.proposal(payload))
        result = fixtures.validation(payload, payload["candidate"])
        selection = payload["independent_review_selection"]
        addresses = {item["address"] for item in payload["source_fact_blocks"]}
        result["facts"] = [f for f in result["facts"] if f["source_address"] in addresses]
        if self.failure == "final" and selection.get("final_document_review"):
            result.update(approved=False, failures=["action order changed across sections"])
        if self.failure == "incomplete" and result["facts"]:
            result["facts"].pop()
        if self.failure == "payload" and result["facts"]:
            result["facts"][0]["candidate_sha256"] = ["f" * 64]
        error = "interrupted" if self.failure == "interrupt" and len(self.calls) == 2 else None
        return self.evidence(body, result, error)

    @staticmethod
    def evidence(body, result, error=None):
        response = {
            "status": "completed",
            "model": body["model"],
            "output": [{"content": [{"type": "output_text", "text": json.dumps(result)}]}],
        }
        return ResponseEvidence(
            request_sha256=request_hash(body),
            response_sha256=sha(json.dumps(response, sort_keys=True, separators=(",", ":"))),
            model=body["model"],
            reasoning="high",
            prompt_version=body["instructions"].rsplit("Prompt version: ", 1)[-1],
            request=body,
            response=response,
            error=error,
            transport="codex_litellm",
        )


def inputs():
    source = fixtures.context()
    candidate = compile_proposal(source, fixtures.proposal(source))
    return source, review_request(source, [], candidate)


def test_scope_covers_every_fact_once_in_section_order():
    source, _ = inputs()
    sections = assignments(source, 1)
    addresses = [address for section in sections for address in section]
    assert len(addresses) == len(set(addresses)) == 5
    assert sections == assignments(source, 1)


def test_complete_source_and_final_review_are_preserved_and_replayable(tmp_path):
    source, body = inputs()
    before = deepcopy(body)
    client = Client()
    result = validate_sections(body, tmp_path, client)
    assert body == before
    assert len(result["validation"]["facts"]) == 5
    assert result["publication_approved"] is False
    assert client.calls[-1]["model"] == "gpt-6.1-sol"
    for call in client.calls:
        payload = json.loads(call["input"][0]["content"][0]["text"])
        assert payload["body"] == source["body"]
        assert payload["blocks"] == source["blocks"]
        assert payload["candidate"] == before_payload(before)["candidate"]
    assert validate_sections(body, tmp_path) == result


def before_payload(body):
    return json.loads(body["input"][0]["content"][0]["text"])


@pytest.mark.parametrize("failure", ["final", "incomplete", "payload"])
def test_partial_or_rejected_evidence_cannot_approve(failure, tmp_path):
    _, body = inputs()
    with pytest.raises(ValueError):
        validate_sections(body, tmp_path, Client(failure))
    assert not list(tmp_path.rglob("validated.json"))


def test_interruption_resumes_complete_sections_and_keeps_failed_response(tmp_path):
    _, body = inputs()
    first = Client("interrupt")
    with pytest.raises(TransportFailure):
        validate_sections(body, tmp_path, first)
    second = Client()
    result = validate_sections(body, tmp_path, second)
    assert len(second.calls) == len(result["sections"])
    assert list(tmp_path.rglob("history/*.json"))
    assert validate_sections(body, tmp_path) == result


def test_changed_policy_context_needs_fresh_validation(tmp_path):
    _, body = inputs()
    validate_sections(body, tmp_path, Client())
    payload = before_payload(body)
    payload["policy_sha256"] = "f" * 64
    body["input"][0]["content"][0]["text"] = json.dumps(payload)
    with pytest.raises(ValueError, match="unavailable"):
        validate_sections(body, tmp_path)


def test_large_renewal_resumes_incomplete_validation_without_new_editor(tmp_path):
    original = fixtures.context()
    body = (
        original["body"]
        + "\n## More technical facts\n\n"
        + "\n\n".join(
            f"Fact {i}: use port 8443 and verify the origin before the load balancer."
            for i in range(100)
        )
        + "\n"
    )
    source = proposal_context(original["metadata"], body, {}, "a" * 64, "b" * 64)
    first = Client("incomplete")
    with pytest.raises(ValueError, match="incomplete"):
        run_renewal(source, [], tmp_path, first)
    state = json.loads((tmp_path / source["binding_sha256"] / "state.json").read_bytes())
    assert state["attempt"] == 0 and state["feedback"] == [] and state["retryable"]
    assert sum(c["metadata"]["content_review_role"] == "editor" for c in first.calls) == 1
    second = Client()
    result = run_renewal(source, [], tmp_path, second)
    assert not any(c["metadata"]["content_review_role"] == "editor" for c in second.calls)
    assert len(result["preservation"]["facts"]) == 105
    assert result["section_validation"]["publication_approved"] is False
    assert replay_renewal(source, [], tmp_path) == result
