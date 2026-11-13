"""Current source admission keeps visible descriptions and exact per-phase identities."""

import json

import pytest

from html_to_markdown.content_policy import _description
from html_to_markdown.source_phase_bindings import privacy_context
from html_to_markdown.source_review import AdmissionValidation, admission_request


@pytest.mark.parametrize("empty", ["\u200b", "\ufeff", "\u200c\u200d", "\u2060 \u200b"])
def test_description_skips_invisible_paragraphs_and_keeps_the_complete_source(empty):
    paragraph = "Configure the HTTP response header. Keep the 8443 listener port unchanged."
    assert _description(f"# Header configuration\n\n{empty}\n\n{paragraph}\n") == paragraph
    assert _description(f"# Heading\n\n{empty}\n") is None
    assert (
        _description("# Heading\n\nVisible co\u200dntent stays exact.\n")
        == "Visible co\u200dntent stays exact."
    )


def context():
    payload = {
        "identity": "doc",
        "binding_sha256": "a" * 64,
        "binding": {
            "identity": "doc",
            "source_sha256": "b" * 64,
            "curation_policy_sha256": "c" * 64,
            "api_policy_sha256": "d" * 64,
        },
        "relevance_decision": {"binding_sha256": "a" * 64},
        "candidate": {},
        "privacy_decision": {},
    }
    payload["privacy_decision"]["binding_sha256"] = privacy_context(payload)["binding_sha256"]
    return payload


def test_validation_supplies_the_exact_distinct_privacy_and_relevance_bindings():
    source = context()
    body = admission_request("gpt-6-astra", "validation", source, AdmissionValidation)
    payload = json.loads(body["input"][0]["content"][0]["text"])
    links = payload["phase_bindings"]
    assert links["source_review_sha256"] == links["relevance_sha256"] == source["binding_sha256"]
    assert links["source_privacy"]["sha256"] == source["privacy_decision"]["binding_sha256"]
    assert links["source_privacy"]["binding"] == privacy_context(source)["binding"]
    assert links["source_privacy"]["sha256"] != links["source_review_sha256"]
    assert "phase_bindings" not in source


@pytest.mark.parametrize("phase", ["relevance_decision", "privacy_decision"])
def test_stale_phase_is_rejected_before_inference(phase):
    source = context()
    source[phase]["binding_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="stale.*linkage"):
        admission_request("gpt-6-astra", "validation", source, AdmissionValidation)
