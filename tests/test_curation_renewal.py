"""Renewal retrieves exact structural spans and never waives independent review."""

import io
import json
from copy import deepcopy

import pytest
from PIL import Image

from html_to_markdown.content_policy import migrate_content, validate_content_policy
from html_to_markdown.curation import CurationPolicy, blocks, json_bytes, load_curation_policy
from html_to_markdown.curation_renewal import TOPICS, compile_proposal, proposal_context
from html_to_markdown.curation_renewal_jobs import replay_renewal, run_renewal
from html_to_markdown.curation_renewal_review import (
    candidate_fact_blocks,
    fact_blocks,
    review_request,
    verify_validation,
)
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.contracts import ResponseEvidence
from html_to_markdown.enrichment.gates import request_hash
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document, split_document


def context():
    metadata = PageMetadata(
        sourceId="my-f5-com",
        title="Cloud services",
        slug="cloud-services",
        url="https://my.f5.com/manage/s/article/K000123456",
        category="knowledge",
    ).model_dump(by_alias=True)
    body = (
        "# Cloud services\n\n"
        "Configure the origin pool before the load balancer. Use port 8443.\n\n"
        "## Obsolete procedure\n\nUse SMSv1 with aws_vpc_site.\n\n"
        "## Verification\n\nExpect healthy origins within 30 seconds.\n\n"
        "| Capability | Mapping |\n| --- | --- |\n| Optional mapping | |\n"
    )
    return proposal_context(metadata, body, {}, "a" * 64, "b" * 64)


def proposal(source):
    removed = next(
        block
        for block in source["blocks"]
        if block["kind"] == "section" and block["text"].startswith("## Obsolete procedure")
    )
    return {
        "identity": source["identity"],
        "binding_sha256": source["binding_sha256"],
        "topics": [
            {
                "topic": topic,
                "disposition": "keep",
                "reason": "independent cloud explanation",
                "removals": [
                    {
                        "address": removed["address"],
                        "source_sha256": removed["sha256"],
                        "reason": "retired procedure",
                    }
                ]
                if topic == TOPICS[0]
                else [],
                "media": [],
                "reference_edits": [],
            }
            for topic in TOPICS
        ],
        "unresolved": [],
    }


def test_structural_renewal_preserves_independent_facts_and_blank_cells():
    source = context()
    decision = proposal(source)
    result = compile_proposal(source, decision)
    assert "Use SMSv1" not in result["body"]
    assert "Use port 8443" in result["body"]
    assert "before the load balancer" in result["body"]
    assert "within 30 seconds" in result["body"]
    assert "| Optional mapping | |" in result["body"]
    assert result == compile_proposal(source, decision)
    assert result["independent_validation_required"] is True
    assert source["body"] != result["body"]
    for stage in result["stages"]:
        assert stage["document"]["input_sha256"] == sha(stage["input_body"])
        assert stage["document"]["output_sha256"] == sha(stage["output_body"])


def test_nested_same_line_list_facts_have_distinct_lossless_review_addresses():
    body = "# Debug\n\n1. 1. Collect debug info\n   2. Download the archive\n\n- - ```\n    curl --output debug.tar.bz2\n    ```\n"
    source = proposal_context(context()["metadata"], body, {}, "a" * 64, "b" * 64)
    facts = fact_blocks(source["blocks"])
    assert len({b["address"] for b in facts}) == len(facts)
    assert [b["text"] for b in facts] == [b["text"] for b in source["blocks"]]
    assert any(":occurrence:2" in b["address"] for b in facts)
    candidate = {"body": body, "output_sha256": "c" * 64}
    request = review_request(source, [], candidate)
    schema = request["text"]["format"]["schema"]
    required = [b for b in facts if b["kind"] not in {"section", "heading", "hr"}]
    addresses = schema["$defs"]["FactV2"]["properties"]["source_address"]["enum"]
    assert len(addresses) == len(set(addresses)) == len(required)
    assert schema["properties"]["facts"]["minItems"] == len(required)
    assert fact_blocks(source["blocks"]) == facts
    decision = {
        "identity": source["identity"],
        "binding_sha256": source["binding_sha256"],
        "topics": [
            {
                "topic": t,
                "disposition": "keep",
                "reason": "Current debug requirements remain useful.",
                "removals": [],
                "media": [],
                "reference_edits": [],
            }
            for t in TOPICS
        ],
        "unresolved": [],
    }
    compiled = compile_proposal(source, decision)
    approved = validation(source, compiled)
    verify_validation(source, compiled, approved)
    approved["facts"][-1] = approved["facts"][0]
    with pytest.raises(ValueError, match="coverage is incomplete"):
        verify_validation(source, compiled, approved)


