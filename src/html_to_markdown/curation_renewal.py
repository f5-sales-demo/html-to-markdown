"""Private, exact-span curation proposals awaiting independent planner validation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from .curation import blocks, json_bytes, remove_blocks
from .enrichment.analysis import sha
from .enrichment.contracts import StrictModel
from .legacy_curation import LegacyFilter
from .urls import validate_source_url
from .vesctl_curation import VesctlFilter

TOPICS = (
    "legacy-retired",
    "smsv2-current",
    "appstack-retired",
    "vesctl-retired",
    "terraform-provider-current",
)
VERSION = "curation-renewal/1"


class Removal(StrictModel):
    address: str
    source_sha256: str
    reason: str


class MediaChoice(StrictModel):
    sha256: str
    disposition: Literal["keep", "remove"]
    reason: str


class TopicDecision(StrictModel):
    topic: str
    disposition: Literal["keep", "remove"]
    reason: str
    removals: list[Removal]
    media: list[MediaChoice]


class Proposal(StrictModel):
    identity: str
    binding_sha256: str
    topics: list[TopicDecision]
    unresolved: list[str]


def proposal_context(
    metadata: dict[str, Any],
    body: str,
    media: dict[str, Any],
    source_files_sha256: str,
    policy_sha256: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind complete source fields, native media, policy and controller semantics."""
    url = validate_source_url(metadata["sourceId"], metadata["url"])
    if any(
        not re.fullmatch(r"[0-9a-f]{64}", pin)
        for pin in [source_files_sha256, policy_sha256, *media]
    ):
        raise ValueError("renewal requires complete source, policy and media digests")
    result = {
        "identity": url,
        "version": VERSION,
        "metadata": metadata,
        "body": body,
        "media": media,
        "source_files_sha256": source_files_sha256,
        "policy_sha256": policy_sha256,
        "evidence": evidence or {},
        "controller_sha256": sha(
            json_bytes(
                {
                    name: sha((Path(__file__).parent / name).read_bytes())
                    for name in (
                        "curation_renewal.py",
                        "curation_renewal_review.py",
                        "curation_renewal_jobs.py",
                        "curation_renewal_inputs.py",
                        "curation_renewal_policy.py",
                        "curation_renewal_publication.py",
                        "curation.py",
                        "content_policy.py",
                        "enrichment/gates.py",
                        "enrichment/requests.py",
                    )
                }
            )
        ),
        "topics": list(TOPICS),
        "blocks": [
            {
                "address": block.location,
                "sha256": block.sha256,
                "kind": block.kind,
                "start": block.start,
                "end": block.end,
                "text": block.text,
            }
            for block in blocks(body)
        ],
    }
    result["binding_sha256"] = sha(json_bytes(result))
    return result


def _guarded_removals(
    context: dict[str, Any], proposal: Proposal
) -> dict[str, list[dict[str, Any]]]:
    indexed = {block["address"]: block for block in context["blocks"]}
    chosen = []
    result: dict[str, list[dict[str, Any]]] = {}
    for topic in proposal.topics:
        result[topic.topic] = []
        for removal in topic.removals:
            block = indexed.get(removal.address)
            if (
                block is None
                or block["sha256"] != removal.source_sha256
                or not removal.reason.strip()
            ):
                raise ValueError("stale or absent renewal structural guard")
            chosen.append(block)
            result[topic.topic].append(block)
    chosen.sort(key=lambda block: (block["start"], block["end"]))
    if any(left["end"] > right["start"] for left, right in zip(chosen, chosen[1:], strict=False)):
        raise ValueError("overlapping curation renewal spans across topics")
    return result


def _stage_rules(body: str, removals: list[dict[str, Any]]) -> list[dict[str, str]]:
    parsed = blocks(body)
    result = []
    for original in removals:
        matches = [
            block
            for block in parsed
            if block.kind == original["kind"]
            and block.text == original["text"]
            and block.sha256 == original["sha256"]
        ]
        if len(matches) != 1:
            raise ValueError("ambiguous or changed renewal span after preceding topic")
        result.append({"location": matches[0].location, "sha256": matches[0].sha256})
    return result


def compile_proposal(context: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
    """Retrieve exact strings deterministically; compilation grants no approval."""
    proposal = Proposal.model_validate(value)
    expected_context = proposal_context(
        context["metadata"],
        context["body"],
        context["media"],
        context["source_files_sha256"],
        context["policy_sha256"],
        context.get("evidence"),
    )
    if (
        context != expected_context
        or proposal.identity != context["identity"]
        or proposal.binding_sha256 != context["binding_sha256"]
    ):
        raise ValueError("stale curation renewal source or request binding")
    if proposal.unresolved:
        raise ValueError("curation renewal proposal has unresolved source decisions")
    if [topic.topic for topic in proposal.topics] != list(TOPICS):
        raise ValueError("renewal must resolve every policy topic exactly once in order")
    guarded = _guarded_removals(context, proposal)
    current = context["body"]
    stages = []
    disposition = "keep"
    for topic in proposal.topics:
        if not topic.reason.strip():
            raise ValueError("renewal topic lacks contextual rationale")
        media_ids = [choice.sha256 for choice in topic.media]
        if (
            len(media_ids) != len(set(media_ids))
            or set(media_ids) != set(context["media"])
            or any(not choice.reason.strip() for choice in topic.media)
        ):
            raise ValueError("renewal must review every exact media placement for each topic")
        rules = _stage_rules(current, guarded[topic.topic])
        retained = remove_blocks(current, rules)
        document = {
            "url": context["identity"],
            "disposition": topic.disposition,
            "reason": "independent_curation_validation_required",
            "input_sha256": sha(current),
            "output_sha256": sha(retained),
            "block_removals": rules,
            "review_index": None,
        }
        if topic.topic == "legacy-retired":
            document["source_sha256"] = LegacyFilter.source_digest(context["metadata"], current)
        elif topic.topic == "vesctl-retired":
            document["source_sha256"] = VesctlFilter.source_digest(context["metadata"], current)
        stages.append(
            {
                "topic": topic.topic,
                "document": document,
                "input_body": current,
                "output_body": retained,
                "media": [
                    {
                        "sha256": choice.sha256,
                        "disposition": choice.disposition,
                        "documents": [context["identity"]],
                        "reason": "independent_curation_validation_required",
                    }
                    for choice in topic.media
                ],
            }
        )
        if topic.disposition == "remove":
            disposition = "remove"
        current = retained
    result = {
        "identity": context["identity"],
        "binding_sha256": context["binding_sha256"],
        "disposition": disposition,
        "body": current,
        "stages": stages,
        "proposal_sha256": sha(json_bytes(proposal.model_dump())),
        "independent_validation_required": True,
    }
    result["output_sha256"] = sha(json_bytes(result))
    return result
