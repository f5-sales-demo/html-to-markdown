"""Version-two source retrieval, complete coverage, and protected payload gates."""

from pathlib import Path

import pytest

from html_to_markdown.enrichment.analysis import inventory, sha
from html_to_markdown.enrichment.contracts_v2 import (
    ClassificationV2,
    ClassifiedSection,
    DocumentDecisionV2,
    EditedSection,
    EditV2,
    ValidationV2,
)
from html_to_markdown.enrichment.v2 import VALIDATOR_PROMPT, _request, assemble, sections
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document


@pytest.fixture
def document(tmp_path: Path) -> dict:
    path = tmp_path / "content/my-f5-com/K000123456/index.md"
    path.parent.mkdir(parents=True)
    body = "# Configure\n\nAn essential explanation.\n\n## Before starting\n\nUse a 30 second timeout.\n\n## Procedure\n\nRun `curl --retry 3` and expect HTTP 200.\n"
    metadata = PageMetadata(
        sourceId="my-f5-com",
        title="Configure",
        slug="k000123456",
        url="https://my.f5.com/manage/s/article/K000123456",
        category="knowledge",
    )
    path.write_text(serialize_document(metadata, body))
    return inventory(path, tmp_path)


def classified(doc: dict, groups: list[dict]) -> list[ClassifiedSection]:
    return [
        ClassifiedSection(
            document=doc["document"],
            input_sha256=doc["input_sha256"],
            section=group["id"],
            classifications=[
                ClassificationV2(
                    address=block["address"],
                    source_sha256=block["sha256"],
                    classification="article_explanation",
                    evidence_addresses=[block["address"]],
                    reason="necessary explanation",
                    uncertain=False,
                )
                for block in group["blocks"]
            ],
        )
        for group in groups
    ]


def header(doc: dict) -> DocumentDecisionV2:
    return DocumentDecisionV2(
        document=doc["document"],
        input_sha256=doc["input_sha256"],
        disposition="rewrite",
        canonical_document=None,
        description="Configure the service with the required timeout and expected result.",
        description_evidence=[doc["blocks"][0]["address"]],
        reason="preserve complete guidance",
        media=[],
        privacy_findings=[],
        unresolved=[],
    )


def test_sections_retain_every_character_and_classify_every_address_once(document: dict) -> None:
    groups = sections(document, max_chars=80)
    assert "".join(group["body"] for group in groups) == document["body"]
    assert [block["address"] for group in groups for block in group["blocks"]] == [
        block["address"] for block in document["blocks"]
    ]
    assert len(groups) == 3


def test_whole_document_validation_uses_independent_prompt_and_role(document: dict) -> None:
    body = _request("gpt-6.1-sol", "whole_document_validation", document, {}, ValidationV2)
    assert body["instructions"].startswith(VALIDATOR_PROMPT)
    assert body["metadata"]["content_review_role"] == "validator"
    assert body["reasoning"] == {"effort": "high"}


def test_model_never_has_to_reproduce_exact_source_strings(document: dict) -> None:
    groups = sections(document)
    block = next(
        block
        for block in document["blocks"]
        if block["kind"] == "paragraph" and "essential explanation" in block["text"]
    )
    edits = [
        EditedSection(
            document=document["document"],
            input_sha256=document["input_sha256"],
            section=groups[0]["id"],
            edits=[
                EditV2(
                    address=block["address"],
                    source_sha256=block["sha256"],
                    replacement="An essential service explanation.\n",
                    reason="clarify prose",
                    evidence_addresses=[block["address"]],
                    repair_evidence=None,
                )
            ],
            reason="retain facts",
            unresolved=[],
        )
    ]
    decision, body = assemble(
        document,
        groups,
        classified(document, groups),
        edits,
        header(document),
        {document["document"]: document},
    )
    assert decision.edits[0].source == block["text"]
    assert "`curl --retry 3`" in body and "30 second timeout" in body
    assert "source" not in EditV2.model_json_schema()["properties"]


def test_missing_classification_or_changed_digest_rejects_entire_proposal(document: dict) -> None:
    groups = sections(document)
    classes = classified(document, groups)
    classes[0].classifications.pop()
    with pytest.raises(ValueError, match="coverage"):
        assemble(document, groups, classes, [], header(document), {document["document"]: document})
    classes = classified(document, groups)
    classes[0].classifications[0].source_sha256 = sha("stale")
    with pytest.raises(ValueError, match="digest"):
        assemble(document, groups, classes, [], header(document), {document["document"]: document})


def test_numeric_limits_and_commands_remain_mandatory(document: dict) -> None:
    groups = sections(document)
    block = next(
        block
        for block in document["blocks"]
        if block["kind"] == "paragraph" and "30 second" in block["text"]
    )
    edit = EditedSection(
        document=document["document"],
        input_sha256=document["input_sha256"],
        section=groups[0]["id"],
        edits=[
            EditV2(
                address=block["address"],
                source_sha256=block["sha256"],
                replacement="Use a timeout.\n",
                reason="bad compression",
                evidence_addresses=[block["address"]],
                repair_evidence=None,
            )
        ],
        reason="incorrect",
        unresolved=[],
    )
    with pytest.raises(ValueError, match="payload|numeric"):
        assemble(
            document,
            groups,
            classified(document, groups),
            [edit],
            header(document),
            {document["document"]: document},
        )
