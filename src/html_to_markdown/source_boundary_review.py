"""Resumable complete source review with actual, independently checked native responses."""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict

from .curation import json_bytes
from .enrichment.analysis import sha
from .enrichment.codex_client import is_transient_error
from .enrichment.contracts import EDITOR_MODEL, VALIDATOR_MODEL, ResponseEvidence, StrictModel
from .enrichment.gates import parse_response, request_hash
from .errors import TransportFailure
from .private_storage import atomic_write
from .source_boundary_requests import (
    MARKER,
    VERSION,
    BoundaryAnalysis,
    BoundaryValidation,
    boundary_plan,
    final_request,
    partition_request,
    phase_checks,
)
from .source_input_projection import MARKER as INPUT_MARKER
from .source_input_projection import VERSION as INPUT_VERSION
from .source_input_projection import project_source_request


class InputProjectionProof(StrictModel):
    version: str
    logical_request: dict[str, Any]
    boundary: dict[str, Any] | None = None


class BoundaryPartEvidence(StrictModel):
    analysis: ResponseEvidence
    validation: ResponseEvidence


class BoundaryProof(StrictModel):
    version: str
    logical_request: dict[str, Any]
    coverage_plan: dict[str, Any]
    parts: list[BoundaryPartEvidence]


class _RawDecision(BaseModel):
    model_config = ConfigDict(extra="allow")


class BoundaryTransportFailure(TransportFailure):
    """Retain the actual failed part without pretending the logical request was invoked."""

    def __init__(self, evidence: ResponseEvidence):
        super().__init__("native source boundary transport failed; exact part evidence preserved")
        self.evidence = evidence
        self.retryable = is_transient_error(evidence.error)


class BoundaryPending(StrictModel):
    version: str
    logical_request_sha256: str
    failed_part_request_sha256: str
    failed_part_response_sha256: str
    error: str
    retryable: bool


def has_retryable_boundary(saved: dict[str, Any]) -> bool:
    for raw in saved.get("boundary_pending", {}).values():
        pending = BoundaryPending.model_validate(raw)
        if pending.version != VERSION:
            raise ValueError("unsupported pending native source boundary contract")
        if pending.retryable:
            return True
    return False


def source_logical_request(evidence: ResponseEvidence) -> dict[str, Any]:
    if evidence.source_boundary is not None:
        if evidence.source_boundary.get("version") == INPUT_VERSION:
            return InputProjectionProof.model_validate(evidence.source_boundary).logical_request
        proof = BoundaryProof.model_validate(evidence.source_boundary)
        if proof.version != VERSION:
            raise ValueError("unsupported source native boundary proof version")
        return proof.logical_request
    return evidence.request


def _check_analysis(value: BoundaryAnalysis, plan: dict[str, Any], part: dict[str, Any]) -> None:
    expected = part["positions"]
    guards = (
        value.logical_request_sha256 == plan["logical_request_sha256"],
        value.partition_sha256 == part["partition_sha256"],
        value.complete_text_reviewed,
        not value.unresolved,
        len(value.visuals) == len(expected),
    )
    if not all(guards):
        raise ValueError("native source boundary analysis lacks complete bound coverage")
    for visual, position in zip(value.visuals, expected, strict=True):
        complete = all(
            (
                visual.position == position["position"],
                visual.content_sha256 == position["content_sha256"],
                visual.description.strip(),
                visual.complete_visual_reviewed,
                not visual.uncertain,
                all(
                    text.strip()
                    for text in [
                        *visual.visible_text,
                        *visual.technical_facts,
                        *visual.privacy_context,
                    ]
                ),
            )
        )
        if not complete:
            raise ValueError("native source boundary visual facts are incomplete or stale")


