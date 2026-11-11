"""Resumable version-two proposals with bounded precise-feedback corrections."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Any

from ..content_inventory import atomic_private
from ..curation import corpus_digest, json_bytes
from .analysis import analyze, sha
from .codex_client import CodexClient
from .contracts import EDITOR_MODEL, VALIDATOR_MODEL, ResponseEvidence
from .contracts_v2 import ClassifiedSection, DocumentDecisionV2, EditedSection, ValidationV2
from .engine import artifact_bytes, load_artifact
from .gates import parse_response, request_hash
from .v2 import (
    assemble,
    classification_request,
    document_request,
    edit_request,
    enforce_enrichment,
    full_visuals,
    replay_document,
    sections,
    validation_request,
    verify_validation,
)


def generate_v2(curated: Path, destination: Path, *, phase: str, journal: Path) -> dict[str, Any]:
    artifact = load_artifact(destination)
    analysis = analyze(curated)
    if artifact.curated_sha256 != corpus_digest(curated) or artifact.analysis_sha256 != sha(
        json_bytes(analysis)
    ):
        raise ValueError("version-two input changed since preparation")
    canonical = {doc["document"]: doc for doc in analysis["documents"]}
    records = {record.document: record for record in artifact.documents}
    client = CodexClient(journal, concurrency=4)
    checkpoint_lock = Lock()

    def checkpoint() -> None:
        with checkpoint_lock:
            atomic_private(destination, artifact_bytes(artifact))

    def work(doc: dict[str, Any]) -> None:
        record = records[doc["document"]]
        if record.v2 is None:
            record.v2 = {"attempts": [], "selected_attempt": None, "failure_reason": None}
        progress = record.v2
        selected = progress.get("selected_attempt")
        if selected is not None:
            replay_document(doc, progress["attempts"][selected], canonical, artifact.repairs)
            return
        groups = sections(doc)
        try:
            visual = full_visuals(curated, doc)
        except (ValueError, OSError) as failure:
            progress["failure_reason"] = "complete_media_unavailable"
            progress["failure_details"] = str(failure)
            checkpoint()
            return
        canonical_candidates = [
            other
            for other in canonical.values()
            if other["document"] != doc["document"]
            and (other["metadata"].get("canonical_url") or other["metadata"]["url"])
            == (doc["metadata"].get("canonical_url") or doc["metadata"]["url"])
        ]
        while len(progress["attempts"]) < 3 or (
            progress["attempts"] and progress["attempts"][-1].get("failure") is None
        ):
            previous = progress["attempts"][-1] if progress["attempts"] else None
            if previous is not None and previous.get("failure") is None:
                attempt = previous
                feedback = attempt["feedback"]
            else:
                feedback = [previous["failure"]] if previous and previous.get("failure") else []
                attempt = {
                    "feedback": feedback,
                    "classification": [],
                    "editing": [],
                    "document": None,
                    "validation": None,
                    "visual": visual,
                    "canonical_candidates": canonical_candidates,
                    "failure": None,
                }
                progress["attempts"].append(attempt)
            checkpoint()
            evidence: ResponseEvidence | None = None

            def obtain(
                key: str,
                body: dict[str, Any],
                index: int | None = None,
                captured_attempt: dict[str, Any] = attempt,
            ) -> ResponseEvidence:
                attempt = captured_attempt
                saved = (
                    attempt[key]
                    if index is None
                    else (attempt[key][index] if index < len(attempt[key]) else None)
                )
                if saved is not None and saved.get("error") is None:
                    if saved["request_sha256"] != request_hash(body):
                        raise ValueError("stale captured phase request")
                    return ResponseEvidence.model_validate(saved)
                response = client.execute(body)
                if saved is not None:
                    attempt.setdefault("previous_phase_receipts", []).append(saved)
                if index is None:
                    attempt[key] = response.model_dump()
                elif index < len(attempt[key]):
                    attempt[key][index] = response.model_dump()
                else:
                    attempt[key].append(response.model_dump())
                checkpoint()
                return response

            try:
                classified = []
                for index, group in enumerate(groups):
                    body = classification_request(doc, group, feedback)
                    evidence = obtain("classification", body, index)
                    classified.append(
                        parse_response(
                            evidence, ClassifiedSection, EDITOR_MODEL, request_hash(body)
                        )
                    )
                all_classes = [
                    value.model_dump()
                    for section in classified
                    for value in section.classifications
                ]
                edited = []
                for index, group in enumerate(groups):
                    body = edit_request(doc, group, all_classes, feedback)
                    evidence = obtain("editing", body, index)
                    edited.append(
                        parse_response(evidence, EditedSection, EDITOR_MODEL, request_hash(body))
                    )
                body = document_request(
                    doc,
                    {
                        "classifications": all_classes,
                        "section_proposals": [section.model_dump() for section in edited],
                        "validation_feedback": feedback,
                        "canonical_candidates": canonical_candidates,
                    },
                    visual,
                )
                evidence = obtain("document", body)
                header = parse_response(
                    evidence, DocumentDecisionV2, EDITOR_MODEL, request_hash(body)
                )
                decision, candidate_body = assemble(
                    doc, groups, classified, edited, header, canonical, artifact.repairs
                )
                if phase == "editor":
                    # The validator phase resumes these complete proposals.
                    attempt["prepared"] = True
                    checkpoint()
                    return
                body = validation_request(doc, candidate_body, decision, {}, visual)
                evidence = obtain("validation", body)
                validation = parse_response(
                    evidence, ValidationV2, VALIDATOR_MODEL, request_hash(body)
                )
                enforce_enrichment(doc, candidate_body, header, validation)
                verify_validation(doc, candidate_body, validation)
                progress["selected_attempt"] = len(progress["attempts"]) - 1
                progress["failure_reason"] = None
                checkpoint()
                return
            except (ValueError, KeyError, TypeError, OSError) as failure:
                if evidence is not None and evidence.error is not None:
                    attempt["transport_failure"] = evidence.error
                    progress["failure_reason"] = "incomplete_transport_or_schema_response"
                    checkpoint()
                    return
                attempt["failure"] = str(failure)
                if attempt.get("validation") is not None:
                    try:
                        rejected = parse_response(
                            ResponseEvidence.model_validate(attempt["validation"]),
                            ValidationV2,
                            VALIDATOR_MODEL,
                            attempt["validation"]["request_sha256"],
                        )
                        attempt["failure"] += "; " + "; ".join(
                            rejected.failures + rejected.unsupported_claims + rejected.unresolved
                        )
                    except (ValueError, KeyError, TypeError):
                        pass
                progress["failure_reason"] = "proposal_or_preservation_validation_failed"
                if evidence is not None:
                    client.invalidate(evidence)
                checkpoint()
        # Three proposals exhausted: preserve the complete reviewed source article.
        checkpoint()

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(work, doc): doc["document"] for doc in analysis["documents"]}
            for future in as_completed(futures):
                future.result()
    finally:
        client.close()
    return {
        "phase": phase,
        "documents": len(records),
        "validated": sum(
            record.v2 is not None and record.v2.get("selected_attempt") is not None
            for record in records.values()
        ),
        "fallbacks": sum(
            record.v2 is not None and record.v2.get("failure_reason") is not None
            for record in records.values()
        ),
    }
