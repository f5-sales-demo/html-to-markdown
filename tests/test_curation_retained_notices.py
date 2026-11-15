"""Compiled limitation notices remain proposals requiring independent review."""

from pathlib import Path

import pytest
from test_curation_renewal import validation

from html_to_markdown.curation import json_bytes, load_curation_policy
from html_to_markdown.curation_renewal import TOPICS, compile_proposal, proposal_context
from html_to_markdown.curation_renewal_review import review_request, verify_validation
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.models import PageMetadata

BODY = (
    "# Mesh connectivity\n\n"
    "> **Note:** Site as pods supports only Mesh functionalities and does not support "
    "F5® Distributed Cloud App Stack. The supported Kubernetes version is 1.23.\n\n"
    "1. Deploy the Mesh site.\n2. Verify the tunnel.\n"
)


def source_context(body: str = BODY):
    metadata = PageMetadata(
        sourceId="docs-cloud-f5-com",
        title="Mesh connectivity",
        slug="mesh-connectivity",
        url="https://docs.cloud.f5.com/docs-v2/how-to/mesh-connectivity",
        category="Guides",
    ).model_dump(by_alias=True)
    return proposal_context(metadata, body, {}, "a" * 64, load_curation_policy().sha256)


def source_proposal(context):
    return {
        "identity": context["identity"],
        "binding_sha256": context["binding_sha256"],
        "topics": [
            {
                "topic": topic,
                "disposition": "keep",
                "reason": "Mesh-only limitation requires Kubernetes 1.23 and no retired site step.",
                "removals": [],
                "media": [],
                "reference_edits": [],
            }
            for topic in TOPICS
        ],
        "unresolved": [],
    }


def test_compiler_keeps_exact_limit_notice_as_an_unapproved_guarded_candidate():
    source = source_context()
    proposal = source_proposal(source)
    compiled = compile_proposal(source, proposal)
    stage = next(s for s in compiled["stages"] if s["topic"] == "appstack-retired")
    block = next(b for b in source["blocks"] if b["kind"] == "paragraph" and "1.23" in b["text"])
    assert stage["document"]["retained_notices"] == [
        {"location": block["address"], "sha256": block["sha256"]}
    ]
    assert compiled["body"] == BODY
    assert compiled["independent_validation_required"]
    assert "renewal_validation_sha256" not in stage["document"]
    body = review_request(source, [], compiled)
    payload = body["input"][0]["content"][0]["text"]
    assert "retained_notices" in payload
    assert "independently" in body["instructions"].lower()
    assert sha(json_bytes(compiled["proposal"])) == compiled["proposal_sha256"]
    approved = validation(source, compiled)
    verify_validation(source, compiled, approved)
    approved["retirement_valid"] = False
    with pytest.raises(ValueError):
        verify_validation(source, compiled, approved)


def test_retirement_or_removed_notice_does_not_get_a_retention_guard():
    source = source_context(BODY.replace("does not support", "requires"))
    compiled = compile_proposal(source, source_proposal(source))
    assert not next(s for s in compiled["stages"] if s["topic"] == "appstack-retired")[
        "document"
    ].get("retained_notices")
    source = source_context()
    proposal = source_proposal(source)
    next(t for t in proposal["topics"] if t["topic"] == "appstack-retired")["disposition"] = (
        "remove"
    )
    compiled = compile_proposal(source, proposal)
    assert not next(s for s in compiled["stages"] if s["topic"] == "appstack-retired")[
        "document"
    ].get("retained_notices")


def test_appstack_controller_change_invalidates_the_complete_review_context(monkeypatch):
    source = source_context()
    original = Path.read_bytes

    def changed(path):
        data = original(path)
        return data + b"\n# changed semantics\n" if path.name == "appstack_curation.py" else data

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert source_context()["controller_sha256"] != source["controller_sha256"]
    with pytest.raises(ValueError, match="stale"):
        compile_proposal(source, source_proposal(source))
