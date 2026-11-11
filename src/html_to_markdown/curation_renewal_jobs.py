"""Resumable private curation attempts; complete validated requests replay offline."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .content_inventory import atomic_private
from .curation import json_bytes
from .curation_renewal import Proposal, compile_proposal, proposal_context
from .curation_renewal_review import RenewalValidation, review_request, verify_validation
from .enrichment.analysis import sha
from .enrichment.codex_client import CodexClient
from .enrichment.contracts import EDITOR_MODEL, VALIDATOR_MODEL, ResponseEvidence
from .enrichment.gates import parse_response, request_hash
from .media_observation_jobs import RequestExecutor


class TransportFailure(ValueError):
    """Failed transports remain resumable without spending semantic corrections."""


def review_batch(
    output: Path, inventory: dict[str, Any], phase: str, journal: Path
) -> dict[str, Any]:
    """Progress independent documents while failed prerequisites stay visible."""
    if phase not in {"all", "replay"}:
        raise ValueError("renewal batch phase must be all or replay")
    ready = any("context_path" in item for item in inventory["documents"])
    client = CodexClient(journal, concurrency=4) if phase == "all" and ready else None

    def review_item(item: dict[str, Any]) -> dict[str, Any]:
        result = {"identity": item["identity"], "disposition": "queued"}
        if "context_path" not in item:
            return {**result, "reason": "source_review_prerequisite_failed"}
        try:
            pin = item["binding_sha256"]
            if not re.fullmatch(r"[0-9a-f]{64}", pin):
                raise ValueError("unsafe renewal input identity")
            for key, filename in [("context_path", "context.json"), ("visual_path", "visual.json")]:
                if (
                    item[key] != pin + "/" + filename
                    or (output / item[key]).is_symlink()
                    or (output / pin).is_symlink()
                ):
                    raise ValueError("unsafe renewal input path")
            context = json.loads((output / item["context_path"]).read_bytes())
            visual = json.loads((output / item["visual_path"]).read_bytes())
            if client is None:
                replay_renewal(context, visual, output)
            else:
                run_renewal(context, visual, output, client)
            return {
                **result,
                "disposition": "independently_validated",
                "planner_validation_required": True,
            }
        except (ValueError, KeyError, OSError) as failure:
            return {
                **result,
                "reason": "renewal_review_failed",
                "error_class": type(failure).__name__,
            }

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            documents = list(pool.map(review_item, inventory["documents"]))
    finally:
        if client is not None:
            client.close()
    result = {
        "documents": documents,
        "independently_validated": sum(
            d["disposition"] == "independently_validated" for d in documents
        ),
        "exceptions": sum(d["disposition"] == "queued" for d in documents),
        "planner_validation_required": True,
        "phase": phase,
    }
    atomic_private(output / "renewal-report.json", json_bytes(result))
    return result


def _save(path: Path, value: dict[str, Any]) -> None:
    data = json_bytes(value)
    if path.exists() and path.read_bytes() != data:
        previous = path.read_bytes()
        atomic_private(path.parent / "history" / (sha(previous) + ".json"), previous)
    atomic_private(path, data)


def _execute(path: Path, body: dict[str, Any], client: RequestExecutor) -> ResponseEvidence:
    if path.exists():
        saved = ResponseEvidence.model_validate_json(path.read_bytes())
        if not saved.error and saved.request_sha256 == request_hash(body):
            return saved
    evidence = client.execute(body)
    _save(path, evidence.model_dump())
    if evidence.error:
        raise TransportFailure("curation renewal transport failed; private evidence preserved")
    return evidence


def _directory(context: dict[str, Any], output: Path, *, create: bool = True) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", str(context.get("binding_sha256", ""))):
        raise ValueError("unsafe curation renewal binding identity")
    expected = proposal_context(
        context["metadata"],
        context["body"],
        context["media"],
        context["source_files_sha256"],
        context["policy_sha256"],
        context.get("evidence"),
    )
    if expected != context:
        raise ValueError("stale curation renewal context")
    directory = output / str(context["binding_sha256"])
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    elif not directory.is_dir():
        raise ValueError("curation renewal has no preserved review directory")
    path = directory / "context.json"
    if path.exists() and path.read_bytes() != json_bytes(context):
        raise ValueError("changed curation renewal context at an existing binding")
    if create:
        atomic_private(path, json_bytes(context))
    return directory


def replay_renewal(
    context: dict[str, Any], visual: list[dict[str, Any]], output: Path
) -> dict[str, Any]:
    directory = _directory(context, output, create=False)
    state = json.loads((directory / "state.json").read_bytes())
    if state["status"] != "independently_validated":
        raise ValueError("curation renewal has no complete independent validation")
    attempt = directory / f"attempt-{state['attempt']:03d}"
    editor_body = review_request(context, visual, feedback=state["feedback"])
    proposal = parse_response(
        ResponseEvidence.model_validate_json((attempt / "editor.json").read_bytes()),
        Proposal,
        EDITOR_MODEL,
        request_hash(editor_body),
    )
    candidate = compile_proposal(context, proposal.model_dump())
    validation_body = review_request(context, visual, candidate, state["feedback"])
    validation = parse_response(
        ResponseEvidence.model_validate_json((attempt / "validator.json").read_bytes()),
        RenewalValidation,
        VALIDATOR_MODEL,
        request_hash(validation_body),
    )
    verify_validation(context, candidate, validation.model_dump())
    result = {
        "identity": context["identity"],
        "binding_sha256": context["binding_sha256"],
        "candidate": candidate,
        "preservation": validation.model_dump(),
        "independently_validated": True,
        "planner_validation_required": True,
        "editor_request_sha256": request_hash(editor_body),
        "validator_request_sha256": request_hash(validation_body),
    }
    if (directory / "validated.json").read_bytes() != json_bytes(result):
        raise ValueError("curation renewal replay result changed")
    return result


# A bounded attempt preserves complete editor and independent validator evidence.
# pylint: disable-next=too-many-locals
def run_renewal(
    context: dict[str, Any],
    visual: list[dict[str, Any]],
    output: Path,
    client: RequestExecutor,
) -> dict[str, Any]:
    directory = _directory(context, output)
    state_path = directory / "state.json"
    state: dict[str, Any] = (
        json.loads(state_path.read_bytes())
        if state_path.exists()
        else {"attempt": 0, "feedback": [], "status": "queued"}
    )
    if state["status"] == "independently_validated":
        return replay_renewal(context, visual, output)
    while state["attempt"] < 3:
        attempt = directory / f"attempt-{state['attempt']:03d}"
        attempt.mkdir(mode=0o700, exist_ok=True)
        evidence = []
        validation = None
        try:
            editor_body = review_request(context, visual, feedback=state["feedback"])
            editor = _execute(attempt / "editor.json", editor_body, client)
            evidence.append(editor)
            proposal = parse_response(editor, Proposal, EDITOR_MODEL, request_hash(editor_body))
            candidate = compile_proposal(context, proposal.model_dump())
            _save(attempt / "candidate.json", candidate)
            validator_body = review_request(context, visual, candidate, state["feedback"])
            validator = _execute(attempt / "validator.json", validator_body, client)
            evidence.append(validator)
            validation = parse_response(
                validator, RenewalValidation, VALIDATOR_MODEL, request_hash(validator_body)
            )
            verify_validation(context, candidate, validation.model_dump())
        except TransportFailure:
            state.update(status="queued", retryable=True)
            _save(state_path, state)
            raise
        except ValueError as failure:
            _save(attempt / "failure.json", {"error": str(failure)})
            invalidator = getattr(client, "invalidate", None)
            if invalidator is not None:
                for response in evidence:
                    invalidator(response)
            details = validation.failures if validation is not None else []
            state["feedback"] = [*state["feedback"], str(failure), *details]
            state.update(attempt=state["attempt"] + 1, status="queued", retryable=False)
            _save(state_path, state)
            continue
        result = {
            "identity": context["identity"],
            "binding_sha256": context["binding_sha256"],
            "candidate": candidate,
            "preservation": validation.model_dump(),
            "independently_validated": True,
            "planner_validation_required": True,
            "editor_request_sha256": request_hash(editor_body),
            "validator_request_sha256": request_hash(validator_body),
        }
        _save(directory / "validated.json", result)
        state.update(status="independently_validated", retryable=False)
        _save(state_path, state)
        return replay_renewal(context, visual, output)
    raise ValueError("curation renewal exhausted two corrected proposals; exception remains queued")
