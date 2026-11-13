"""Private, exact-span curation proposals awaiting independent planner validation."""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from .curation import (
    blocks,
    canonical,
    json_bytes,
    load_curation_policy,
    reference_spans,
    remove_blocks,
    unlink_references,
)
from .curation_renewal_media import projected_media_fact, reviewed_output_media, stage_media
from .curation_spans import prose_guard
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
SCHEMA_VERSION = 2


class ProseSpan(StrictModel):
    start: int
    end: int


class Removal(StrictModel):
    address: str
    source_sha256: str
    reason: str
    prose_span: ProseSpan | None = None


class MediaMask(StrictModel):
    frame: int
    rectangle: list[int]
    color: list[int]
    reason: str


class MediaChoice(StrictModel):
    sha256: str
    disposition: Literal["keep", "remove"]
    reason: str
    masks: list[MediaMask] = []


class ReferenceEdit(StrictModel):
    address: str
    source_sha256: str
    reason: str


class TopicDecision(StrictModel):
    topic: str
    disposition: Literal["keep", "remove"]
    reason: str
    removals: list[Removal]
    media: list[MediaChoice]
    reference_edits: list[ReferenceEdit]


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
        "schema_version": SCHEMA_VERSION,
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
                        "curation_renewal_media.py",
                        "curation_media.py",
                        "curation_spans.py",
                        "curation_api_evidence.py",
                        "curation_api_chain.py",
                        "curation_renewal_review.py",
                        "curation_renewal_evidence.py",
                        "curation_renewal_jobs.py",
                        "curation_renewal_inputs.py",
                        "curation_renewal_dependencies.py",
                        "curation_renewal_policy.py",
                        "curation_renewal_publication.py",
                        "curation.py",
                        "appstack_curation.py",
                        "legacy_curation.py",
                        "vesctl_curation.py",
                        "terraform_curation.py",
                        "content_policy.py",
                        "enrichment/gates.py",
                        "enrichment/requests.py",
                    )
                }
            )
        ),
        "topics": list(TOPICS),
        "reference_spans": reference_spans(body),
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
            selected = dict(block)
            if removal.prose_span is not None:
                selected["prose_span"] = prose_guard(
                    block["text"], block["kind"], removal.prose_span.model_dump()
                )
            chosen.append(selected)
            result[topic.topic].append(selected)
    chosen.sort(key=lambda block: (block["start"], block["end"]))
    if any(left["end"] > right["start"] for left, right in zip(chosen, chosen[1:], strict=False)):
        raise ValueError("overlapping curation renewal spans across topics")
    return result


def retired_reference(topic: dict[str, Any], url: str) -> bool:
    """Apply declared identities and known documentation aliases, never arbitrary domains."""
    from urllib.parse import urlsplit  # pylint: disable=import-outside-toplevel

    parsed = urlsplit(url)
    if parsed.query or parsed.username or parsed.password or "%" in url:
        return False
    value = canonical(url)
    if value in {canonical(item) for item in topic["retired_urls"]}:
        return True
    if topic["id"] == "legacy-retired" and "dns_domain" in topic["retired_identities"]:
        return (
            parsed.scheme == "https"
            and parsed.netloc == "www.volterra.io"
            and parsed.path.rstrip("/") == "/docs/how-to/app-networking/domain-delegation"
        )
    if topic["id"] == "terraform-provider-current":
        return any(
            value == canonical(item) or value.startswith(canonical(item) + "/")
            for item in topic.get("retired_documentation_urls", [])
        )
    return False


