"""Pixel-bound edits must compose with policy application and protected image references."""

import io
from copy import deepcopy

import pytest
from PIL import Image

from html_to_markdown.curation import (
    CurationPolicy,
    corpus_digest,
    curate_topics,
    json_bytes,
    load_curation_policy,
    validate_curation,
)
from html_to_markdown.curation_renewal import (
    TOPICS,
    compile_proposal,
    projected_fact,
    proposal_context,
)
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.models import PageMetadata
from html_to_markdown.render import serialize_document, split_document


def prepared():
    out = io.BytesIO()
    Image.new("RGB", (30, 20), (90, 120, 160)).save(out, format="PNG")
    raw = out.getvalue()
    digest = sha(raw)
    metadata = PageMetadata(
        sourceId="docs-cloud-f5-com",
        url="https://docs.cloud.f5.com/docs-v2/how-to/current-dashboard",
        canonical_url="https://docs.cloud.f5.com/docs-v2/how-to/current-dashboard",
        title="Provisioning dashboard",
        slug="current-dashboard",
        category="Guides",
    ).model_dump(by_alias=True)
    body = f"# Provisioning dashboard\n\nWait for Provisioned status. Retain Kubernetes 1.23.\n\n![Provisioned controls](assets/{digest}.png)\n"
    context = proposal_context(
        metadata,
        body,
        {digest: {"frame_count": 1, "frames": []}},
        "a" * 64,
        load_curation_policy().sha256,
    )
    proposal = {
        "identity": context["identity"],
        "binding_sha256": context["binding_sha256"],
        "topics": [
            {
                "topic": topic,
                "disposition": "keep",
                "reason": "Independent current provisioning procedure",
                "removals": [],
                "reference_edits": [],
                "media": [
                    {
                        "sha256": digest,
                        "disposition": "keep",
                        "reason": "Retain required states and versions",
                        "masks": [
                            {
                                "frame": 0,
                                "rectangle": [2, 2, 10, 5],
                                "color": [246, 247, 249],
                                "reason": "Remove the separately retired banner",
                            }
                        ]
                        if topic == "appstack-retired"
                        else [],
                    }
                ],
            }
            for topic in TOPICS
        ],
        "unresolved": [],
    }
    return context, proposal, raw, digest


def policy_for(context, candidate):
    raw = deepcopy(load_curation_policy().raw)
    for stage in candidate["stages"]:
        topic = next(t for t in raw["topics"] if t["id"] == stage["topic"])
        topic["documents"] = [d for d in topic["documents"] if d["url"] != context["identity"]]
        topic["documents"].append(
            {
                **stage["document"],
                "reason": "independently_validated_current_source",
                "renewal_binding_sha256": "a" * 64,
                "renewal_validation_sha256": "b" * 64,
            }
        )
        for choice in stage["media"]:
            promoted = {
                **choice,
                "renewal_binding_sha256": "a" * 64,
                "renewal_validation_sha256": "b" * 64,
            }
            topic["media"].append(promoted)
            if "transformation" in choice:
                topic["media"].append(
                    {
                        "sha256": choice["output_sha256"],
                        "disposition": "keep",
                        "documents": [context["identity"]],
                        "reason": "independently_validated_retirement_output",
                    }
                )
    return CurationPolicy(raw, sha(json_bytes(raw)))


def test_compiled_mask_keeps_exact_image_caption_and_all_other_facts():
    context, proposal, raw, digest = prepared()
    with pytest.raises(ValueError, match="preserved source media"):
        compile_proposal(context, proposal)
    candidate = compile_proposal(context, proposal, {digest: raw})
    transformation = candidate["media_transforms"][digest]
    assert transformation["specification"]["source_sha256"] == digest
    assert transformation["output_sha256"] in candidate["body"]
    assert "Provisioned controls" in candidate["body"] and "Kubernetes 1.23" in candidate["body"]
    fact = next(
        b
        for b in context["blocks"]
        if "![Provisioned controls]" in b["text"] and b["kind"] == "paragraph"
    )
    assert projected_fact(context, candidate, fact) in candidate["body"]
    stage = next(s for s in candidate["stages"] if s["topic"] == "appstack-retired")
    assert stage["document"]["media_references"][0]["count"] == 1
    assert candidate["independent_validation_required"]


def test_examination_is_readonly_application_and_replay_are_identical(tmp_path):
    context, proposal, raw, digest = prepared()
    candidate = compile_proposal(context, proposal, {digest: raw})
    policy = policy_for(context, candidate)
    path = tmp_path / "content/docs-cloud-f5-com/how-to/current-dashboard/index.md"
    path.parent.mkdir(parents=True)
    asset = path.parent / "assets" / (digest + ".png")
    asset.parent.mkdir()
    asset.write_bytes(raw)
    path.write_text(
        serialize_document(PageMetadata.model_validate(context["metadata"]), context["body"])
    )
    before = corpus_digest(tmp_path)
    preview = curate_topics(tmp_path, policy=policy, apply=False)
    assert corpus_digest(tmp_path) == before
    assert preview["counts"]["keep"] == 1
    result = curate_topics(tmp_path, policy=policy)
    assert result["counts"]["keep"] == 1
    assert split_document(path.read_text())[1] == candidate["body"]
    validate_curation(tmp_path, policy=policy)
    after = corpus_digest(tmp_path)
    curate_topics(tmp_path, policy=policy)
    assert corpus_digest(tmp_path) == after
    assert not asset.exists()
    assert (
        asset.parent / (candidate["media_transforms"][digest]["output_sha256"] + ".png")
    ).is_file()


def test_wrong_document_or_unapproved_mask_never_mutates_assets(tmp_path):
    context, proposal, raw, digest = prepared()
    candidate = compile_proposal(context, proposal, {digest: raw})
    policy = policy_for(context, candidate)
    entry = next(m for t in policy.topics for m in t["media"] if "transformation" in m)
    entry.pop("renewal_validation_sha256")
    path = tmp_path / "content/docs-cloud-f5-com/how-to/current-dashboard/index.md"
    path.parent.mkdir(parents=True)
    asset = path.parent / "assets" / (digest + ".png")
    asset.parent.mkdir()
    asset.write_bytes(raw)
    path.write_text(
        serialize_document(PageMetadata.model_validate(context["metadata"]), context["body"])
    )
    before = corpus_digest(tmp_path)
    with pytest.raises(ValueError, match="independent placement"):
        curate_topics(tmp_path, policy=policy)
    assert corpus_digest(tmp_path) == before
