"""Generated examples retain exact source guards without approving credential matches."""

# These denials require the empty mapping contract, not another falsey return type.
# pylint: disable=use-implicit-booleaness-not-comparison

import json

import pytest

from html_to_markdown.curation import json_bytes
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.source_replacements import replacement_provenance
from html_to_markdown.source_review import MediaChange, PrivacyDecision, scan
from html_to_markdown.source_review_reconciliation import (
    FinalClassification,
    check_classification,
    final_request,
)

SOURCE = "12345678-1234-4234-8234-123456789abc"
EXAMPLE = "00000000-0000-4000-8000-000000000002"


def text_case():
    context = {
        "identity": "doc",
        "binding_sha256": "b" * 64,
        "metadata": {},
        "body": SOURCE,
        "media": {},
    }
    candidate = {
        "metadata": {},
        "body": EXAMPLE,
        "media": {},
        "media_transforms": {},
        "findings": scan({"body": EXAMPLE}),
    }
    proposal = PrivacyDecision(
        identity="doc",
        binding_sha256="b" * 64,
        findings=[],
        unresolved=[],
        media_changes=[],
        values=[
            {
                "source_sha256": sha(SOURCE),
                "replacement": EXAMPLE,
            }
        ],
        text_changes=[
            {
                "field": "body",
                "start": 0,
                "end": len(SOURCE),
                "source_sha256": sha(SOURCE),
                "replacement": EXAMPLE,
                "reason": "Generated nonfunctional UUID example; preserve the UUID field type.",
            }
        ],
    )
    return context, candidate, proposal


def test_text_example_provenance_requires_exact_source_edit_and_new_value():
    context, candidate, proposal = text_case()
    evidence = replacement_provenance(context, candidate, proposal)
    assert evidence[candidate["findings"][0]["id"]][0]["source_sha256"] == sha(SOURCE)
    assert candidate["body"] == EXAMPLE and context["body"] == SOURCE
    assert (
        replacement_provenance(context, candidate, proposal.model_copy(update={"text_changes": []}))
        == {}
    )
    bad = proposal.text_changes[0].model_copy(update={"source_sha256": "c" * 64})
    assert (
        replacement_provenance(
            context, candidate, proposal.model_copy(update={"text_changes": [bad]})
        )
        == {}
    )
    retained = {**context, "body": EXAMPLE}
    assert replacement_provenance(retained, candidate, proposal) == {}
    stale = {**candidate, "body": "changed"}
    with pytest.raises(ValueError, match="finding"):
        replacement_provenance(context, stale, proposal)


def test_provenance_does_not_address_an_unedited_occurrence_in_the_same_field():
    context, candidate, proposal = text_case()
    context["body"] = SOURCE + "\n" + EXAMPLE
    candidate["body"] = EXAMPLE + "\n" + EXAMPLE
    candidate["findings"] = scan({"body": candidate["body"]})
    evidence = replacement_provenance(context, candidate, proposal)
    assert evidence
    for finding in candidate["findings"]:
        assert (finding["id"] in evidence) == (finding["start"] < len(EXAMPLE))


def test_unchanged_mapping_cannot_authorize_matches_inside_an_original_identifier():
    context, candidate, proposal = text_case()
    candidate.update(body=SOURCE, findings=scan({"body": SOURCE}))
    proposal = proposal.model_copy(
        update={
            "values": [proposal.values[0].model_copy(update={"replacement": SOURCE})],
            "text_changes": [proposal.text_changes[0].model_copy(update={"replacement": SOURCE})],
        }
    )
    assert replacement_provenance(context, candidate, proposal) == {}
    with pytest.raises(ValueError, match="stale"):
        replacement_provenance(
            context, candidate, proposal.model_copy(update={"binding_sha256": "d" * 64})
        )


def test_quoted_native_uuid_requires_confirmed_source_rectangle_and_output_digest():
    context, candidate, proposal = text_case()
    source, target = json.dumps(SOURCE), json.dumps(EXAMPLE)
    source_digest, output_digest = "a" * 64, "c" * 64
    location = "media/" + output_digest + "/0/ocr"
    context["confirmed_media_values"] = [
        {
            "media_sha256": source_digest,
            "frame": 0,
            "rectangle": [2, 3, 120, 20],
            "value": source,
            "value_sha256": sha(source),
        }
    ]
    candidate.update(
        body="",
        media={output_digest: {"frames": [{"text": target, "metadata": {}}]}},
        media_transforms={source_digest: {"output_sha256": output_digest}},
        findings=scan({location: target}),
    )
    proposal = proposal.model_copy(
        update={
            "text_changes": [],
            "values": [
                proposal.values[0].model_copy(
                    update={"source_sha256": sha(source), "replacement": target}
                )
            ],
        }
    )
    proposal.media_changes = [
        MediaChange(
            source_sha256=source_digest,
            rectangles=[],
            frames=[],
            strip_metadata=False,
            reason="Exact independently confirmed replacement.",
            labels=[
                {
                    "rectangle": [2, 3, 120, 20],
                    "source_value_sha256": sha(source),
                    "text": target,
                    "size": 12,
                }
            ],
        )
    ]
    evidence = replacement_provenance(context, candidate, proposal)
    assert evidence[candidate["findings"][0]["id"]][0]["frame"] == 0
    assert (
        replacement_provenance({**context, "confirmed_media_values": []}, candidate, proposal) == {}
    )
    wrong_output = {**candidate, "media_transforms": {source_digest: {"output_sha256": "d" * 64}}}
    assert replacement_provenance(context, wrong_output, proposal) == {}
    misplaced = {
        **context,
        "confirmed_media_values": [
            {**context["confirmed_media_values"][0], "rectangle": [3, 3, 120, 20]}
        ],
    }
    assert replacement_provenance(misplaced, candidate, proposal) == {}


@pytest.mark.parametrize(
    "kind", ["credential", "private-key", "authorization-header", "session-cookie"]
)
def test_versioned_request_preserves_legacy_and_never_approves_credential_kinds(kind):
    context, candidate, proposal = text_case()
    legacy = final_request(context, candidate, [])
    assert "replacement_provenance" not in json.loads(legacy["input"][0]["content"][0]["text"])
    current = final_request(
        context, candidate, [], proposal=proposal, version="final-privacy-findings/2"
    )
    payload = json.loads(current["input"][0]["content"][0]["text"])
    assert payload["replacement_provenance"]
    assert payload["transformation_sha256"] == sha(json_bytes(proposal.model_dump()))
    assert final_request(context, candidate, []) == legacy
    finding = {**candidate["findings"][0], "kind": kind}
    credential = {**candidate, "findings": [finding]}
    assert replacement_provenance(context, credential, proposal) == {}
    classification = FinalClassification(
        identity="doc",
        binding_sha256="b" * 64,
        candidate_sha256=sha(json_bytes(credential)),
        findings=[
            {
                "finding_id": finding["id"],
                "disposition": "documentation_example",
                "reason": "mapped",
            }
        ],
    )
    with pytest.raises(ValueError, match="credential"):
        check_classification(context, credential, classification)