def _check_validation(
    value: BoundaryValidation,
    analysis: BoundaryAnalysis,
    plan: dict[str, Any],
    part: dict[str, Any],
    body: dict[str, Any],
) -> None:
    guards = (
        value.logical_request_sha256 == plan["logical_request_sha256"],
        value.partition_sha256 == part["partition_sha256"],
        value.analysis_sha256 == sha(json_bytes(analysis.model_dump())),
        value.covered_positions == [position["position"] for position in part["positions"]],
        all(
            (
                value.approved,
                value.complete_text_reviewed,
                value.complete_assigned_visuals_reviewed,
                value.all_necessary_facts_captured,
                value.relationships_and_privacy_context_checked,
            )
        ),
        not value.failures,
        not value.unresolved,
        [check.name for check in value.assigned_phase_checks] == phase_checks(body),
        all(check.passed and check.reason.strip() for check in value.assigned_phase_checks),
    )
    if not all(guards):
        prefix = (
            "privacy proposal " if body["text"]["format"]["name"] == "admissionvalidation" else ""
        )
        details = [
            *value.failures,
            *value.unresolved,
            *[check.reason for check in value.assigned_phase_checks if not check.passed],
        ]
        raise ValueError(
            prefix
            + "independent native source boundary preservation validation failed: "
            + "; ".join(details)
        )


def _summary(
    part: BoundaryPartEvidence, analysis: BoundaryAnalysis, validation: BoundaryValidation
) -> dict[str, Any]:
    return {
        "analysis": analysis.model_dump(),
        "validation": validation.model_dump(),
        "analysis_request_sha256": part.analysis.request_sha256,
        "analysis_response_sha256": part.analysis.response_sha256,
        "validation_request_sha256": part.validation.request_sha256,
        "validation_response_sha256": part.validation.response_sha256,
    }


def validate_boundary_evidence(evidence: ResponseEvidence) -> dict[str, Any]:
    """Reconstruct every leaf request and final actual request; no response is rebound."""
    if evidence.source_boundary and evidence.source_boundary.get("version") == INPUT_VERSION:
        projected = InputProjectionProof.model_validate(evidence.source_boundary)
        actual = project_source_request(projected.logical_request)
        if actual is None:
            raise ValueError("source projection proof does not contain a projectable request")
        if projected.boundary is None:
            if evidence.request != actual or evidence.request_sha256 != request_hash(actual):
                raise ValueError("actual projected source request changed or is rebound")
        else:
            nested = evidence.model_copy(update={"source_boundary": projected.boundary})
            if validate_boundary_evidence(nested) != actual:
                raise ValueError("projected source boundary does not cover the complete request")
        return projected.logical_request
    proof = BoundaryProof.model_validate(evidence.source_boundary)
    if proof.version != VERSION:
        raise ValueError("unsupported source native boundary proof version")
    body = proof.logical_request
    plan = boundary_plan(body)
    if plan is None or plan != proof.coverage_plan or len(proof.parts) != len(plan["partitions"]):
        raise ValueError("source native boundary plan or complete coverage changed")
    summaries = []
    for part, scope in zip(proof.parts, plan["partitions"], strict=True):
        if part.analysis.source_boundary is not None or part.validation.source_boundary is not None:
            raise ValueError("source boundary leaf evidence cannot contain another aggregate")
        proposal_body = partition_request(body, plan, scope)
        analysis = parse_response(
            part.analysis, BoundaryAnalysis, EDITOR_MODEL, request_hash(proposal_body)
        )
        _check_analysis(analysis, plan, scope)
        validation_body = partition_request(body, plan, scope, analysis)
        validation = parse_response(
            part.validation, BoundaryValidation, VALIDATOR_MODEL, request_hash(validation_body)
        )
        _check_validation(validation, analysis, plan, scope, body)
        summaries.append(_summary(part, analysis, validation))
    final = final_request(body, plan, summaries)
    if evidence.request != final or evidence.request_sha256 != request_hash(final):
        raise ValueError("actual final source boundary request changed or is rebound")
    return body


def parse_source_response(
    evidence: ResponseEvidence,
    schema: type[BaseModel],
    model: str,
    expected_request_hash: str,
) -> Any:
    if evidence.source_boundary is None:
        content = evidence.request.get("input", [{}])[0].get("content", [{}])[0]
        payload = json.loads(content["text"]) if "text" in content else None
        if isinstance(payload, dict) and (MARKER in payload or INPUT_MARKER in payload):
            raise ValueError("final source response is missing its complete boundary evidence")
        return parse_response(evidence, schema, model, expected_request_hash)
    logical = validate_boundary_evidence(evidence)
    if request_hash(logical) != expected_request_hash:
        raise ValueError("stale complete logical source boundary request")
    plain = evidence.model_copy(update={"source_boundary": None})
    return parse_response(plain, schema, model, evidence.request_sha256)


