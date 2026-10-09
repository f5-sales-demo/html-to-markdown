"""Deterministic preservation gates complement the independent semantic validator."""

import json
import re
from collections import Counter
from typing import Any

from bs4 import BeautifulSoup
from pydantic import BaseModel

from ..curation import blocks
from ..render import normalize_body
from .analysis import links, sha
from .contracts import PROMPT_VERSION, Decision, ResponseEvidence, Validation


def parse_response(
    evidence: ResponseEvidence, schema: type[BaseModel], model: str, expected_request_hash: str
) -> Any:
    if (
        evidence.error
        or evidence.model != model
        or evidence.request_sha256 != expected_request_hash
    ):
        raise ValueError("failed, wrong-model or stale request evidence")
    if evidence.prompt_version != PROMPT_VERSION or evidence.reasoning != "high":
        raise ValueError("stale prompt or reasoning evidence")
    if request_hash(evidence.request) != evidence.request_sha256:
        raise ValueError("captured request digest mismatch")
    response = evidence.response
    if evidence.transport == "codex_litellm" and any(
        event.get("type") in {"context.compacted", "thread.compacted", "turn.failed"}
        or event.get("item", {}).get("type")
        in {"command_execution", "file_change", "mcp_tool_call", "web_search", "tool_call"}
        for event in response.get("events", [])
    ):
        raise ValueError("document worker used a tool or lost complete input context")
    if sha(json.dumps(response, sort_keys=True, separators=(",", ":"))) != evidence.response_sha256:
        raise ValueError("response digest mismatch")
    if response.get("status") != "completed" or response.get("model") != model:
        raise ValueError("incomplete or wrong-model response")
    content = [c for item in response.get("output", []) for c in item.get("content", [])]
    if any(c.get("type") == "refusal" for c in content):
        raise ValueError("model refusal")
    texts = [c["text"] for c in content if c.get("type") == "output_text"]
    if len(texts) != 1:
        raise ValueError("missing or ambiguous structured output")
    if evidence.output_selector is not None:
        parsed = json.loads(texts[0])
        matches = [
            item
            for item in parsed.get("images", [])
            if item.get("sha256") == evidence.output_selector
        ]
        if len(matches) != 1:
            raise ValueError("missing or duplicate digest in grouped image response")
        return schema.model_validate(matches[0])
    return schema.model_validate_json(texts[0])


