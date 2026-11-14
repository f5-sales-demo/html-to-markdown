"""Retired link targets may be removed without losing prerequisite or payload facts."""

from copy import deepcopy

import pytest
from test_curation_renewal import validation

from html_to_markdown.content_policy import migrate_content, validate_content_policy
from html_to_markdown.curation import (
    CurationPolicy,
    blocks,
    corpus_digest,
    json_bytes,
    load_curation_policy,
    reference_spans,
    remove_blocks,
)
from html_to_markdown.curation_renewal import (
    TOPICS,
    compile_proposal,
    projected_fact,
    proposal_context,
    retired_reference,
)
from html_to_markdown.curation_renewal_review import verify_validation
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document

OLD_VM = "https://docs.cloud.f5.com/docs-v2/multi-cloud-network-connect/how-to/site-management/create-vmw-site"
OLD_TOKEN = "https://docs.cloud.f5.com/docs-v2/multi-cloud-network-connect/how-to/site-management/create-secure-mesh-site"


def inputs():
    metadata = PageMetadata(
        sourceId="docs-cloud-f5-com",
        title="Current site prerequisites",
        slug="site-local-ui-usage",
        category="knowledge",
        url="https://docs.cloud.f5.com/docs-v2/multi-cloud-network-connect/how-to/site-management/site-local-ui-usage",
    ).model_dump(by_alias=True)
    body = (
        "# Current site prerequisites\n\n## Before you begin\n\n- Deployed a VMware site with 8 GiB memory: [VMware prerequisite]("
        + OLD_VM
        + ").\n- Generated a site token before filling the Token field: [Token prerequisite]("
        + OLD_TOKEN
        + ").\n\n## Verification\n\nKeep TLS enabled. Expect a healthy site within 30 seconds.\n\n```sh\nkubectl get pods -n app\n```\n"
    )
    source = proposal_context(metadata, body, {}, "a" * 64, load_curation_policy().sha256)
    proposal = {
        "identity": source["identity"],
        "binding_sha256": source["binding_sha256"],
        "unresolved": [],
        "topics": [
            {
                "topic": topic,
                "disposition": "keep",
                "reason": "independent current prerequisites",
                "removals": [],
                "media": [],
                "reference_edits": [
                    {
                        "address": r["address"],
                        "source_sha256": r["source_sha256"],
                        "reason": "exact retired URL; prerequisite remains current",
                    }
                    for r in source["reference_spans"]
                ]
                if topic == "smsv2-current"
                else [],
            }
            for topic in TOPICS
        ],
    }
    return source, proposal


def test_exact_unlink_keeps_prerequisites_order_commands_and_numeric_limits(tmp_path):
    source, proposal = inputs()
    candidate = compile_proposal(source, proposal)
    assert OLD_VM not in candidate["body"] and OLD_TOKEN not in candidate["body"]
    for text in [
        "Deployed a VMware site with 8 GiB",
        "Generated a site token before filling the Token field",
        "Keep TLS enabled",
        "within 30 seconds",
        "kubectl get pods -n app",
        "VMware prerequisite",
        "Token prerequisite",
    ]:
        assert text in candidate["body"]
    assert candidate == compile_proposal(source, proposal)
    for stage in candidate["stages"]:
        assert (
            remove_blocks(stage["input_body"], stage["document"]["block_removals"])
            == stage["output_body"]
        )
    raw = deepcopy(load_curation_policy().raw)
    for stage in candidate["stages"]:
        topic = next(t for t in raw["topics"] if t["id"] == stage["topic"])
        topic["documents"] = [d for d in topic["documents"] if d["url"] != source["identity"]] + [
            stage["document"]
        ]
    policy = CurationPolicy(raw, sha(json_bytes(raw)))
    path = (
        tmp_path
        / "content/docs-cloud-f5-com/multi-cloud-network-connect/how-to/site-management/site-local-ui-usage/index.md"
    )
    path.parent.mkdir(parents=True)
    path.write_text(
        serialize_document(PageMetadata.model_validate(source["metadata"]), source["body"])
    )
    migrate_content(tmp_path, curation_policy=policy)
    validate_content_policy(tmp_path, curation_policy=policy)
    assert "8 GiB" in path.read_text() and "Token prerequisite" in path.read_text()
    first = corpus_digest(tmp_path)
    migrate_content(tmp_path, curation_policy=policy)
    validate_content_policy(tmp_path, curation_policy=policy)
    assert corpus_digest(tmp_path) == first


