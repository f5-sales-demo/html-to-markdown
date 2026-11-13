"""Actual planner requests need selected qualified API records, not just policy hashes."""

import json
from copy import deepcopy

import pytest

from html_to_markdown.content_policy import load_content_policy, rewrite_text
from html_to_markdown.curation import load_curation_policy
from html_to_markdown.curation_api_evidence import api_reference_evidence
from html_to_markdown.curation_renewal import TOPICS, compile_proposal, proposal_context
from html_to_markdown.curation_renewal_publication import planner_request
from html_to_markdown.models import PageMetadata

ALIAS = "https://docs.cloud.f5.com/docs-v2/docs/api/endpoint"
IDENTITY = "https://my.f5.com/manage/s/article/K000123456"


def inputs():
    active = load_content_policy()
    body = "# Endpoint configuration\n\nUse the [endpoint API](" + ALIAS + ").\n"
    metadata = PageMetadata(
        sourceId="my-f5-com",
        url=IDENTITY,
        title="Endpoint configuration",
        slug="endpoint-configuration",
        category="knowledge",
    ).model_dump(by_alias=True)
    context = proposal_context(metadata, body, {}, "a" * 64, load_curation_policy().sha256)
    proposal = {
        "identity": IDENTITY,
        "binding_sha256": context["binding_sha256"],
        "topics": [
            {
                "topic": t,
                "disposition": "keep",
                "reason": "Preserve independent endpoint configuration.",
                "removals": [],
                "media": [],
                "reference_edits": [],
            }
            for t in TOPICS
        ],
        "unresolved": [],
    }
    reviewed = {"candidate": compile_proposal(context, proposal)}
    final, _ = rewrite_text(body, IDENTITY, active)
    candidate = {
        "body": final,
        "identity": IDENTITY,
        "binding_sha256": context["binding_sha256"],
        "disposition": "keep",
        "metadata": metadata,
        "media": {},
        "findings": [],
    }
    qualification = {"qualification_sha256": "c" * 64, "api_policy_sha256": active.policy_digest}
    return context, reviewed, candidate, qualification, active


def test_planner_supplies_only_relevant_qualified_api_records():
    context, reviewed, candidate, qualification, active = inputs()
    body = planner_request(context, reviewed, qualification, candidate, [])
    payload = json.loads(body["input"][0]["content"][0]["text"])
    evidence = payload["api_reference_evidence"]
    target = active.resolve(ALIAS)["destination"]
    assert evidence["policy_sha256"] == active.policy_digest
    assert evidence["catalog_sha256"] == active.catalog_digest
    assert evidence["policy"] == active.raw
    assert {row["url"] for row in evidence["targets"]} == {target}
    assert all(row["qualification"]["status"] == 200 for row in evidence["targets"])
    assert evidence["mappings"][0]["legacy_url"] == ALIAS
    assert evidence["operations"] and len(evidence["operations"]) < len(active.operations)
    assert {op["url"] for op in evidence["operations"]} == set(active.resolve(ALIAS)["candidates"])
    assert evidence["specification"] == active.catalog["specification"]


@pytest.mark.parametrize("fault", ["qualification", "policy", "operation", "target"])
def test_missing_or_tampered_api_evidence_cannot_reach_independent_review(fault):
    context, reviewed, candidate, _, original = inputs()
    active = deepcopy(original)
    if fault == "qualification":
        active.catalog["qualification"] = []
    elif fault == "policy":
        active.raw["exclusions"] = []
    elif fault == "operation":
        active.operations[0]["method"] = "DELETE"
    else:
        candidate["body"] += (
            "\nSee https://f5-sales-demo.github.io/api-specs-enriched/api-reference/missing/.\n"
        )
    with pytest.raises(ValueError):
        api_reference_evidence(context, reviewed, candidate, active)


def test_request_needs_the_exact_qualification_policy_pin():
    context, reviewed, candidate, qualification, _ = inputs()
    qualification["api_policy_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="API policy"):
        planner_request(context, reviewed, qualification, candidate, [])
