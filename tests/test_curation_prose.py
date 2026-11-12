"""Smallest retirement scope must retain the independently necessary condition."""

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from html_to_markdown.curation import blocks, remove_blocks
from html_to_markdown.curation_renewal import (
    TOPICS,
    compile_proposal,
    projected_fact,
    proposal_context,
)
from html_to_markdown.curation_renewal_review import review_request
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.models import PageMetadata

RETIRED = "Delegate the domain to Volterra. "
NECESSARY = "Volterra checks the configured domain and verifies tenant ownership."
BODY = "# Procedure\n\n" + RETIRED + NECESSARY + "\n\nCreate the origin pool on port 80.\n"


def source():
    metadata = PageMetadata(
        sourceId="my-f5-com",
        url="https://my.f5.com/manage/s/article/K000123456",
        title="Procedure",
        slug="procedure",
        category="knowledge",
    ).model_dump(by_alias=True)
    return proposal_context(metadata, BODY, {}, "a" * 64, "b" * 64)


def rule(body=BODY):
    block = next(b for b in blocks(body) if b.kind == "paragraph" and RETIRED in b.text)
    return {
        "location": block.location,
        "sha256": block.sha256,
        "prose_span": {"start": 0, "end": len(RETIRED), "sha256": sha(RETIRED)},
    }


def decision(context):
    block = next(b for b in context["blocks"] if b["kind"] == "paragraph" and RETIRED in b["text"])
    return {
        "identity": context["identity"],
        "binding_sha256": context["binding_sha256"],
        "topics": [
            {
                "topic": t,
                "disposition": "keep",
                "reason": "Preserve the independent ownership condition.",
                "removals": [
                    {
                        "address": block["address"],
                        "source_sha256": block["sha256"],
                        "reason": "Delete only the retired delegation instruction.",
                        "prose_span": {"start": 0, "end": len(RETIRED)},
                    }
                ]
                if t == TOPICS[0]
                else [],
                "media": [],
                "reference_edits": [],
            }
            for t in TOPICS
        ],
        "unresolved": [],
    }


def test_exact_partial_deletion_preserves_condition_and_all_adjacent_text():
    expected = BODY.replace(RETIRED, "")
    assert remove_blocks(BODY, [rule()]) == expected
    assert remove_blocks(expected, []) == expected


@pytest.mark.parametrize(
    "fault",
    [
        "source",
        "span",
        "negative",
        "outside",
        "empty",
        "boolean",
        "duplicate",
        "whole",
        "reference",
        "payload",
    ],
)
def test_partial_deletion_rejects_invalid_or_overlapping_guards(fault):
    selected = rule()
    rules = [selected]
    body = BODY
    if fault == "source":
        selected["sha256"] = "0" * 64
    elif fault == "span":
        selected["prose_span"]["sha256"] = "0" * 64
    elif fault == "negative":
        selected["prose_span"]["start"] = -1
    elif fault == "outside":
        selected["prose_span"]["end"] = 10000
    elif fault == "empty":
        selected["prose_span"]["end"] = 0
    elif fault == "boolean":
        selected["prose_span"]["start"] = False
    elif fault == "duplicate":
        rules.append(deepcopy(selected))
    elif fault == "whole":
        rules.append({k: v for k, v in selected.items() if k != "prose_span"})
    elif fault == "reference":
        selected["unlink_reference_sha256"] = ["0" * 64]
    else:
        body = BODY.replace(RETIRED, "`" + RETIRED + "` ")
        block = next(b for b in blocks(body) if b.kind == "paragraph")
        selected.update(location=block.location, sha256=block.sha256)
        selected["prose_span"] = {"start": 1, "end": 1 + len(RETIRED), "sha256": sha(RETIRED)}
    with pytest.raises(ValueError):
        remove_blocks(body, rules)


def test_partial_deletion_can_remove_hard_wrapped_prose_before_whole_block():
    retired = "Delegate the domain\nto Volterra. "
    body = BODY.replace(RETIRED, retired) + "\n## Retired\n\nRemove this whole procedure.\n"
    paragraph = next(b for b in blocks(body) if b.kind == "paragraph" and retired in b.text)
    section = next(
        b for b in blocks(body) if b.kind == "section" and b.text.startswith("## Retired")
    )
    selected = {
        "location": paragraph.location,
        "sha256": paragraph.sha256,
        "prose_span": {"start": 0, "end": len(retired), "sha256": sha(retired)},
    }
    output = remove_blocks(
        body, [selected, {"location": section.location, "sha256": section.sha256}]
    )
    assert output == BODY.replace(RETIRED, "")


def test_renewal_projects_the_retained_necessary_fact_without_weakening_necessity():
    context = source()
    candidate = compile_proposal(context, decision(context))
    assert candidate["body"] == BODY.replace(RETIRED, "")
    original = next(
        b for b in context["blocks"] if b["kind"] == "paragraph" and RETIRED in b["text"]
    )
    assert projected_fact(context, candidate, original) == NECESSARY + "\n"
    assert candidate["stages"][0]["document"]["block_removals"] == [rule()]
    assert candidate == compile_proposal(context, decision(context))


def test_native_removal_schema_is_strict_and_historical_whole_blocks_still_parse():
    context = source()
    body = review_request(context, [])
    schema = body["text"]["format"]["schema"]
    Draft202012Validator.check_schema(schema)
    removal = schema["$defs"]["Removal"]
    assert set(removal["required"]) == set(removal["properties"])
    assert "default" not in removal["properties"]["prose_span"]
    whole = decision(context)
    del whole["topics"][0]["removals"][0]["prose_span"]
    candidate = compile_proposal(context, whole)
    assert NECESSARY not in candidate["body"]
    assert candidate["proposal"] == whole


def test_partial_projection_requires_the_exact_independent_ownership_witness():
    from html_to_markdown.curation_renewal_review import (
        candidate_fact_blocks,
        fact_blocks,
        verify_validation,
    )

    context = source()
    candidate = compile_proposal(context, decision(context))
    retained = candidate_fact_blocks(candidate["body"])
    facts = []
    for original in fact_blocks(context["blocks"]):
        if original["kind"] in {"section", "heading", "hr"}:
            continue
        expected = NECESSARY + "\n" if RETIRED in original["text"] else original["text"]
        witness = next(block for block in retained if block["text"] == expected)
        facts.append(
            {
                "source_address": original["address"],
                "source_sha256": original["sha256"],
                "candidate_addresses": [witness["address"]],
                "candidate_sha256": [witness["sha256"]],
                "necessary": True,
                "preserved": True,
                "kind": "condition",
            }
        )
    validation = {
        "identity": context["identity"],
        "binding_sha256": context["binding_sha256"],
        "output_sha256": candidate["output_sha256"],
        "approved": True,
        "complete_document_reviewed": True,
        "complete_media_reviewed": True,
        "retirement_valid": True,
        "technical_relationships_preserved": True,
        "privacy_valid": True,
        "classifications_valid": True,
        "facts": facts,
        "failures": [],
        "unresolved": [],
    }
    verify_validation(context, candidate, validation)
    validation["facts"][0]["candidate_addresses"] = validation["facts"][1]["candidate_addresses"]
    validation["facts"][0]["candidate_sha256"] = validation["facts"][1]["candidate_sha256"]
    with pytest.raises(ValueError, match="retained fact or payload changed"):
        verify_validation(context, candidate, validation)