def _reference_choices(
    context: dict[str, Any], proposal: Proposal
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {topic.topic: [] for topic in proposal.topics}
    if not any(topic.reference_edits for topic in proposal.topics):
        return result
    policy = load_curation_policy()
    if policy.sha256 != context["policy_sha256"]:
        raise ValueError("retired reference policy has been superseded")
    indexed = {item["address"]: item for item in context["reference_spans"]}
    chosen = set()
    for decision in proposal.topics:
        topic = next(t for t in policy.topics if t["id"] == decision.topic)
        for edit in decision.reference_edits:
            item = indexed.get(edit.address)
            if (
                item is None
                or item["source_sha256"] != edit.source_sha256
                or not edit.reason.strip()
            ):
                raise ValueError("stale or absent retired reference guard")
            if not retired_reference(topic, item["url"]):
                raise ValueError(
                    f"reference edit lacks an explicit topic retirement decision: topic={decision.topic}; address={edit.address}"
                )
            if edit.address in chosen:
                raise ValueError("overlapping retired reference choices")
            chosen.add(edit.address)
            result[decision.topic].append(item)
    return result


def _stage_references(body: str, selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    available = reference_spans(body)
    grouped: dict[str, dict[str, Any]] = {}
    for original in selected:
        prefix = original["block_address"].rsplit(":", 1)[0]
        matches = [
            item
            for item in available
            if item["source_sha256"] == original["source_sha256"]
            and item["block_address"].rsplit(":", 1)[0] == prefix
        ]
        if len(matches) != 1:
            raise ValueError("retired reference changed after preceding topic")
        current = matches[0]
        rule = grouped.setdefault(
            current["block_address"],
            {
                "location": current["block_address"],
                "sha256": current["block_sha256"],
                "unlink_reference_sha256": [],
            },
        )
        rule["unlink_reference_sha256"].append(current["source_sha256"])
    return list(grouped.values())


def projected_fact(
    context: dict[str, Any], candidate: dict[str, Any], source: dict[str, Any]
) -> str:
    """Only compiled, exact retired-link edits may alter a retained source fact's bytes."""
    lines = context["body"].splitlines(keepends=True)
    start = sum(len(line) for line in lines[: source["start"]])
    end = sum(len(line) for line in lines[: source["end"]])
    selected = [
        {**item, "start": item["start"] - start, "end": item["end"] - start}
        for item in candidate.get("reference_edits", [])
        if start <= item["start"] and item["end"] <= end
    ]
    text = source["text"]
    for removal in sorted(
        candidate.get("prose_removals", []), key=lambda item: item["start"], reverse=True
    ):
        if start <= removal["start"] and removal["end"] <= end:
            left, right = removal["start"] - start, removal["end"] - start
            if sha(text[left:right]) != removal["sha256"]:
                raise ValueError("partial prose fact projection digest changed")
            text = text[:left] + text[right:]
    return projected_media_fact(unlink_references(text, selected), candidate)


def _stage_rules(body: str, removals: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
        rule: dict[str, Any] = {"location": matches[0].location, "sha256": matches[0].sha256}
        if "prose_span" in original:
            rule["prose_span"] = original["prose_span"]
        result.append(rule)
    return result


def _prose_removal_records(
    context: dict[str, Any], guarded: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    prose_removals = []
    lines = context["body"].splitlines(keepends=True)
    for removals in guarded.values():
        for block in removals:
            if "prose_span" not in block:
                continue
            offset = sum(len(line) for line in lines[: block["start"]])
            span = block["prose_span"]
            prose_removals.append(
                {
                    "source_address": block["address"],
                    "source_sha256": block["sha256"],
                    "start": offset + span["start"],
                    "end": offset + span["end"],
                    "sha256": span["sha256"],
                }
            )
    return prose_removals


# Full source, topic, reference and pixel guards remain one deterministic transaction.
# pylint: disable-next=too-many-locals
def compile_proposal(
    context: dict[str, Any], value: dict[str, Any], media_bytes: dict[str, bytes] | None = None
) -> dict[str, Any]:
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
    references = _reference_choices(context, proposal)
    current = context["body"]
    current_media = {digest: digest for digest in context["media"]}
    media_transforms: dict[str, Any] = {}
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
            missing = sorted(set(context["media"]) - set(media_ids))
            unexpected = sorted(set(media_ids) - set(context["media"]))
            raise ValueError(
                f"renewal must review every exact media placement for each topic: topic={topic.topic}; expected={len(context['media'])}; actual={len(media_ids)}; missing={missing}; unexpected={unexpected}"
            )
        rules = [
            *_stage_rules(current, guarded[topic.topic]),
            *_stage_references(current, references[topic.topic]),
        ]
        retained = remove_blocks(current, rules)
        retained, media_reviews, media_references = stage_media(
            context, topic, retained, media_bytes or {}, current_media, media_transforms
        )
        document = {
            "url": context["identity"],
            "disposition": topic.disposition,
            "reason": "independent_curation_validation_required",
            "input_sha256": sha(current),
            "output_sha256": sha(retained),
            "block_removals": rules,
            "review_index": None,
        }
        if media_references:
            document["media_references"] = media_references
        if topic.topic == "appstack-retired" and topic.disposition == "keep":
            appstack = load_curation_policy().appstack
            if appstack is not None and (notices := appstack.notice_rules(retained)):
                # Compilation only proposes guards; independent review and the
                # actual whole-corpus planner must qualify their full context.
                document["retained_notices"] = notices
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
                "media": media_reviews,
            }
        )
        if topic.disposition == "remove":
            disposition = "remove"
        current = retained
    reviewed_output_media(context, stages, media_transforms)
    for stage in stages:
        stage["document"]["accepted_output_sha256"] = sorted(
            {stage["document"]["output_sha256"], sha(current)}
        )
    result = {
        "identity": context["identity"],
        "binding_sha256": context["binding_sha256"],
        "disposition": disposition,
        "body": current,
        "stages": stages,
        "proposal_sha256": sha(json_bytes(value)),
        "proposal": deepcopy(value),
        "reference_edits": [item for topic in proposal.topics for item in references[topic.topic]],
        "independent_validation_required": True,
    }
    if prose_removals := _prose_removal_records(context, guarded):
        result["prose_removals"] = prose_removals
    if media_transforms:
        result["media_transforms"] = media_transforms
    result["output_sha256"] = sha(json_bytes(result))
    return result