def _retire(path: Path, client: Any, evidence: ResponseEvidence) -> None:
    raw = path.read_bytes()
    atomic_write(path.parent / "history" / (sha(raw) + ".json"), raw)
    invalidate = getattr(client, "invalidate", None)
    if invalidate is not None and evidence.error is None:
        invalidate(evidence)


def _part_response(
    path: Path,
    body: dict[str, Any],
    client: Any,
    schema: type[BaseModel],
    check: Any,
) -> tuple[ResponseEvidence, Any]:
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("unsafe source boundary checkpoint path")
    if path.exists():
        previous = ResponseEvidence.model_validate_json(path.read_bytes())
        if previous.request != body or previous.source_boundary is not None:
            raise ValueError("source boundary checkpoint addresses different complete input")
        if previous.error and not is_transient_error(previous.error):
            raise BoundaryTransportFailure(previous)
        try:
            value = parse_response(previous, schema, body["model"], request_hash(body))
            check(value)
            return previous, value
        except ValueError:
            if client is None:
                raise
            _retire(path, client, previous)
    if client is None:
        raise ValueError("complete native source part evidence is unavailable")
    _check_input_size(body, path.parent)
    evidence = ResponseEvidence.model_validate(client.execute(body))
    atomic_write(path, json_bytes(evidence.model_dump(mode="json")))
    if evidence.error:
        raise BoundaryTransportFailure(evidence)
    try:
        value = parse_response(evidence, schema, body["model"], request_hash(body))
        check(value)
    except ValueError:
        _retire(path, client, evidence)
        raise
    return evidence, value


def _review_partition(
    body: dict[str, Any],
    plan: dict[str, Any],
    part: dict[str, Any],
    client: Any,
    checkpoint: Path,
) -> tuple[BoundaryPartEvidence, dict[str, Any]]:
    prefix = checkpoint / str(part["index"])
    proposal_body = partition_request(body, plan, part)
    editor, analysis = _part_response(
        prefix / "analysis.json",
        proposal_body,
        client,
        BoundaryAnalysis,
        partial(_check_analysis, plan=plan, part=part),
    )
    validator_body = partition_request(body, plan, part, analysis)
    validator, validation = _part_response(
        prefix / "validation.json",
        validator_body,
        client,
        BoundaryValidation,
        partial(_check_validation, analysis=analysis, plan=plan, part=part, body=body),
    )
    evidence = BoundaryPartEvidence(analysis=editor, validation=validator)
    return evidence, _summary(evidence, analysis, validation)


def _check_input_size(body: dict[str, Any], checkpoint: Path) -> None:
    texts = [
        content["text"]
        for message in body.get("input", [])
        for content in message.get("content", [])
        if content.get("type") == "input_text"
    ]
    characters = len("\n\n".join(texts))
    if characters <= 1048576:
        return
    if checkpoint.is_symlink() or any(parent.is_symlink() for parent in checkpoint.parents):
        raise ValueError("unsafe source input limit checkpoint")
    checkpoint.mkdir(mode=0o700, parents=True, exist_ok=True)
    raw = json_bytes(body)
    original = checkpoint / "input-limit-request.json"
    if original.exists() and original.read_bytes() != raw:
        raise ValueError("preserved oversized source request changed")
    if not original.exists():
        atomic_write(original, raw)
    atomic_write(
        checkpoint / "input-limit.json",
        json_bytes(
            {
                "request_sha256": request_hash(body),
                "characters": characters,
                "maximum_characters": 1048576,
                "inference_invoked": False,
            }
        ),
    )
    raise ValueError("complete source input exceeds native character limit; full input preserved")


def execute_source_request(body: dict[str, Any], client: Any, checkpoint: Path) -> ResponseEvidence:
    projected = project_source_request(body)
    if projected is None:
        return _execute_native_source_request(body, client, checkpoint)
    directory = checkpoint / INPUT_VERSION.replace("/", "-")
    if directory.is_symlink() or any(parent.is_symlink() for parent in directory.parents):
        raise ValueError("unsafe projected source checkpoint path")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, data in (("logical-request.json", body), ("projected-request.json", projected)):
        path = directory / name
        raw = json_bytes(data)
        if path.exists() and path.read_bytes() != raw:
            raise ValueError("preserved projected source request changed")
        if not path.exists():
            atomic_write(path, raw)
    actual = _execute_native_source_request(projected, client, directory / "native")
    proof = InputProjectionProof(
        version=INPUT_VERSION, logical_request=body, boundary=actual.source_boundary
    )
    result = actual.model_copy(update={"source_boundary": proof.model_dump(mode="json")})
    atomic_write(directory / "final-response.json", json_bytes(result.model_dump(mode="json")))
    return result