def test_actual_migration_uses_the_proposed_topic_policy_without_global_changes(tmp_path):
    source = context()
    body = (
        "# Cloud services\n\nOrchestrate current cloud workloads with independent origin pools.\n"
    )
    path = tmp_path / "content/my-f5-com/K000123456/index.md"
    path.parent.mkdir(parents=True)
    path.write_text(serialize_document(PageMetadata.model_validate(source["metadata"]), body))
    metadata, original = split_document(path.read_text())
    raw = deepcopy(load_curation_policy().raw)
    topic = next(t for t in raw["topics"] if t["id"] == "smsv2-current")
    topic["documents"].append(
        {
            "url": metadata["url"],
            "disposition": "keep",
            "input_sha256": sha(original),
            "output_sha256": sha(original),
            "block_removals": [],
            "reason": "independently reviewed current procedure",
            "review_index": None,
        }
    )
    proposed = CurationPolicy(raw, sha(json_bytes(raw)))
    current_pin = load_curation_policy().sha256
    migrate_content(tmp_path, curation_policy=proposed)
    validate_content_policy(tmp_path, curation_policy=proposed)
    assert path.is_file() and "independent origin pools" in path.read_text()
    assert load_curation_policy().sha256 == current_pin


def test_same_media_bytes_require_separate_document_placement_decisions(tmp_path):
    raw = deepcopy(load_curation_policy().raw)
    topic = next(t for t in raw["topics"] if t["id"] == "smsv2-current")
    first = "https://my.f5.com/manage/s/article/K000123456"
    second = "https://my.f5.com/manage/s/article/K000123457"
    third = "https://my.f5.com/manage/s/article/K000123458"
    for url, disposition in [(first, "keep"), (second, "remove")]:
        topic["media"].append(
            {
                "sha256": "a" * 64,
                "documents": [url],
                "disposition": disposition,
                "reason": "independent placement review",
            }
        )
    policy = CurationPolicy(raw, sha(json_bytes(raw)))
    path = tmp_path / "content/my-f5-com/K000123456/index.md"
    assert policy.media_review("smsv2-current", "a" * 64, first, path)["disposition"] == "keep"
    assert policy.media_review("smsv2-current", "a" * 64, second, path)["disposition"] == "remove"
    assert not policy.media_review("smsv2-current", "a" * 64, third, path)
    assert not policy.media_review("smsv2-current", "b" * 64, first, path)


def test_actual_planner_never_reuses_another_documents_media_approval(tmp_path):
    raw = deepcopy(load_curation_policy().raw)
    topic = next(t for t in raw["topics"] if t["id"] == "smsv2-current")
    pixels = io.BytesIO()
    Image.new("RGB", (16, 16), "blue").save(pixels, format="PNG")
    digest = sha(pixels.getvalue())
    paths = []
    for identifier in ["K000123456", "K000123457"]:
        metadata = PageMetadata.model_validate(context()["metadata"])
        metadata.url = "https://my.f5.com/manage/s/article/" + identifier
        path = tmp_path / "content/my-f5-com" / identifier / "index.md"
        paths.append(path)
        asset = path.parent / "assets" / (digest + ".png")
        asset.parent.mkdir(parents=True)
        asset.write_bytes(pixels.getvalue())
        body = (
            "# Cloud services\n\nOrchestrate current workloads with independent origin pools.\n\n![Diagram](assets/"
            + digest
            + ".png)\n"
        )
        path.write_text(serialize_document(metadata, body))
        _, body = split_document(path.read_text())
        topic["documents"].append(
            {
                "url": metadata.url,
                "disposition": "keep",
                "input_sha256": sha(body),
                "output_sha256": sha(body),
                "block_removals": [],
                "reason": "independent current workload",
                "review_index": None,
            }
        )
    topic["media"].append(
        {
            "sha256": digest,
            "disposition": "keep",
            "documents": ["https://my.f5.com/manage/s/article/K000123456"],
            "reason": "reviewed only for the first article",
        }
    )
    policy = CurationPolicy(raw, sha(json_bytes(raw)))
    migrate_content(tmp_path, curation_policy=policy)
    assert paths[0].is_file()
    assert not paths[1].exists()


