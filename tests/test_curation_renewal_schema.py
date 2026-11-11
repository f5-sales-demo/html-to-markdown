"""Exact request domains prevent headings or unreviewed media escaping coverage."""

from test_curation_renewal import context, proposal

from html_to_markdown.curation_renewal import compile_proposal, proposal_context
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
