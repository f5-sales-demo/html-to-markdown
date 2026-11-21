"""Original structural positions survive exact staged edits with duplicate source bytes."""

from copy import deepcopy

import pytest
import test_curation_renewal as fixtures

from html_to_markdown.curation import blocks, load_curation_policy, reference_spans, remove_blocks
from html_to_markdown.curation_media import relocate_references
from html_to_markdown.curation_projection import SourceProjection
from html_to_markdown.curation_renewal import (
    TOPICS,
    _stage_references,
    _stage_rules,
    compile_proposal,
    proposal_context,
)
from html_to_markdown.render import normalize_body


def prepared(body):
    source = fixtures.context()
    context = proposal_context(
        source["metadata"], normalize_body(body), {}, "a" * 64, load_curation_policy().sha256
    )
    proposal = {
        "identity": context["identity"],
        "binding_sha256": context["binding_sha256"],
        "topics": [
            {
                "topic": topic,
                "disposition": "keep",
                "reason": "Complete independent contextual decision required",
                "removals": [],
                "reference_edits": [],
                "media": [],
            }
            for topic in TOPICS
        ],
        "unresolved": [],
    }
    return context, proposal


def select(proposal, topic, block, prose_span=None):
    proposal["topics"][topic]["removals"].append(
        {
            "address": block.location,
            "source_sha256": block.sha256,
            "reason": "Only this exact original occurrence is retired",
            "prose_span": prose_span,
        }
    )


@pytest.mark.parametrize("heading", ["## Second", "## First", ""])
@pytest.mark.parametrize("preceding", [False, True])
def test_duplicate_paragraphs_keep_their_original_identity(heading, preceding):
    source, proposal = prepared(
        "# Current\n\n## Retired\n\nOld task.\n\n## First\n\nRepeated explanation.\n\n"
        "Keep port 8443.\n\n" + heading + "\n\nRepeated explanation.\n\nKeep port 9443.\n"
    )
    parsed = blocks(source["body"])
    repeated = [b for b in parsed if b.kind == "paragraph" and "Repeated" in b.text]
    if preceding:
        select(
            proposal,
            0,
            next(
                b
                for b in parsed
                if b.kind == "section" and "Old task" in b.text and b.text.startswith("##")
            ),
        )
    select(proposal, 4, repeated[1])
    result = compile_proposal(source, proposal)
    assert result["body"].count("Repeated explanation.") == 1
    assert "Repeated explanation.\n\nKeep port 8443." in result["body"]
    assert "Keep port 9443." in result["body"]
    assert result["independent_validation_required"]
    assert compile_proposal(source, proposal) == result


def test_preceding_duplicate_removal_cannot_retarget_another_occurrence():
    source, proposal = prepared("# Current\n\nSame.\n\nKeep A.\n\nSame.\n\nKeep B.\n\nSame.\n")
    repeated = [b for b in blocks(source["body"]) if b.text == "Same.\n"]
    select(proposal, 0, repeated[0])
    select(proposal, 4, repeated[2])
    result = compile_proposal(source, proposal)
    assert result["body"] == "# Current\n\nKeep A.\n\nSame.\n\nKeep B.\n"


def test_preceding_multiline_prose_deletion_preserves_positions():
    source, proposal = prepared(
        "# Current\n\nKeep prefix.\nRetired sentence.\nKeep suffix.\n\nSame.\n\nKeep A.\n\nSame.\n"
    )
    parsed = blocks(source["body"])
    prose = next(b for b in parsed if b.kind == "paragraph" and "Retired sentence" in b.text)
    start = prose.text.index("Retired sentence.")
    select(proposal, 0, prose, {"start": start, "end": start + len("Retired sentence.\n")})
    repeated = [b for b in parsed if b.text == "Same.\n"]
    select(proposal, 4, repeated[1])
    result = compile_proposal(source, proposal)
    assert "Keep prefix.\nKeep suffix." in result["body"]
    assert result["body"].count("Same.") == 1


def test_original_digest_and_overlap_guards_still_fail():
    source, proposal = prepared("# Current\n\nSame.\n\nKeep.\n\nSame.\n")
    repeated = [b for b in blocks(source["body"]) if b.text == "Same.\n"]
    select(proposal, 4, repeated[1])
    stale = deepcopy(proposal)
    stale["topics"][4]["removals"][0]["source_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="structural guard"):
        compile_proposal(source, stale)
    select(proposal, 0, repeated[1])
    with pytest.raises(ValueError, match="overlapping"):
        compile_proposal(source, proposal)


@pytest.mark.parametrize(
    "body",
    [
        "",
        " \n \t\n",
        "# H\r\n\r\nA.\r\n",
        " \t# H  \n\n\n\nA.\t\n\n",
        "A.\u2028B.\u2029",
        "A.\rB.\n",
        "A.\n\n\nB.",
        "A.\n\n\n",
    ],
)
def test_projection_normalization_matches_the_renderer(body):
    projection = SourceProjection(body)
    projection.normalize()
    projection.check(normalize_body(body))


def test_media_replacement_tracks_positions_without_textual_retargeting():
    source, proposal = prepared("# Current\n\nSame.\n\n![Diagram](assets/source.png)\n\nSame.\n")
    projection = SourceProjection(source["body"])
    current = relocate_references(
        source["body"],
        [{"source": "assets/source.png", "destination": "assets/" + "a" * 64 + ".png", "count": 1}],
        projection=projection,
    )
    projection.check(current)
    repeated = [b for b in blocks(source["body"]) if b.text == "Same.\n"]
    rules = _stage_rules(
        current,
        [
            {
                "start": repeated[1].start,
                "kind": repeated[1].kind,
                "text": repeated[1].text,
                "sha256": repeated[1].sha256,
            }
        ],
        projection,
    )
    assert rules[0]["location"] == next(
        b.location
        for b in blocks(current)
        if b.start == repeated[1].start and b.kind == "paragraph"
    )


def test_modified_original_span_cannot_retarget_an_identical_survivor():
    source, _ = prepared("# Current\n\nSame.\n\nKeep.\n\nSame.\n")
    projection = SourceProjection(source["body"])
    repeated = [b for b in blocks(source["body"]) if b.text == "Same.\n"]
    offsets = projection.line_offsets(source["body"])
    projection.edit(offsets[repeated[0].start], offsets[repeated[0].end])
    with pytest.raises(ValueError, match="original renewal structural span"):
        _stage_rules(
            projection.body,
            [
                {
                    "start": repeated[0].start,
                    "kind": repeated[0].kind,
                    "text": repeated[0].text,
                    "sha256": repeated[0].sha256,
                }
            ],
            projection,
        )


def test_duplicate_references_follow_original_coordinates_after_heading_cleanup():
    body = "# Current\n\n## First\n\nRetired task.\n\n## Same\n\n[Old](https://example.invalid/retired)\n\nKeep A.\n\n## Same\n\n[Old](https://example.invalid/retired)\n"
    selected = reference_spans(body)[1]
    retired = next(b for b in blocks(body) if b.kind == "paragraph" and "Retired task" in b.text)
    projection = SourceProjection(body)
    current = remove_blocks(
        body, [{"location": retired.location, "sha256": retired.sha256}], projection=projection
    )
    rules = _stage_references(current, [selected], projection)
    assert rules[0]["location"] == reference_spans(current)[1]["block_address"]
    result = remove_blocks(current, rules, projection=projection)
    assert result.count("[Old](https://example.invalid/retired)") == 1
    assert "Old\n" in result and "## First" not in result
