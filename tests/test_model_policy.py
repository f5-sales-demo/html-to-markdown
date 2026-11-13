"""Routine Sol reviews stay independent and legacy Astra evidence retains exact models."""

import json
from copy import deepcopy

import pytest

from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.contracts import VALIDATOR_MODEL, StrictModel
from html_to_markdown.enrichment.gates import request_hash
from html_to_markdown.enrichment.model_policy import (
    ANNOTATION,
    ASTRA_MODEL,
    POLICY_KEY,
    ROLE_KEY,
    compatible_request,
    escalate_validation,
    validate_new_request,
    validation_model,
)
from html_to_markdown.enrichment.requests import request


class ExampleValidation(StrictModel):
    passed: bool
    failures: list[str]
    unresolved: list[str]


def routine():
    return request(
        VALIDATOR_MODEL,
        "Independent complete-source review",
        {"source": "complete article"},
        ExampleValidation,
    )


def failed_evidence(body):
    response = {
        "status": "completed",
        "model": body["model"],
        "output": [
            {
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            {
                                "passed": False,
                                "failures": ["necessary media relationship unresolved"],
                                "unresolved": [],
                            }
                        ),
                    }
                ]
            }
        ],
        "events": [],
    }
    return {
        "model": body["model"],
        "reasoning": "high",
        "transport": "codex_litellm",
        "error": None,
        "request": body,
        "request_sha256": request_hash(body),
        "response": response,
        "response_sha256": sha(json.dumps(response, sort_keys=True, separators=(",", ":"))),
    }


def test_sol_high_default_has_explicit_independent_validator_role():
    """Default roles are bound separately from the shared model identity."""
    body = routine()
    assert body["model"] == "gpt-6.1-sol" and body["reasoning"] == {"effort": "high"}
    assert body["metadata"][ROLE_KEY] == "validator"
    assert body["metadata"][POLICY_KEY] == "2"
    assert body["instructions"].endswith("Prompt version: article-enrichment/1")
    assert validation_model(body) == "gpt-6.1-sol"


def test_same_model_proposal_and_validator_have_different_cache_keys():
    editor = request(
        "gpt-6.1-sol", "Review", {"source": "complete"}, ExampleValidation, role="editor"
    )
    validator = request(
        "gpt-6.1-sol", "Review", {"source": "complete"}, ExampleValidation, role="validator"
    )
    assert request_hash(editor) != request_hash(validator)
    with pytest.raises(ValueError, match="role"):
        validation_model(editor)


def test_legacy_expected_request_is_reconstructed_without_rebinding_evidence():
    current = routine()
    old = deepcopy(current)
    old["instructions"] = old["instructions"].replace(ANNOTATION + "validator", "")
    old.pop("metadata")
    old["model"] = ASTRA_MODEL
    original = deepcopy(old)
    expected = compatible_request(current, old)
    assert expected == old == original
    assert validation_model(old) == ASTRA_MODEL
    wrong = deepcopy(old)
    wrong["model"] = "gpt-6.1-sol"
    with pytest.raises(ValueError, match="wrong model"):
        compatible_request(current, wrong)


def test_astra_requires_completed_failed_sol_evidence_and_keeps_original_unchanged():
    body = routine()
    prior = failed_evidence(body)
    original = deepcopy(prior)
    escalated = escalate_validation(
        body, "Resolve this demonstrated media-preservation failure", prior
    )
    assert escalated["model"] == ASTRA_MODEL and prior == original
    assert validation_model(escalated) == ASTRA_MODEL
    validate_new_request(escalated)
    assert compatible_request(body, escalated) == escalated
    assert json.loads(escalated["input"][0]["content"][0]["text"])["source"] == "complete article"
    with pytest.raises(ValueError, match="exception"):
        validate_new_request({**body, "model": ASTRA_MODEL})


def test_escalation_preserves_exact_unicode_request_hash():
    body = request(
        VALIDATOR_MODEL,
        "Independently validate the complete article",
        {"source": "Réessayez après 30 secondes; preserve → destination."},
        ExampleValidation,
        role="validator",
    )
    prior = failed_evidence(body)
    escalated = escalate_validation(body, "Resolve the demonstrated relationship failure", prior)
    assert validation_model(escalated) == ASTRA_MODEL
    assert compatible_request(body, escalated) == escalated


def test_passing_validation_with_findings_does_not_authorize_astra():
    body = routine()
    prior = failed_evidence(body)
    result = json.loads(prior["response"]["output"][0]["content"][0]["text"])
    result["passed"] = True
    prior["response"]["output"][0]["content"][0]["text"] = json.dumps(result)
    prior["response_sha256"] = sha(
        json.dumps(prior["response"], sort_keys=True, separators=(",", ":"))
    )
    with pytest.raises(ValueError, match="specific unresolved"):
        escalate_validation(body, "Claimed difficult exception", prior)


@pytest.mark.parametrize(
    "change", ["transport_failure", "wrong_source", "complete_success", "tool_use"]
)
def test_ungrounded_escalation_cannot_spend_astra(change):
    body = routine()
    prior = failed_evidence(body)
    if change == "transport_failure":
        prior["error"] = "HTTP 503"
    elif change == "wrong_source":
        prior["request_sha256"] = "0" * 64
    elif change == "complete_success":
        prior["response"]["output"][0]["content"][0]["text"] = json.dumps(
            {"passed": True, "failures": [], "unresolved": []}
        )
        prior["response_sha256"] = sha(
            json.dumps(prior["response"], sort_keys=True, separators=(",", ":"))
        )
    else:
        prior["response"]["events"] = [{"item": {"type": "command_execution"}}]
        prior["response_sha256"] = sha(
            json.dumps(prior["response"], sort_keys=True, separators=(",", ":"))
        )
    with pytest.raises(ValueError):
        escalate_validation(body, "Claimed difficult exception", prior)