@pytest.mark.parametrize(
    "change", ["binding", "missing_topic", "duplicate_topic", "guard", "overlap", "unresolved"]
)
def test_incomplete_stale_or_overlapping_proposals_cannot_compile(change):
    source = context()
    decision = deepcopy(proposal(source))
    if change == "binding":
        decision["binding_sha256"] = "c" * 64
    elif change == "missing_topic":
        decision["topics"].pop()
    elif change == "duplicate_topic":
        decision["topics"][-1]["topic"] = TOPICS[0]
    elif change == "guard":
        decision["topics"][0]["removals"][0]["source_sha256"] = "c" * 64
    elif change == "overlap":
        child = next(
            b for b in blocks(source["body"]) if b.kind == "paragraph" and "SMSv1" in b.text
        )
        decision["topics"][1]["removals"] = [
            {
                "address": child.location,
                "source_sha256": child.sha256,
                "reason": "dependent paragraph",
            }
        ]
    else:
        decision["unresolved"] = ["requires independent source evidence"]
    with pytest.raises(ValueError):
        compile_proposal(source, decision)


def test_changed_media_and_incomplete_placement_reviews_are_rejected():
    source = context()
    source = proposal_context(
        source["metadata"], source["body"], {"d" * 64: {"frame_count": 1}}, "a" * 64, "b" * 64
    )
    decision = proposal(source)
    with pytest.raises(ValueError, match="media"):
        compile_proposal(source, decision)

    for topic in decision["topics"]:
        topic["media"] = [
            {"sha256": "d" * 64, "disposition": "keep", "reason": "independent architecture"}
        ]
    assert compile_proposal(source, decision)["independent_validation_required"]
    decision["topics"][0]["media"][0]["sha256"] = "e" * 64
    with pytest.raises(ValueError, match="media"):
        compile_proposal(source, decision)


def validation(source, candidate):
    retained = candidate_fact_blocks(candidate["body"])
    return {
        "identity": source["identity"],
        "binding_sha256": source["binding_sha256"],
        "output_sha256": candidate["output_sha256"],
        "approved": True,
        "complete_document_reviewed": True,
        "complete_media_reviewed": True,
        "retirement_valid": True,
        "technical_relationships_preserved": True,
        "privacy_valid": True,
        "classifications_valid": True,
        "facts": [
            {
                "source_address": b["address"],
                "source_sha256": b["sha256"],
                "necessary": any(c["sha256"] == b["sha256"] for c in retained),
                "candidate_addresses": [
                    c["address"] for c in retained if c["sha256"] == b["sha256"]
                ],
                "candidate_sha256": [c["sha256"] for c in retained if c["sha256"] == b["sha256"]],
                "preserved": any(c["sha256"] == b["sha256"] for c in retained),
                "kind": "fact",
            }
            for b in fact_blocks(source["blocks"])
            if b["kind"] not in {"section", "heading", "hr"}
        ],
        "failures": [],
        "unresolved": [],
    }


