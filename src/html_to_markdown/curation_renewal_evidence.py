"""Lossless private request views of complete curation candidate stages."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .enrichment.analysis import sha


def restore_candidate_stages(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve only complete digest-guarded bodies supplied in the same request."""
    candidate: dict[str, Any] = deepcopy(payload["candidate"])
    if "candidate_stage_bodies" not in payload:
        return candidate
    pool = payload["candidate_stage_bodies"]
    if not isinstance(pool, dict) or any(
        not isinstance(text, str) or sha(text) != digest for digest, text in pool.items()
    ):
        raise ValueError("candidate stage body digest changed")
    used = set()
    for stage in candidate.get("stages", []):
        for field in ("input_body", "output_body"):
            reference = stage[field]
            if (
                not isinstance(reference, dict)
                or set(reference) != {"stage_body_sha256"}
                or reference["stage_body_sha256"] not in pool
            ):
                raise ValueError("candidate stage body reference is missing or malformed")
            digest = reference["stage_body_sha256"]
            stage[field] = pool[digest]
            used.add(digest)
    if used != set(pool):
        raise ValueError("candidate stage body reference coverage changed")
    return candidate


def index_candidate_stages(payload: dict[str, Any]) -> None:
    """Supply each distinct complete body once without changing the raw candidate."""
    original = payload["candidate"]
    candidate = deepcopy(original)
    pool: dict[str, str] = {}
    for stage in candidate.get("stages", []):
        for field in ("input_body", "output_body"):
            body = stage[field]
            if not isinstance(body, str):
                raise ValueError("complete candidate stage body is missing")
            digest = sha(body)
            if digest in pool and pool[digest] != body:
                raise ValueError("candidate stage body digest is ambiguous")
            pool[digest] = body
            stage[field] = {"stage_body_sha256": digest}
    payload["candidate"] = candidate
    payload["candidate_stage_bodies"] = pool
    if restore_candidate_stages(payload) != original:
        raise ValueError("candidate stage body reconstruction changed")


def restore_planner_candidate(payload: dict[str, Any]) -> dict[str, Any]:
    """Recover the complete reviewed proposal candidate from an actual-planner view."""
    view = {"candidate": payload["reviewed_proposal"]["candidate"]}
    if "planner_candidate_stage_bodies" in payload:
        view["candidate_stage_bodies"] = payload["planner_candidate_stage_bodies"]
    return restore_candidate_stages(view)


def index_planner_candidate(payload: dict[str, Any]) -> None:
    """Do not mutate the preserved reviewed proposal while indexing duplicate stages."""
    original = payload["reviewed_proposal"]["candidate"]
    view = {"candidate": original}
    index_candidate_stages(view)
    payload["reviewed_proposal"] = {**payload["reviewed_proposal"], "candidate": view["candidate"]}
    payload["planner_candidate_stage_bodies"] = view["candidate_stage_bodies"]
    if restore_planner_candidate(payload) != original:
        raise ValueError("actual-planner reviewed candidate reconstruction changed")


def restore_planner_fact_blocks(payload: dict[str, Any], field: str) -> list[dict[str, Any]]:
    """Resolve exact source text while keeping every distinct fact address and digest."""
    originals = payload["source"]["blocks"]
    restored = []
    for item in payload[field]:
        block = dict(item)
        if "source_block_index" in block:
            index = block.pop("source_block_index")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < len(originals)
            ):
                raise ValueError("actual-planner fact source reference is invalid")
            text = originals[index]["text"]
            if "text" in block or sha(text) != block["sha256"]:
                raise ValueError("actual-planner fact source digest changed")
            block["text"] = text
        restored.append(block)
    return restored


def index_planner_candidate_facts(payload: dict[str, Any]) -> None:
    """Reused candidate text remains supplied in its exact complete source block."""
    original = payload["candidate_blocks"]
    source = {
        (block["sha256"], block["text"]): index
        for index, block in enumerate(payload["source"]["blocks"])
    }
    payload["candidate_blocks"] = [
        {
            **{key: value for key, value in block.items() if key != "text"},
            "source_block_index": source[(block["sha256"], block["text"])],
        }
        if (block["sha256"], block["text"]) in source
        else block
        for block in original
    ]
    if restore_planner_fact_blocks(payload, "candidate_blocks") != original:
        raise ValueError("actual-planner candidate fact reconstruction changed")
