"""Current independent validation of an exact completed historical proposal."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .baseline_native_context import native_context
from .baseline_review_renewal import preparation_controller, verify_pending
from .baseline_validation_requests import validation_request as _validation_request
from .content_inventory import atomic_private
from .curation import blocks, json_bytes, load_curation_policy
from .enrichment.analysis import sha
from .enrichment.contracts import EDITOR_MODEL, VALIDATOR_MODEL, ResponseEvidence
from .enrichment.gates import request_hash
from .history_preservation import preserve_history
from .media_observation_jobs import confirmed_media_values
from .render import split_document
from .source_boundary_review import (
    execute_cached_source as _execute,
)
from .source_boundary_review import (
    parse_source_response as parse_response,
)
from .source_boundary_review import (
    source_logical_request,
)
from .source_phase_bindings import privacy_context
from .source_review import (
    AdmissionValidation,
    PrivacyDecision,
    RelevanceDecision,
    enforce,
    review_context_digest,
    scan,
    text_fields,
)
from .source_review_jobs import _compile, _request

VERSION = "baseline-existing-proposal-validation/1"


def _execution_digest() -> str:
    return sha(
        json_bytes(
            {
                "module": sha(Path(__file__).read_bytes()),
                "renewal": sha(Path(__file__).with_name("baseline_review_renewal.py").read_bytes()),
                "baseline": sha(Path(__file__).with_name("baseline_source_review.py").read_bytes()),
                "model_evidence": sha(
                    Path(__file__).with_name("curation_model_evidence.py").read_bytes()
                ),
                "privacy": review_context_digest(),
                "validation_request": sha(
                    Path(__file__).with_name("baseline_validation_requests.py").read_bytes()
                ),
                "original_native_context": sha(
                    Path(__file__).with_name("baseline_native_context.py").read_bytes()
                ),
                "media_reference_compiler": sha(
                    Path(__file__).with_name("source_media_references.py").read_bytes()
                ),
                "curation_policy": load_curation_policy().sha256,
            }
        )
    )


def _context(
    output: Path, saved: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]], PrivacyDecision]:
    """Historical controller claims are data; actual source bytes are checked now."""
    if (
        saved["binding"].get("version") != "baseline-source-privacy/3"
        or saved["binding"].get("input_kind") != "retained_verified_baseline"
    ):
        raise ValueError("unsupported existing baseline source contract")
    relative = Path(saved["source"])
    if relative.is_absolute() or ".." in relative.parts or (output / relative).is_symlink():
        raise ValueError("unsafe existing baseline source")
    source = output / relative
    files = {
        path.relative_to(source).as_posix(): sha(path.read_bytes())
        for path in sorted(source.rglob("*"))
        if path.is_file()
    }
    if (
        any(path.is_symlink() for path in source.rglob("*"))
        or files != saved["input_file_hashes"]
        or sha(json_bytes(files)) != saved["binding"]["source_files_sha256"]
        or (sha(json_bytes(saved["binding"])) != saved["binding_sha256"])
    ):
        raise ValueError("existing baseline source bytes or binding changed")
    relevance_evidence = ResponseEvidence.model_validate(saved["phases"]["relevance"])
    relevance = parse_response(
        relevance_evidence,
        RelevanceDecision,
        EDITOR_MODEL,
        request_hash(source_logical_request(relevance_evidence)),
    )
    context = json.loads(
        source_logical_request(relevance_evidence)["input"][0]["content"][0]["text"]
    )
    metadata, body = split_document((source / saved["item"]["article_path"]).read_text())
    visual, media_fields, media = native_context(source, output, saved)
    fields = {**text_fields(metadata, body), **media_fields}
    if any(
        (
            context["binding"] != saved["binding"],
            context["binding_sha256"] != saved["binding_sha256"],
            context["metadata"] != metadata,
            context["body"] != body,
            context["media"] != media,
            context["fields"] != fields,
            context["findings"] != scan(fields),
        )
    ):
        raise ValueError("historical baseline request addresses different current source data")
    if any(
        (
            relevance.identity != context["identity"],
            relevance.binding_sha256 != context["binding_sha256"],
            relevance.disposition != "include",
            relevance.extraction != "complete",
            not relevance.evidence_addresses,
            set(relevance.evidence_addresses)
            - {span["address"] for span in context["structural_addresses"]},
        )
    ):
        raise ValueError("existing baseline relevance evidence is incomplete")
    if saved["item"]["media"]:
        context["confirmed_media_values"] = confirmed_media_values(
            saved, privacy_context(context), visual
        )
    structural = [
        {
            "address": f"b:{index}",
            "source_address": block.location,
            "sha256": block.sha256,
            "text": block.text,
        }
        for index, block in enumerate(blocks(body))
    ]
    if context["structural_addresses"] != structural:
        raise ValueError("historical source structural coverage changed")
    context["relevance_decision"] = relevance.model_dump()
    editor = ResponseEvidence.model_validate(saved["phases"]["privacy"])
    proposal = parse_response(
        editor, PrivacyDecision, EDITOR_MODEL, request_hash(source_logical_request(editor))
    )
    original_payload = json.loads(source_logical_request(editor)["input"][0]["content"][0]["text"])
    original_feedback = original_payload.get(
        "validation_feedback", original_payload.get("feedback", [])
    )
    expected = _request(
        EDITOR_MODEL, "privacy", context, visual, PrivacyDecision, original_feedback
    )
    if (
        source_logical_request(editor) != expected
        or proposal.identity != context["identity"]
        or proposal.binding_sha256 != privacy_context(context)["binding_sha256"]
        or proposal.unresolved
    ):
        raise ValueError("existing proposal request, source linkage or completion changed")
    return (context, visual, proposal)


def _selected_request(
    context: dict[str, Any],
    candidate: dict[str, Any],
    proposal: PrivacyDecision,
    visual: list[dict[str, Any]],
    saved: dict[str, Any],
    parent_sha: str,
) -> dict[str, Any]:
    body = _validation_request(
        context, candidate, proposal, visual, saved.get("privacy_feedback", [])
    )
    payload = json.loads(body["input"][0]["content"][0]["text"])
    payload["selected_existing_proposal"] = {
        "version": VERSION,
        "original_record_sha256": parent_sha,
        "original_editor_request_sha256": saved["phases"]["privacy"]["request_sha256"],
        "current_execution_sha256": _execution_digest(),
        "historical_feedback_entries": len(saved.get("privacy_feedback", [])),
        "new_editor_proposals": 0,
    }
    body["input"][0]["content"][0]["text"] = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    body["instructions"] = body["instructions"].replace(
        "\nPrompt version:",
        "\nValidation-only selection of the exact already completed proposal. validation_feedback is immutable HISTORICAL feedback, not a list of current candidate defects. Current source bytes, linkage, label syntax, rendering and file hashes were checked independently by the controller, but those checks are not privacy or preservation approval. Review the complete current source and candidate; report actual current defects. Never infer a current failure solely from an old generic error in history. No editor proposal or correction budget is granted.\nPrompt version:",
        1,
    )
    return body


def _verify_independent(
    checked: AdmissionValidation, context: dict[str, Any], candidate: dict[str, Any]
) -> None:
    checks = (
        "relevance_valid",
        "privacy_valid",
        "extraction_valid",
        "technical_relationships_preserved",
        "media_valid",
        "complete_document_reviewed",
        "complete_media_reviewed",
    )
    if any(
        (
            checked.identity != context["identity"],
            checked.binding_sha256 != context["binding_sha256"],
            checked.output_sha256 != candidate["output_sha256"],
            checked.approved,
            checked.retirement_valid,
            not all(getattr(checked, key) for key in checks),
            checked.failures,
            checked.unresolved,
        )
    ):
        raise ValueError("independent existing-proposal privacy validation failed")


def _verify_origin(output: Path, saved: dict[str, Any], selection: dict[str, Any]) -> None:
    if selection:
        history = output / "history" / (selection["original_record_sha256"] + ".json")
        if (
            not history.is_file()
            or sha(history.read_bytes()) != selection["original_record_sha256"]
        ):
            raise ValueError("selected proposal original history is unavailable or changed")
        previous = json.loads(history.read_bytes())
        original_fields = (
            "binding",
            "binding_sha256",
            "source",
            "item",
            "input_file_hashes",
            "privacy_feedback",
            "phases",
        )
        if any(previous.get(key) != saved.get(key) for key in original_fields):
            raise ValueError("selected original proposal or failure ledger changed")


def _retire_rejected(path: Path, evidence: ResponseEvidence, client: Any) -> None:
    if client is not None:
        invalidate = getattr(client, "invalidate", None)
        if invalidate is not None:
            invalidate(evidence)
        if path.exists():
            path.rename(path.with_suffix(".rejected-" + sha(path.read_bytes())))


def validate_existing_baseline(output: Path, record: Path, client: Any = None) -> dict[str, Any]:
    """Never issue another editor proposal or change its historical correction ledger."""
    original = record.read_bytes()
    saved: dict[str, Any] = json.loads(original)
    selection = saved.get("selected_existing_validation", {})
    verify_pending(
        output,
        saved,
        _execution_digest(),
        {
            "preparation_controller_sha256": preparation_controller(),
            "source_privacy_controller_sha256": review_context_digest(),
            "curation_policy_sha256": load_curation_policy().sha256,
            "version": "baseline-source-privacy/3",
        },
    )
    if selection and selection["execution_sha256"] != _execution_digest():
        raise ValueError("stale existing-proposal validation controller or policy")
    _verify_origin(output, saved, selection)
    context, visual, proposal = _context(output, saved)
    candidate, candidate_visual, projected, files = _compile(
        output / saved["source"], output, saved, context, proposal
    )
    body = _selected_request(
        context,
        candidate,
        proposal,
        visual + candidate_visual,
        saved,
        selection.get(
            "original_record_sha256",
            saved.get("existing_proposal_pending", {}).get("source_record_sha256", sha(original)),
        ),
    )
    path = output / "existing-validation" / record.stem / (request_hash(body) + ".json")
    if client is None and not path.is_file():
        raise ValueError("complete existing-proposal validation evidence is unavailable")
    evidence = _execute(path, body, client)
    try:
        checked = parse_response(evidence, AdmissionValidation, VALIDATOR_MODEL, body)
        _verify_independent(checked, context, candidate)
        enforce(candidate["findings"], projected, checked)
    except ValueError:
        _retire_rejected(path, evidence, client)
        raise
    selected = {
        "version": VERSION,
        "original_record_sha256": selection.get(
            "original_record_sha256",
            saved.get("existing_proposal_pending", {}).get("source_record_sha256", sha(original)),
        ),
        "execution_sha256": _execution_digest(),
        "request_sha256": request_hash(body),
        "response_sha256": evidence.response_sha256,
        "candidate_sha256": candidate["output_sha256"],
    }
    if selection:
        if selection != selected or saved["candidate"] != candidate:
            raise ValueError("existing-proposal validation replay changed")
        return saved
    preserve_history(output, original)
    for name, content in files.items():
        atomic_private(output / "reviewed" / record.stem / name, content)
    saved.pop("existing_proposal_pending", None)
    saved.update(
        status="privacy_only_validated",
        candidate=candidate,
        privacy_decision=proposal.model_dump(),
        privacy_validation_evidence=evidence.model_dump(mode="json"),
        privacy_validation_binding_sha256=context["binding_sha256"],
        selected_existing_validation=selected,
        enforcement_findings=0,
        retirement_review_required=True,
        privacy_retryable=False,
    )
    atomic_private(record, json_bytes(saved))
    return saved


def reusable_existing_validation(previous: dict[str, Any], binding: dict[str, Any]) -> bool:
    """Preserve the selected original envelope only with current executable proof."""
    selection = previous.get("selected_existing_validation")
    ignored = {
        "preparation_controller_sha256",
        "source_privacy_controller_sha256",
        "curation_policy_sha256",
        "version",
    }
    before = {
        key: value for key, value in previous.get("binding", {}).items() if key not in ignored
    }
    current = {key: value for key, value in binding.items() if key not in ignored}
    return bool(
        selection
        and selection.get("execution_sha256") == _execution_digest()
        and (before == current)
    )


def existing_validation_context(
    output: Path, saved: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Source context cannot be used without replaying its selected proof first."""
    record = output / "documents" / (sha(saved["binding"]["baseline_article"]) + ".json")
    if json.loads(record.read_bytes()) != saved:
        raise ValueError("existing baseline context differs from its selected record")
    validate_existing_baseline(output, record)
    context, visual, _ = _context(output, saved)
    return (context, visual)