def request_hash(body: dict[str, Any]) -> str:
    return sha(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def technical_payloads(body: str) -> list[str]:
    # Preserve fenced/indented code, inline identifiers, and literal numeric limits.
    payloads = [b.text for b in blocks(body) if b.kind in {"fence", "code_block"}]
    payloads += re.findall(r"(?<!`)`([^`\n]+)`(?!`)", body)
    payloads += re.findall(
        r"\b\d+(?:\.\d+)*(?:\s?(?:%|GB|MB|KB|ms|seconds?|minutes?|hours?|days?))", body
    )
    return payloads


# The atomic candidate gate checks all preservation invariants before mutation.
# pylint: disable-next=too-many-locals,too-many-branches,too-many-statements
def candidate(
    doc: dict[str, Any],
    decision: Decision,
    canonical_docs: dict[str, dict[str, Any]],
    repairs: dict[str, dict[str, Any]] | None = None,
) -> str:
    if decision.document != doc["document"] or decision.input_sha256 != doc["input_sha256"]:
        raise ValueError("document identity or input digest mismatch")
    by_address = {block["address"]: block for block in doc["blocks"]}
    classifications = [c.address for c in decision.classifications]
    if len(classifications) != len(set(classifications)) or set(classifications) != set(by_address):
        raise ValueError("incomplete or duplicate structural classifications")
    if decision.unresolved:
        raise ValueError("unresolved article requires whole-document fallback")
    if not decision.reason.strip():
        raise ValueError("missing whole-document rationale")
    if decision.disposition != "rewrite" and decision.edits:
        raise ValueError("non-rewrite disposition has edits")
    body = doc["body"]
    if decision.disposition == "alias":
        target = canonical_docs.get(decision.canonical_document or "")
        if target is None or target["document"] == doc["document"]:
            raise ValueError("missing canonical target")

        def canonical(d: dict[str, Any]) -> Any:
            return d["metadata"].get("canonical_url") or d["metadata"]["url"]

        if canonical(target) != canonical(doc) or target["body"] != body:
            raise ValueError("canonical identity and substantive content do not both match")
    elif decision.canonical_document is not None:
        raise ValueError("unexpected canonical target")
    edits = []
    lines = body.splitlines(keepends=True)
    for edit in decision.edits:
        source = by_address.get(edit.address)
        if (
            source is None
            or source["sha256"] != edit.source_sha256
            or source["text"] != edit.source
            or edit.count != 1
        ):
            raise ValueError("stale structural source guard or replacement count")
        if (
            not edit.reason.strip()
            or not edit.evidence
            or any(e not in by_address for e in edit.evidence)
        ):
            raise ValueError("unsupported replacement evidence")
        verified_repair = False
        if edit.repair_evidence is not None:
            repair = (repairs or {}).get(edit.repair_evidence)
            if repair is None or repair.get("document") != doc["document"]:
                raise ValueError("technical repair requires qualified authoritative evidence")
            if (
                repair.get("address") != edit.address
                or repair.get("source") != edit.source
                or repair.get("replacement") != edit.replacement
                or repair.get("kind") != "html_code_line_boundaries"
                or sha(repair.get("html", "")) != repair.get("html_sha256")
            ):
                raise ValueError("technical repair evidence mismatch")
            nodes = BeautifulSoup(repair["html"], "html.parser").select(".ec-line .code")
            if not nodes:
                raise ValueError("authoritative code line evidence missing")
            original_code_text = "".join(node.get_text() for node in nodes)
            repaired_code_text = "\n".join(node.get_text() for node in nodes)
            source_inner = "\n".join(edit.source.strip().splitlines()[1:-1])
            replacement_inner = "\n".join(edit.replacement.strip().splitlines()[1:-1])
            if source_inner != original_code_text or replacement_inner != repaired_code_text:
                raise ValueError("repair alters authoritative code tokens")
            verified_repair = True
        if (
            source["kind"] in {"fence", "code_block", "table", "table_row"}
            and edit.replacement != edit.source
            and not verified_repair
        ):
            raise ValueError("technical code/table payload changed")
        edits.append((source["start_line"], source["end_line"], edit.replacement))
    edits.sort()
    if any(a[1] > b[0] for a, b in zip(edits, edits[1:], strict=False)):
        raise ValueError("overlapping edits")
    for start, end, replacement in reversed(edits):
        lines[start:end] = [replacement]
    retained = normalize_body("".join(lines))
    if re.search(
        r"(?im)^.*(?:Return to Top|Show social share buttons|Cookie Settings).*$", retained
    ):
        raise ValueError("candidate contains navigation chrome")
    existing_links = set(doc["links"])
    for edit in decision.edits:
        if edit.repair_evidence is not None:
            existing_links.update(links(edit.replacement))
    if set(links(retained)) - existing_links:
        raise ValueError("new reference lacks supplied authoritative evidence")
    protected_source = body
    for edit in decision.edits:
        if edit.repair_evidence is not None:
            protected_source = protected_source.replace(edit.source, edit.replacement, 1)
    before, after = (
        Counter(technical_payloads(protected_source)),
        Counter(technical_payloads(retained)),
    )
    if any(after[value] < count for value, count in before.items()):
        raise ValueError("lost literal technical payload or numeric constraint")
    classifications_by_address = {c.address: c.classification for c in decision.classifications}
    for address, block in by_address.items():
        if classifications_by_address[address] in {
            "action",
            "constraint",
            "warning",
            "prerequisite",
            "example",
        }:
            for number in re.findall(r"\b\d+(?:\.\d+)*\b", block["text"]):
                if not re.search(r"\b" + re.escape(number) + r"\b", retained):
                    raise ValueError("lost protected numeric value")
    original_code = [b.text for b in blocks(protected_source) if b.kind in {"fence", "code_block"}]
    retained_code = [b.text for b in blocks(retained) if b.kind in {"fence", "code_block"}]
    if original_code != retained_code:
        raise ValueError("technical payload order changed")
    if decision.disposition in {"rewrite", "retain"}:
        if not decision.description.strip() or decision.description.rstrip()[-1] not in ".!?":
            raise ValueError("missing or incomplete grounded description")
        if not decision.description_evidence or any(
            e not in by_address for e in decision.description_evidence
        ):
            raise ValueError("missing description evidence")
    for fact in decision.protected_facts:
        source = by_address.get(fact.address)
        if (
            not fact.source_quote
            or source is None
            or fact.source_quote not in source["text"]
            or not fact.retained_quote
            or fact.retained_quote not in retained
        ):
            raise ValueError("protected fact quote absent from source or candidate")
    if any(not p or p not in retained for p in decision.retained_prerequisites):
        raise ValueError("necessary prerequisite absent")
    media = {m["reference"]: m for m in doc["media"]}
    placements = {m.reference: m for m in decision.media}
    if len(placements) != len(decision.media) or set(media) != set(placements):
        raise ValueError("incomplete or duplicate media review")
    for reference, review in placements.items():
        if review.sha256 != media[reference]["sha256"] or not review.evidence.strip():
            raise ValueError("changed image bytes or missing contextual media evidence")
        if review.disposition == "remove" and reference in retained:
            raise ValueError("removed media remains referenced")
        if review.disposition != "remove" and reference not in retained:
            raise ValueError("retained or uncertain media lost")
    return retained


def validate_candidate(doc: dict[str, Any], body: str, validation: Validation) -> None:
    if (
        validation.document != doc["document"]
        or validation.input_sha256 != doc["input_sha256"]
        or validation.candidate_sha256 != sha(body)
    ):
        raise ValueError("stale independent validation")
    required = [
        validation.passed,
        validation.disposition_valid,
        validation.description_grounded,
        validation.coherent_standalone,
        validation.action_order_preserved,
        validation.all_necessary_facts_preserved,
        validation.media_valid,
        validation.classifications_complete,
    ]
    if (
        not all(required)
        or validation.failures
        or validation.unresolved
        or validation.unsupported_claims
    ):
        raise ValueError("independent preservation or relevance validation failed")
    if doc["body"].strip() and not validation.protected_facts:
        raise ValueError("independent fact inventory missing")
    for fact in validation.protected_facts:
        if (
            not fact.preserved
            or not fact.source_quote
            or fact.source_quote not in doc["body"]
            or not fact.retained_quote
            or fact.retained_quote not in body
        ):
            raise ValueError("independent protected fact lost")
