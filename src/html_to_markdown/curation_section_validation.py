"""Bound exhaustive fact responses while retaining complete independent review inputs."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .content_inventory import atomic_private
from .curation import json_bytes
from .curation_fact_ledger import index_ledger, sizing_ledger
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
# Immutable deployed section protocol; only its final ledger representation changes.
LEGACY_SECTION_SHA256 = "b5cdd24b02e6feff6df7630a7c7d96fcd02e3a9028794b754e76b7251b16506b"
# Released v1.14.54/v1.14.55 section plan and indexed final request representation.
PACKING_PREDECESSOR_SHA256 = "b1745d5b4876db5802d8bd2268e0d73c8dc38ac5b19531ce16a941610ca7678e"


class IncompleteValidation(ValueError):
    """An unfinished independent response does not authorize another editor proposal."""


def _section_assignments(context: dict[str, Any], limit: int = FACT_LIMIT) -> list[list[str]]:
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


def assignments(context: dict[str, Any], limit: int = FACT_LIMIT) -> list[list[str]]:
    """Pack adjacent complete section groups without changing source fact order."""
    packed: list[list[str]] = []
    for group in _section_assignments(context, limit):
        if packed and len(packed[-1]) + len(group) <= limit:
            packed[-1].extend(group)
        else:
            packed.append(group)
    return packed


def _final_controller_digest(section_sha256: str | None = None) -> str:
    return sha(
        json_bytes(
            {
                "validation": section_sha256 or sha(Path(__file__).read_bytes()),
                "ledger": sha(Path(__file__).with_name("curation_fact_ledger.py").read_bytes()),
            }
        )
    )


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
        index_ledger(payload, ledger)
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
    if ledger is not None:
        scope += (
            " Final ledger representation 1 uses source_block_index into the complete blocks "
            "array and candidate_block_indices into candidate_blocks, both supplied in this "
            "same request. Recover exact addresses/digests from those blocks and preserve every "
            "necessary/preserved/kind decision. The reconstruction digest guards the entire "
            "original ledger. Indices never establish semantic approval."
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
    body = _scope_request(
        original,
        [],
        {
            **selection,
            "final_document_review": True,
            "final_controller_sha256": _final_controller_digest(),
        },
        facts,
    )
    final_path = directory / "final.json"
    if final_path.is_file() and not final_path.is_symlink():
        saved = ResponseEvidence.model_validate_json(final_path.read_bytes())
        compatible_executions = {
            _execution_digest(section_sha256=PACKING_PREDECESSOR_SHA256),
            _execution_digest(legacy=True),
        }
        if not saved.error and selection["execution_sha256"] in compatible_executions:
            previous_body = _scope_request(
                original,
                [],
                {
                    **selection,
                    "final_document_review": True,
                    "final_controller_sha256": _final_controller_digest(PACKING_PREDECESSOR_SHA256),
                },
                facts,
            )
            # Equality is over the complete reconstructed request, never just its hash.
            if saved.request == previous_body:
                body = previous_body
    checked, final = _verify(final_path, body, {**context, "blocks": []}, candidate, client)
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


def _execution_digest(*, legacy: bool = False, section_sha256: str | None = None) -> str:
    root = Path(__file__).parent
    return sha(
        json_bytes(
            {
                name: (LEGACY_SECTION_SHA256 if legacy else section_sha256)
                if name == "curation_section_validation.py" and (legacy or section_sha256)
                else sha((root / name).read_bytes())
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
    )


def _compatible_selection(
    selection: dict[str, Any], directory: Path, context: dict[str, Any]
) -> tuple[dict[str, Any], list[list[str]]]:
    ordered = assignments(context)
    current_directory = directory / sha(json_bytes(selection))
    if current_directory.is_symlink() or (current_directory / "selection.json").is_symlink():
        raise ValueError("unsafe section review selection")
    if current_directory.exists():
        return selection, ordered
    previous_ordered = _section_assignments(context)
    for digest in (PACKING_PREDECESSOR_SHA256, LEGACY_SECTION_SHA256):
        previous_selection = {k: v for k, v in selection.items() if k != "assignment_strategy"}
        previous_selection.update(
            execution_sha256=_execution_digest(section_sha256=digest),
            assignments_sha256=sha(json_bytes(previous_ordered)),
        )
        previous_directory = directory / sha(json_bytes(previous_selection))
        guard = previous_directory / "selection.json"
        if previous_directory.is_symlink() or guard.is_symlink():
            raise ValueError("unsafe historical section selection")
        if guard.is_file():
            if guard.read_bytes() != json_bytes(previous_selection):
                raise ValueError("changed historical section selection")
            return previous_selection, previous_ordered
    return selection, ordered


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
        "assignment_strategy": "adjacent-sections/1",
        "original_request_sha256": request_hash(original),
        "execution_sha256": _execution_digest(),
        "candidate_sha256": candidate["output_sha256"],
        "assignments_sha256": sha(json_bytes(ordered)),
        "new_editor_proposals": 0,
    }
    selection, ordered = _compatible_selection(selection, directory, context)
    directory = directory / sha(json_bytes(selection))
    guard = directory / "selection.json"
    if guard.exists() and guard.read_bytes() != json_bytes(selection):
        raise ValueError("changed section review selection")
    atomic_private(guard, json_bytes(selection))
    # Conservative schema-valid sizing only; never an inferred judgment or response.
    _scope_request(
        original,
        [],
        {
            **selection,
            "final_document_review": True,
            "preflight_only": True,
            "final_controller_sha256": _final_controller_digest(),
        },
        sizing_ledger(context, candidate),
    )
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
