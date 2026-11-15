"""Lossless final-review references into the complete source and candidate blocks."""

from __future__ import annotations

from typing import Any

from .curation import json_bytes
from .curation_renewal import projected_fact
from .curation_renewal_review import candidate_fact_blocks, fact_blocks
from .enrichment.analysis import sha
from .enrichment.contracts_v2 import FactV2

VERSION = "curation-fact-ledger/1"


def _position(value: Any, length: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < length:
        raise ValueError("invalid final fact ledger block index")
    return value


def restore_ledger(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """A reference is never fact approval; recover every exact guarded source field."""
    if payload.get("independent_fact_review_ledger_representation") != VERSION:
        raise ValueError("unknown final fact ledger representation")
    source = fact_blocks(payload["blocks"])
    candidate = payload["candidate_blocks"]
    ledger = []
    for entry in payload["independent_fact_review_ledger"]:
        indices = entry["candidate_block_indices"]
        if not isinstance(indices, list):
            raise ValueError("invalid final fact ledger candidate indices")
        positions = [_position(i, len(candidate)) for i in indices]
        if len(positions) != len(set(positions)):
            raise ValueError("final ledger candidate witnesses are duplicated")
        block = source[_position(entry["source_block_index"], len(source))]
        retained = [candidate[i] for i in positions]
        if set(entry) != {
            "source_block_index",
            "candidate_block_indices",
            "necessary",
            "preserved",
            "kind",
        }:
            raise ValueError("unexpected final fact ledger fields")
        fact = FactV2.model_validate(
            {
                "source_address": block["address"],
                "source_sha256": block["sha256"],
                "candidate_addresses": [b["address"] for b in retained],
                "candidate_sha256": [b["sha256"] for b in retained],
                "necessary": entry["necessary"],
                "preserved": entry["preserved"],
                "kind": entry["kind"],
            }
        )
        ledger.append(fact.model_dump())
    if sha(json_bytes(ledger)) != payload["independent_fact_review_ledger_sha256"]:
        raise ValueError("final fact ledger reconstruction digest changed")
    return ledger


def index_ledger(payload: dict[str, Any], facts: list[dict[str, Any]]) -> None:
    """Omit duplicated strings only when they reconstruct from complete block arrays."""
    source = {b["address"]: (i, b) for i, b in enumerate(fact_blocks(payload["blocks"]))}
    candidate = {b["address"]: (i, b) for i, b in enumerate(payload["candidate_blocks"])}
    indexed = []
    for value in facts:
        fact = FactV2.model_validate(value)
        position, block = source[fact.source_address]
        if block["sha256"] != fact.source_sha256:
            raise ValueError("final ledger source digest changed")
        if len(fact.candidate_addresses) != len(fact.candidate_sha256) or len(
            fact.candidate_addresses
        ) != len(set(fact.candidate_addresses)):
            raise ValueError("final ledger candidate witnesses are incomplete or duplicated")
        references = []
        for address, digest in zip(fact.candidate_addresses, fact.candidate_sha256, strict=True):
            index, retained = candidate[address]
            if retained["sha256"] != digest:
                raise ValueError("final ledger candidate digest changed")
            references.append(index)
        indexed.append(
            {
                "source_block_index": position,
                "candidate_block_indices": references,
                "necessary": fact.necessary,
                "preserved": fact.preserved,
                "kind": fact.kind,
            }
        )
    payload["independent_fact_review_ledger"] = indexed
    payload["independent_fact_review_ledger_sha256"] = sha(json_bytes(facts))
    payload["independent_fact_review_ledger_representation"] = VERSION
    if restore_ledger(payload) != facts:
        raise ValueError("final fact ledger indexing changed a decision")


def sizing_ledger(context: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """Conservative valid-schema sizing data, never a judgment or a model response."""
    retained = candidate_fact_blocks(candidate["body"])
    result = []
    for block in fact_blocks(context["blocks"]):
        if block["kind"] in {"section", "heading", "hr"}:
            continue
        text = projected_fact(context, candidate, block)
        matches = [b for b in retained if b["sha256"] == sha(text) and b["text"] == text]
        result.append(
            {
                "source_address": block["address"],
                "source_sha256": block["sha256"],
                "candidate_addresses": [b["address"] for b in matches],
                "candidate_sha256": [b["sha256"] for b in matches],
                "necessary": False,
                "preserved": False,
                "kind": "prerequisite",
            }
        )
    return result
