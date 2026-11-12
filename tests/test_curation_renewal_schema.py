"""Exact request domains prevent headings or unreviewed media escaping coverage."""

import pytest
from jsonschema import Draft202012Validator, ValidationError
from test_curation_renewal import context, proposal

from html_to_markdown.curation_renewal import Proposal, compile_proposal, proposal_context
from html_to_markdown.curation_renewal_review import review_request


def test_independent_fact_schema_contains_only_required_source_blocks():
    source = context()
    candidate = compile_proposal(source, proposal(source))
    schema = review_request(source, [], candidate)["text"]["format"]["schema"]
    required = [b for b in source["blocks"] if b["kind"] not in {"heading", "section", "hr"}]
    assert (
        schema["properties"]["facts"]["minItems"]
        == schema["properties"]["facts"]["maxItems"]
        == len(required)
    )
    assert schema["$defs"]["FactV2"]["properties"]["source_address"]["enum"] == [
        b["address"] for b in required
    ]


def test_editor_schema_requires_every_media_placement_for_every_topic():
    source = context()
    source = proposal_context(
        source["metadata"], source["body"], {"a" * 64: {}, "b" * 64: {}}, "c" * 64, "d" * 64
    )
    schema = review_request(source, [])["text"]["format"]["schema"]
    media = schema["$defs"]["TopicDecision"]["properties"]["media"]
    assert media["minItems"] == media["maxItems"] == 2
    assert schema["$defs"]["MediaChoice"]["properties"]["sha256"]["enum"] == ["a" * 64, "b" * 64]


def test_native_media_addresses_cannot_witness_markdown_fact_blocks():
    source = context()
    candidate = compile_proposal(source, proposal(source))
    schema = review_request(source, [], candidate)["text"]["format"]["schema"]
    properties = schema["$defs"]["FactV2"]["properties"]
    addresses = properties["candidate_addresses"]["items"]["enum"]
    assert addresses and all(not address.startswith("media/") for address in addresses)
    assert "media/" + "a" * 64 + "/0" not in addresses
    assert "a" * 64 not in properties["candidate_sha256"]["items"]["enum"]


def test_topic_schema_requires_reference_edits_even_when_empty():
    schema = review_request(context(), [])["text"]["format"]["schema"]["$defs"]["TopicDecision"]
    assert set(schema["required"]) == set(schema["properties"])
    assert "reference_edits" in schema["required"]


def test_native_editor_schema_requires_all_nested_object_properties():
    schema = review_request(context(), [])["text"]["format"]["schema"]

    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert set(node["required"]) == set(node["properties"])
                assert node["additionalProperties"] is False
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(schema)
    assert "default" not in schema["$defs"]["MediaChoice"]["properties"]["masks"]


def test_native_media_choice_requires_explicit_empty_masks():
    source = context()
    source = proposal_context(
        source["metadata"], source["body"], {"a" * 64: {}}, "c" * 64, "d" * 64
    )
    value = proposal(source)
    for topic in value["topics"]:
        topic["media"] = [
            {
                "sha256": "a" * 64,
                "disposition": "keep",
                "reason": "Necessary dashboard",
                "masks": [],
            }
        ]
    schema = review_request(source, [])["text"]["format"]["schema"]
    Draft202012Validator(schema).validate(value)
    value["topics"][0]["media"][0].pop("masks")
    with pytest.raises(ValidationError, match="masks"):
        Draft202012Validator(schema).validate(value)


def test_historical_media_choices_preserve_original_unset_fields():
    source = context()
    value = proposal(source)
    for topic in value["topics"]:
        topic["media"] = [
            {"sha256": "a" * 64, "disposition": "keep", "reason": "Necessary dashboard"}
        ]
    parsed = Proposal.model_validate(value)
    assert parsed.model_dump(exclude_unset=True) == value
    assert all(not choice.masks for topic in parsed.topics for choice in topic.media)