def _execute_native_source_request(
    body: dict[str, Any], client: Any, checkpoint: Path
) -> ResponseEvidence:
    _check_input_size(body, checkpoint)
    plan = boundary_plan(body)
    if plan is None:
        return ResponseEvidence.model_validate(client.execute(body))
    if checkpoint.is_symlink() or any(parent.is_symlink() for parent in checkpoint.parents):
        raise ValueError("unsafe source native boundary journal")
    checkpoint.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, data in (("logical-request.json", body), ("coverage-plan.json", plan)):
        path = checkpoint / name
        raw = json_bytes(data)
        if path.exists() and path.read_bytes() != raw:
            raise ValueError("preserved complete source boundary request changed")
        if not path.exists():
            atomic_write(path, raw)
    parts = []
    summaries = []
    for part in plan["partitions"]:
        evidence, summary = _review_partition(body, plan, part, client, checkpoint)
        parts.append(evidence)
        summaries.append(summary)
    final_body = final_request(body, plan, summaries)
    _check_input_size(final_body, checkpoint)
    final = ResponseEvidence.model_validate(client.execute(final_body))
    proof = BoundaryProof(version=VERSION, logical_request=body, coverage_plan=plan, parts=parts)
    result = final.model_copy(update={"source_boundary": proof.model_dump(mode="json")})
    atomic_write(checkpoint / "final-response.json", json_bytes(result.model_dump(mode="json")))
    return result


def execute_cached_source(path: Path, body: dict[str, Any], client: Any) -> ResponseEvidence:
    """Source/baseline callers keep their existing evidence path and full offline contract."""
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("unsafe source review evidence path")
    if path.exists():
        saved = ResponseEvidence.model_validate_json(path.read_bytes())
        if source_logical_request(saved) != body:
            raise ValueError("stale complete source review request")
        if not saved.error:
            value = parse_source_response(saved, _RawDecision, body["model"], request_hash(body))
            Draft202012Validator(body["text"]["format"]["schema"]).validate(value.model_dump())
            return saved
    if client is None:
        raise ValueError("complete source review evidence is unavailable")
    evidence = execute_source_request(
        body, client, path.parent / "native-boundaries" / request_hash(body)
    )
    if path.exists():
        _retire(path, client, ResponseEvidence.model_validate_json(path.read_bytes()))
    atomic_write(path, json_bytes(evidence.model_dump(mode="json")))
    if evidence.error:
        raise TransportFailure(
            "source review transport failed; complete private evidence preserved"
        )
    return evidence


def execute_source_phase(
    body: dict[str, Any],
    client: Any,
    checkpoint: Path,
    record: Path,
    phase: str,
    state: dict[str, Any] | None = None,
) -> ResponseEvidence:
    saved = json.loads(record.read_bytes())
    if phase in saved.get("boundary_pending", {}):
        atomic_write(
            record.parent.parent / "history" / (sha(record.read_bytes()) + ".json"),
            record.read_bytes(),
        )
        saved["boundary_pending"].pop(phase)
        if state is not None:
            state["boundary_pending"] = saved["boundary_pending"]
        atomic_write(record, json_bytes(saved))
    try:
        return execute_source_request(body, client, checkpoint)
    except BoundaryTransportFailure as failure:
        saved = json.loads(record.read_bytes())
        pending = BoundaryPending(
            version=VERSION,
            logical_request_sha256=request_hash(body),
            failed_part_request_sha256=failure.evidence.request_sha256,
            failed_part_response_sha256=failure.evidence.response_sha256,
            error=failure.evidence.error or "incomplete native response",
            retryable=failure.retryable,
        )
        saved.setdefault("boundary_pending", {})[phase] = pending.model_dump()
        atomic_write(record, json_bytes(saved))
        raise