def test_inline_reference_fact_guards_do_not_allow_payload_or_prerequisite_changes():
    source, proposal = inputs()
    candidate = compile_proposal(source, proposal)
    decision = validation(source, candidate)
    retained = blocks(candidate["body"])
    originals = {b["address"]: b for b in source["blocks"]}
    for fact in decision["facts"]:
        expected = projected_fact(source, candidate, originals[fact["source_address"]])
        matches = [b for b in retained if b.text == expected]
        fact.update(
            necessary=True,
            preserved=True,
            candidate_addresses=[b.location for b in matches],
            candidate_sha256=[b.sha256 for b in matches],
        )
    verify_validation(source, candidate, decision)
    candidate["body"] = candidate["body"].replace("8 GiB", "4 GiB")
    candidate["output_sha256"] = sha(
        json_bytes({k: v for k, v in candidate.items() if k != "output_sha256"})
    )
    decision["output_sha256"] = candidate["output_sha256"]
    retained = blocks(candidate["body"])
    for fact in decision["facts"]:
        fact["candidate_sha256"] = [
            next(b.sha256 for b in retained if b.location == a) for a in fact["candidate_addresses"]
        ]
    with pytest.raises(ValueError, match="retained fact or payload"):
        verify_validation(source, candidate, decision)


@pytest.mark.parametrize("change", ["digest", "unknown", "topic", "duplicate", "overlap"])
def test_unlink_cannot_accept_stale_unclassified_or_overlapping_source_edits(change):
    source, proposal = inputs()
    topic = next(t for t in proposal["topics"] if t["topic"] == "smsv2-current")
    if change == "digest":
        topic["reference_edits"][0]["source_sha256"] = "f" * 64
    elif change == "unknown":
        source = proposal_context(
            source["metadata"],
            source["body"].replace(OLD_VM, "https://example.com/current"),
            {},
            "a" * 64,
            load_curation_policy().sha256,
        )
        proposal["binding_sha256"] = source["binding_sha256"]
        topic["reference_edits"] = [
            {
                "address": r["address"],
                "source_sha256": r["source_sha256"],
                "reason": "invalid removal",
            }
            for r in source["reference_spans"]
        ]
    elif change == "topic":
        proposal["topics"][0]["reference_edits"] = topic["reference_edits"]
        topic["reference_edits"] = []
    elif change == "duplicate":
        topic["reference_edits"] *= 2
    else:
        item = source["reference_spans"][0]
        block = next(b for b in source["blocks"] if b["address"] == item["block_address"])
        topic["removals"] = [
            {"address": block["address"], "source_sha256": block["sha256"], "reason": "overlap"}
        ]
    with pytest.raises(ValueError):
        compile_proposal(source, proposal)


def test_code_payloads_and_images_are_not_editable_link_references():
    body = (
        "Inline `payload [label]("
        + OLD_VM
        + ")` remains.\n\n```sh\necho '[label]("
        + OLD_VM
        + ")'\n```\n\n![image]("
        + OLD_VM
        + ")\n\n[`site_token`]("
        + OLD_TOKEN
        + ") is a label to retain.\n"
    )
    refs = reference_spans(body)
    assert len(refs) == 1 and refs[0]["label"] == "`site_token`"


def test_noop_topic_preserves_authored_blank_section():
    body = "# Diagnostics\n\n## Answer/Recommended Actions\n\n## Verification\n\nExpect a healthy site within 30 seconds.\n"
    assert remove_blocks(body, []) == body


def test_dns_retirement_alias_is_limited_to_its_declared_identity_and_authority():
    topic = next(t for t in load_curation_policy().topics if t["id"] == "legacy-retired")
    url = "https://www.volterra.io/docs/how-to/app-networking/domain-delegation"
    assert retired_reference(topic, url)
    assert not retired_reference(topic, url.replace("www.volterra.io", "unrelated.invalid"))
    assert not retired_reference({**topic, "retired_identities": []}, url)
    assert not retired_reference(topic, url + "?token=example")


def test_actual_retirement_removal_preserves_unrelated_authored_blank_heading():
    body = "# Diagnostics\n\n## Answer/Recommended Actions\n\n## Retired instructions\n\nUse retired_tool.\n\n## Verification\n\nExpect a healthy site within 30 seconds.\n"
    retired = next(b for b in blocks(body) if b.kind == "paragraph" and "retired_tool" in b.text)
    output = remove_blocks(body, [{"location": retired.location, "sha256": retired.sha256}])
    assert "## Answer/Recommended Actions\n\n## Verification" in output
    assert "## Retired instructions" not in output
    assert "30 seconds" in output


def test_original_blank_descendant_keeps_context_after_neighboring_content_is_removed():
    body = "# Instructions\n\nUse retired_tool.\n\n## Optional field\n\n# Verification\n\nExpect a healthy site within 30 seconds.\n"
    retired = next(b for b in blocks(body) if b.kind == "paragraph" and "retired_tool" in b.text)
    output = remove_blocks(body, [{"location": retired.location, "sha256": retired.sha256}])
    assert output.startswith("# Instructions\n\n## Optional field\n\n# Verification")
    assert "retired_tool" not in output