@pytest.mark.parametrize(
    "change", ["none", "rejection", "binding", "incomplete", "payload", "media"]
)
def test_independent_validation_requires_complete_exact_fact_and_media_review(change):
    source = context()
    candidate = compile_proposal(source, proposal(source))
    approved = validation(source, candidate)
    if change == "rejection":
        approved["approved"] = False
    elif change == "binding":
        approved["binding_sha256"] = "f" * 64
    elif change == "incomplete":
        approved["facts"].pop()
    elif change == "payload":
        approved["facts"][0]["candidate_sha256"] = ["f" * 64]
    elif change == "media":
        approved["complete_media_reviewed"] = False
    if change == "none":
        verify_validation(source, candidate, approved)
    else:
        with pytest.raises(ValueError):
            verify_validation(source, candidate, approved)


def test_review_requests_use_pinned_models_and_exact_controller_context():
    source = context()
    candidate = compile_proposal(source, proposal(source))
    editor = review_request(source, [])
    reviewer = review_request(source, [], candidate)
    assert editor["model"] == "gpt-6.1-sol"
    assert reviewer["model"] == "gpt-6.1-sol"
    assert editor["reasoning"] == reviewer["reasoning"] == {"effort": "high"}
    assert editor["store"] is False and "tools" not in editor
    assert reviewer["text"]["format"]["schema"]["properties"]["output_sha256"]["enum"] == [
        candidate["output_sha256"]
    ]


@pytest.mark.parametrize("reject", [1, 3])
def test_private_journal_bounds_corrections_and_replays_exact_requests(tmp_path, reject):
    source = context()
    calls = []
    rejected = []

    class Client:
        validations = 0

        def execute(self, body):
            calls.append(body)
            payload = json.loads(body["input"][0]["content"][0]["text"])
            if body["metadata"]["content_review_role"] == "editor":
                result = proposal(source)
            else:
                self.validations += 1
                result = validation(source, payload["candidate"])
                if self.validations <= reject:
                    result.update(
                        approved=False, failures=["independent topic evidence is incomplete"]
                    )
            response = {
                "status": "completed",
                "model": body["model"],
                "output": [{"content": [{"type": "output_text", "text": json.dumps(result)}]}],
            }
            return ResponseEvidence(
                request_sha256=request_hash(body),
                response_sha256=sha(json.dumps(response, sort_keys=True, separators=(",", ":"))),
                model=body["model"],
                reasoning="high",
                prompt_version="curation-renewal/1",
                request=body,
                response=response,
                error=None,
                transport="codex_litellm",
            )

        def invalidate(self, evidence):
            rejected.append(evidence.request_sha256)

    if reject == 1:
        result = run_renewal(source, [], tmp_path, Client())
        assert result["planner_validation_required"] is True
        assert result["independently_validated"] is True
        assert len(calls) == 4 and len(rejected) == 2
        assert replay_renewal(source, [], tmp_path) == result
        assert len(calls) == 4
        assert request_hash(calls[0]) != request_hash(calls[2])
    else:
        with pytest.raises(ValueError, match="corrected"):
            run_renewal(source, [], tmp_path, Client())
        assert len(calls) == 6
        with pytest.raises(ValueError):
            replay_renewal(source, [], tmp_path)
        with pytest.raises(ValueError, match="corrected"):
            run_renewal(source, [], tmp_path, Client())
        assert len(calls) == 6


def test_media_placement_dispositions_are_scoped_to_their_policy_topic(tmp_path):
    raw = deepcopy(load_curation_policy().raw)
    url = "https://my.f5.com/manage/s/article/K000123456"
    for identifier, disposition in [
        ("smsv2-current", "keep"),
        ("terraform-provider-current", "remove"),
    ]:
        topic = next(t for t in raw["topics"] if t["id"] == identifier)
        topic["media"].append(
            {
                "sha256": "a" * 64,
                "documents": [url],
                "disposition": disposition,
                "reason": "independent topic placement review",
            }
        )
    policy = CurationPolicy(raw, sha(json_bytes(raw)))
    path = tmp_path / "content/my-f5-com/K000123456/index.md"
    assert policy.media_review("smsv2-current", "a" * 64, url, path)["disposition"] == "keep"
    assert (
        policy.media_review("terraform-provider-current", "a" * 64, url, path)["disposition"]
        == "remove"
    )
