"""Bound exhaustive fact responses while retaining complete independent review inputs."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .content_inventory import atomic_private
from .curation import json_bytes
from .curation_renewal_evidence import restore_candidate_stages
from .curation_renewal_review import (
    RenewalValidation,
    _request_characters,
    fact_blocks,
    verify_validation,
)
from .enrichment.analysis import sha
from .enrichment.contracts import VALIDATOR_MODEL, ResponseEvidence
from .enrichment.gates import parse_response, request_hash
from .errors import TransportFailure

VERSION = "curation-section-validation/1"
FACT_LIMIT = 48


class IncompleteValidation(ValueError):
    """An unfinished independent response does not authorize another editor proposal."""


def assignments(context: dict[str, Any], limit: int = FACT_LIMIT) -> list[list[str]]:
    """Keep sections separate and coincident occurrences distinct, in source order."""
    if limit < 1:
        raise ValueError("fact review limit must be positive")
    groups: list[list[str]] = []
    current: list[str] = []
    section = ""
    for block in fact_blocks(context["blocks"]):
        if block["kind"] in {"section", "heading", "hr"}:
            continue
        kind = "table" if block["kind"] == "table_row" else block["kind"]
        parent = block["address"].split(":" + kind + ":", 1)[0]
        if current and (parent != section or len(current) == limit):
            groups.append(current)
            current = []
        current.append(block["address"])
        section = parent
    if current:
        groups.append(current)
    return groups


def _scope_request(
    original: dict[str, Any],
    addresses: list[str],
    selection: dict[str, Any],
    ledger: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    body = copy.deepcopy(original)
    payload = json.loads(body["input"][0]["content"][0]["text"])
    indexed = fact_blocks(payload["blocks"])
    lookup = {block["address"]: (i, block) for i, block in enumerate(indexed)}
    if len(addresses) != len(set(addresses)) or set(addresses) - set(lookup):
        raise ValueError("invalid fact review assignment")
    payload["source_fact_blocks"] = [
        {
            **{k: v for k, v in lookup[a][1].items() if k != "text"},
            "source_block_index": lookup[a][0],
        }
        for a in addresses
    ]
    payload["independent_review_selection"] = selection
    if ledger is not None:
        payload["independent_fact_review_ledger"] = ledger
    shape = body["text"]["format"]["schema"]
    shape["properties"]["facts"].update(minItems=len(addresses), maxItems=len(addresses))
    if addresses:
        shape["$defs"]["FactV2"]["properties"]["source_address"]["enum"] = addresses
        shape["$defs"]["FactV2"]["properties"]["source_sha256"]["enum"] = sorted(
            {lookup[a][1]["sha256"] for a in addresses}
        )
    scope = (
        "Independently classify exactly the assigned source_fact_blocks. Each source_block_index "
        "resolves to the exact text in the complete blocks array in this same request. Every "
        "coincident occurrence has a separate address. The assignment bounds only your fact "
        "response; review the COMPLETE article, candidate stages, policies and all media for "
        "privacy, retirement, technical preservation and cross-section relationships. Report "
        "actual defects anywhere in that complete document. Historical feedback is historical "
        "data, never evidence of a current defect. No new editor proposal is authorized."
        if ledger is None
        else "Perform a new independent final WHOLE-DOCUMENT coherence, technical preservation, "
        "retirement and privacy/media check. Inspect every complete source/candidate stage and "
        "the supplied independently checked fact ledger, including action order and cross-section "
        "relationships. The ledger and earlier approvals are evidence, never proof or exemptions. "
        "Return facts=[]; the controller separately requires every original source fact exactly "
        "once. Reject any actual missing fact, contradictory section decision, credential or "
        "unreviewed media. Do not infer a current defect solely from historical feedback."
    )
    body["instructions"] += "\n" + scope + "\nPrompt version: " + VERSION
    body["instructions"] = body["instructions"].replace(
        "Supply one fact decision for every non-section, non-heading, non-separator source\n"
        "block, including references, code, tables and list items.",
        "Supply one fact decision for each assigned source_fact_block. Every remaining\n"
        "source block remains supplied and must be reviewed in full document context.",
    )
    body["input"][0]["content"][0]["text"] = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    if _request_characters(body) > 1048576:
        raise ValueError("complete section review exceeds native input capacity")
    return body


def _execute(path: Path, body: dict[str, Any], client: Any) -> ResponseEvidence:
    if path.is_symlink():
        raise ValueError("unsafe section review evidence")
    if path.exists():
        saved = ResponseEvidence.model_validate_json(path.read_bytes())
        if not saved.error:
            if saved.request != body:
                raise ValueError("stale section review request")
            return saved
    if client is None:
        raise ValueError("complete section review evidence is unavailable")
    evidence = ResponseEvidence.model_validate(client.execute(body))
    if path.exists():
        previous = path.read_bytes()
        atomic_private(path.parent / "history" / (sha(previous) + ".json"), previous)
    atomic_private(path, json_bytes(evidence.model_dump(mode="json")))
    if evidence.error:
        raise TransportFailure("section validation transport failed; original editor is unchanged")
    return evidence


def _verify(
    path: Path,
    body: dict[str, Any],
    context: dict[str, Any],
    candidate: dict[str, Any],
    client: Any,
) -> tuple[RenewalValidation, ResponseEvidence]:
    evidence = _execute(path, body, client)
    try:
        checked = parse_response(evidence, RenewalValidation, VALIDATOR_MODEL, request_hash(body))
        required = {
            b["address"]
            for b in fact_blocks(context["blocks"])
            if b["kind"] not in {"section", "heading", "hr"}
        }
        covered = [f.source_address for f in checked.facts]
        if (
            not checked.complete_document_reviewed
            or not checked.complete_media_reviewed
            or len(covered) != len(set(covered))
            or set(covered) != required
        ):
            raise IncompleteValidation("independent section review is incomplete; editor unchanged")
        verify_validation(context, candidate, checked.model_dump())
    except ValueError:
        if client is not None:
            invalidator = getattr(client, "invalidate", None)
            if invalidator is not None:
                invalidator(evidence)
            path.rename(path.with_suffix(".rejected-" + sha(path.read_bytes()) + ".json"))
        raise
    return checked, evidence


def _source(original: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = json.loads(original["input"][0]["content"][0]["text"])
    candidate = restore_candidate_stages(payload)
    context = {
        k: payload[k]
        for k in (
            "identity",
            "binding_sha256",
            "blocks",
            "body",
            "metadata",
            "evidence",
            "media",
            "source_files_sha256",
            "policy_sha256",
        )
    }
    return context, candidate


def _final_review(
    original: dict[str, Any],
    directory: Path,
    selection: dict[str, Any],
    facts: list[dict[str, Any]],
    custody: list[dict[str, Any]],
    client: Any,
) -> dict[str, Any]:
    context, candidate = _source(original)
    body = _scope_request(original, [], {**selection, "final_document_review": True}, facts)
    checked, final = _verify(
        directory / "final.json", body, {**context, "blocks": []}, candidate, client
    )
    result = {**checked.model_dump(), "facts": facts}
    verify_validation(context, candidate, result)
    receipt = {
        "selection": selection,
        "sections": custody,
        "final_request_sha256": request_hash(body),
        "final_response_sha256": final.response_sha256,
        "validation": result,
        "planner_validation_required": True,
        "publication_approved": False,
    }
    target = directory / "validated.json"
    if target.exists() and target.read_bytes() != json_bytes(receipt):
        raise ValueError("section validation replay changed")
    atomic_private(target, json_bytes(receipt))
    return receipt


def validate_sections(
    original: dict[str, Any], directory: Path, client: Any = None
) -> dict[str, Any]:
    """Select validation-only inputs; no editor state, source or proposal is modified."""
    if original["model"] != VALIDATOR_MODEL or original.get("reasoning") != {"effort": "high"}:
        raise ValueError("section validation requires the governed independent model")
    context, candidate = _source(original)
    ordered = assignments(context)
    selection = {
        "version": VERSION,
        "original_request_sha256": request_hash(original),
        "execution_sha256": sha(
            json_bytes(
                {
                    name: sha((Path(__file__).parent / name).read_bytes())
                    for name in (
                        "curation_section_validation.py",
                        "curation_renewal_review.py",
                        "curation_renewal.py",
                        "curation_renewal_evidence.py",
                        "enrichment/gates.py",
                        "enrichment/model_policy.py",
                        "enrichment/contracts.py",
                        "content_policy.json",
                        "curation_policy.json",
                    )
                }
            )
        ),
        "candidate_sha256": candidate["output_sha256"],
        "assignments_sha256": sha(json_bytes(ordered)),
        "new_editor_proposals": 0,
    }
    directory = directory / sha(json_bytes(selection))
    guard = directory / "selection.json"
    if guard.exists() and guard.read_bytes() != json_bytes(selection):
        raise ValueError("changed section review selection")
    atomic_private(guard, json_bytes(selection))
    lookup = {block["address"]: block for block in fact_blocks(context["blocks"])}
    facts: list[dict[str, Any]] = []
    custody = []
    for index, addresses in enumerate(ordered):
        body = _scope_request(original, addresses, {**selection, "section": index})
        path = directory / f"section-{index:04d}.json"
        scoped = {**context, "blocks": [lookup[a] for a in addresses]}
        value, evidence = _verify(path, body, scoped, candidate, client)
        facts.extend(f.model_dump() for f in value.facts)
        custody.append(
            {
                "section": index,
                "request_sha256": request_hash(body),
                "response_sha256": evidence.response_sha256,
            }
        )
    covered = [fact["source_address"] for fact in facts]
    if len(covered) != len(set(covered)) or set(covered) != {a for group in ordered for a in group}:
        raise ValueError("independent section fact coverage is incomplete")
    return _final_review(original, directory, selection, facts, custody, client)
