"""Private, resumable admission phases with complete offline replay."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .content_inventory import atomic_private, verify_capture
from .curation import blocks, json_bytes
from .enrichment.analysis import sha
from .enrichment.clients import client_for_host
from .enrichment.codex_client import CodexClient, is_transient_error
from .enrichment.contracts import EDITOR_MODEL, VALIDATOR_MODEL, ResponseEvidence
from .enrichment.gates import request_hash
from .enrichment.media import all_frame_inputs
from .errors import TransportFailure
from .media_encoding import review_media_encoding, source_review_encoding
from .media_observation_jobs import (
    RequestExecutor,
    prepare_media_observations,
)
from .media_observation_jobs import (
    confirmed_media_values as _confirmed_media_values,
)
from .render import split_document
from .review_controller_compatibility import admission_binding
from .review_priorities import ReviewPriorities, select_records
from .source_admission_requests import source_request as _request
from .source_boundary_review import (
    execute_source_phase,
    has_retryable_boundary,
)
from .source_boundary_review import (
    parse_source_response as parse_response,
)
from .source_candidate import article_html as _article_html
from .source_candidate import media_context as _candidate_media_context
from .source_candidate import render_source_proposal as _render_source_proposal
from .source_candidate import retirement_rules as _retirement_rules
from .source_curation import CurationRequired, preview_curation, record_exception
from .source_exclusion import MARKER as EXCLUSION_MARKER
from .source_exclusion import eligible as exclusion_eligible
from .source_exclusion import read_relevance, review_exclusion
from .source_existing_delivery import replay_existing_source
from .source_phase_bindings import (
    _privacy_budget,
    invalidate_failed_review,
    pending_privacy_proposal,
    privacy_context,
    privacy_ledger,
    reusable_privacy,
)
from .source_review import (
    VERSION,
    AdmissionValidation,
    FindingDecision,
    PrivacyDecision,
    RelevanceDecision,
    binding_for,
    enforce,
    scan,
    text_fields,
    validate_admission,
)
from .source_review_outputs import retire_output as _retire_output
from .source_review_reconciliation import (
    invalidate_classification,
    replay_classification,
    review_final_findings,
)
from .source_review_replay import verify_source_reviews

# Preserve the historical helper import surface after separating implementation.
__all__ = [
    "_article_html",
    "_request",
    "prepare_reviews",
    "review_one",
    "run_reviews",
    "verify_reviews",
]


def prepare_reviews(capture: Path, output: Path) -> dict[str, Any]:
    inventory = json.loads((capture / "inventory.json").read_bytes())
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    records = []
    for item in inventory["documents"]:
        destination = output / "documents" / (sha(item["identity"]) + ".json")
        saved = json.loads(destination.read_bytes()) if destination.exists() else {}
        if not item.get("capture_complete") or item["disposition"] == "policy_excluded":
            if saved:
                atomic_private(
                    output / "history" / (sha(destination.read_bytes()) + ".json"),
                    destination.read_bytes(),
                )
                _retire_output(output, saved)
            records.append(
                {
                    "identity": item["identity"],
                    "url": item["url"],
                    "sourceId": item["sourceId"],
                    "disposition": item["disposition"],
                    "reason": item["reason"],
                }
            )
            continue
        verify_capture(capture, item)
        binding = binding_for(item, source_article_sha256=sha(_article_html(capture, item)))
        binding = admission_binding(saved, binding)
        digest = sha(json_bytes(binding))
        if saved.get("binding_sha256") != digest:
            retained = {
                **_privacy_budget(saved, binding),
                **privacy_ledger(saved, binding),
                **pending_privacy_proposal(saved, binding),
                **reusable_privacy(saved, binding),
            }
            if saved:
                history = output / "history" / (sha(destination.read_bytes()) + ".json")
                atomic_private(history, destination.read_bytes())
                _retire_output(output, saved)
            saved = {
                "schema_version": 1,
                "version": VERSION,
                "item": item,
                "binding": binding,
                "binding_sha256": digest,
                "phases": {},
                "media_encoding_version": source_review_encoding(saved),
                "disposition": "queued",
                "reason": "admission_review_required",
                **retained,
            }
            atomic_private(destination, json_bytes(saved))
        elif saved["item"] != item:
            # Capture age and raw response wrappers do not change the reviewed
            # article. Bind the fresh raw receipt while retaining exact decisions.
            atomic_private(
                output / "history" / (sha(destination.read_bytes()) + ".json"),
                destination.read_bytes(),
            )
            saved["item"] = item
            atomic_private(destination, json_bytes(saved))
        records.append(
            {
                "identity": item["identity"],
                "binding_sha256": digest,
                "path": destination.relative_to(output).as_posix(),
            }
        )
    active = {record["path"] for record in records if "path" in record}
    for path in (output / "documents").glob("*.json"):
        if path.relative_to(output).as_posix() not in active:
            _retire_output(output, json.loads(path.read_bytes()))
    result = {
        "schema_version": 1,
        "version": VERSION,
        "inventory_sha256": sha((capture / "inventory.json").read_bytes()),
        "capture_binding": inventory["binding"],
        "documents": records,
        "enumeration_complete": inventory["completeness"]["enumeration"],
    }
    atomic_private(output / "review-inventory.json", json_bytes(result))
    return result


def _media_context(
    capture: Path, output: Path, item: dict[str, Any], *, require_cached: bool = False
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any]]:
    return _candidate_media_context(
        capture, output, item, require_cached=require_cached, frame_inputs=all_frame_inputs
    )


def _context(
    capture: Path, output: Path, saved: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    item = saved["item"]
    verify_capture(capture, item)
    source_article = _article_html(capture, item)
    if (
        admission_binding(saved, binding_for(item, source_article_sha256=sha(source_article)))
        != saved["binding"]
        or sha(json_bytes(saved["binding"])) != saved["binding_sha256"]
    ):
        raise ValueError("stale source policy or admission binding")
    metadata, body = split_document((capture / item["article_path"]).read_text())
    rules = _retirement_rules()
    visual, media_fields, media_receipts = _media_context(capture, output, item)
    fields = {**text_fields(metadata, body), **media_fields}
    context = {
        "identity": item["identity"],
        "binding_sha256": saved["binding_sha256"],
        "binding": saved["binding"],
        "metadata": metadata,
        "body": body,
        "source_article_html": str(source_article),
        "retirement_rules": rules,
        "structural_addresses": [
            {
                "address": f"b:{index}",
                "source_address": block.location,
                "sha256": block.sha256,
                "text": block.text,
            }
            for index, block in enumerate(blocks(body))
        ],
        "fields": fields,
        "findings": scan(fields),
        "media": media_receipts,
    }
    return context, visual


def _prepare_media_observations(
    capture: Path,
    output: Path,
    record: Path,
    client: RequestExecutor,
    feedback: list[str] | None = None,
) -> None:
    prepare_media_observations(
        capture, output, record, client, _privacy_observation_context, feedback
    )


def _privacy_observation_context(
    capture: Path, output: Path, saved: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    context, visual = _context(capture, output, saved)
    return privacy_context(context), visual


def _privacy_media_intake(
    capture: Path,
    output: Path,
    record: Path,
    phase: str,
    client: CodexClient | None,
    context: dict[str, Any],
    visual: list[dict[str, Any]],
) -> dict[str, Any]:
    """Confirm native guards before proposals and replay the exact privacy context."""
    saved: dict[str, Any] = json.loads(record.read_bytes())
    if (
        phase == "privacy"
        and saved["item"]["media"]
        and "media_observation_validation" not in saved["phases"]
    ):
        if client is None:
            raise ValueError(
                "complete confirmed native media guards are required before privacy inference"
            )
        _prepare_media_observations(capture, output, record, client)
        saved = json.loads(record.read_bytes())
    if "media_observation_validation" in saved["phases"]:
        context["confirmed_media_values"] = _confirmed_media_values(
            saved, privacy_context(context), visual
        )
    return saved


def _compile(
    capture: Path,
    output: Path,
    saved: dict[str, Any],
    context: dict[str, Any],
    proposal: PrivacyDecision,
) -> tuple[dict[str, Any], list[dict[str, Any]], PrivacyDecision, dict[str, bytes]]:
    with review_media_encoding(source_review_encoding(saved)):
        return _compile_encoded(capture, output, saved, context, proposal)


# Preserve all stage evidence and checks in one transaction.
# pylint: disable-next=too-many-locals,too-many-statements
def _compile_encoded(
    capture: Path,
    output: Path,
    saved: dict[str, Any],
    context: dict[str, Any],
    proposal: PrivacyDecision,
) -> tuple[dict[str, Any], list[dict[str, Any]], PrivacyDecision, dict[str, bytes]]:
    stale = (
        proposal.identity != context["identity"]
        or proposal.binding_sha256 != privacy_context(context)["binding_sha256"]
    )
    if stale or proposal.unresolved:
        raise ValueError(
            "privacy proposal is stale"
            if stale
            else "privacy proposal is unresolved: " + "; ".join(proposal.unresolved)
        )
    candidate, visual, projected, files = _render_source_proposal(
        capture, output, saved, context, proposal, media_context=_media_context
    )
    candidate, decisions = _compiled_findings(
        capture, output, saved, context, candidate, visual, proposal
    )
    return candidate, visual, proposal.model_copy(update={"findings": decisions}), files


def _compiled_findings(
    capture: Path,
    output: Path,
    saved: dict[str, Any],
    context: dict[str, Any],
    candidate: dict[str, Any],
    visual: list[dict[str, Any]],
    proposal: PrivacyDecision,
) -> tuple[dict[str, Any], list[FindingDecision]]:
    if "privacy_reconciliation" in saved.get("phases", {}) and not saved["phases"][
        "privacy_reconciliation"
    ].get("error"):
        original_visual, _, _ = _media_context(capture, output, saved["item"])
        candidate = replay_classification(
            saved, privacy_context(context), candidate, original_visual + visual, proposal
        )
    return candidate, [FindingDecision.model_validate(c) for c in candidate["finding_decisions"]]


def _compile_admission(
    capture: Path,
    output: Path,
    record: Path,
    context: dict[str, Any],
    visual: list[dict[str, Any]],
    proposal: PrivacyDecision,
    client: CodexClient | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], PrivacyDecision, dict[str, bytes]]:
    saved = json.loads(record.read_bytes())
    candidate, candidate_visual, projected, files = _compile(
        capture, output, saved, context, proposal
    )
    if any(choice.disposition == "unresolved" for choice in projected.findings):
        if client is None:
            raise ValueError("new final privacy findings require the MacBook classifier")
        review_final_findings(
            saved,
            record,
            output,
            privacy_context(context),
            candidate,
            visual + candidate_visual,
            client,
            proposal,
        )
        saved = json.loads(record.read_bytes())
        return _compile(capture, output, saved, context, proposal)
    return candidate, candidate_visual, projected, files


# Preserve all stage evidence and checks in one transaction.


def review_one(
    capture: Path, output: Path, record: Path, phase: str, client: CodexClient | None
) -> dict[str, Any]:
    with review_media_encoding(source_review_encoding(json.loads(record.read_bytes()))):
        return _review_one(capture, output, record, phase, client)


# pylint: disable-next=too-many-locals,too-many-branches
def _review_one(
    capture: Path, output: Path, record: Path, phase: str, client: CodexClient | None
) -> dict[str, Any]:
    saved = json.loads(record.read_bytes())
    if saved.get("selected_existing_source"):
        return replay_existing_source(capture, output, record)
    context, visual = _context(capture, output, saved)
    if phase == "relevance" and not saved.get(EXCLUSION_MARKER):
        body = _request(EDITOR_MODEL, phase, context, visual, RelevanceDecision)
    else:
        relevance_body = _request(EDITOR_MODEL, "relevance", context, visual, RelevanceDecision)
        relevance = read_relevance(saved, relevance_body, context)
        if saved.get(EXCLUSION_MARKER) or (phase == "privacy" and exclusion_eligible(relevance)):
            return review_exclusion(saved, context, visual, relevance, output, record, client)
        if saved.get("privacy_exhausted") and phase in {"privacy", "validation"}:
            raise ValueError(
                "privacy proposal exhausted two corrections; unchanged privacy context remains queued"
            )
        context["relevance_decision"] = relevance.model_dump()
        saved = _privacy_media_intake(capture, output, record, phase, client, context, visual)
        if phase == "privacy":
            body = _request(
                EDITOR_MODEL, phase, context, visual, PrivacyDecision, saved.get("privacy_feedback")
            )
        else:
            privacy_body = _request(
                EDITOR_MODEL,
                "privacy",
                context,
                visual,
                PrivacyDecision,
                saved.get("privacy_feedback"),
            )
            proposal = parse_response(
                ResponseEvidence.model_validate(saved["phases"]["privacy"]),
                PrivacyDecision,
                EDITOR_MODEL,
                privacy_body,
            )
            candidate, candidate_visual, projected, files = _compile_admission(
                capture,
                output,
                record,
                context,
                visual,
                proposal,
                client if phase != "replay" else None,
            )
            saved = json.loads(record.read_bytes())
            context["privacy_decision"] = proposal.model_dump()
            context["candidate"] = candidate
            if relevance.disposition == "include" and relevance.retirement != "excluded":
                context["curation_candidate"] = preview_curation(
                    output,
                    files,
                    saved["item"].get("published_path", saved["item"]["article_path"]),
                )
            body = _request(
                VALIDATOR_MODEL,
                "validation",
                context,
                visual + candidate_visual,
                AdmissionValidation,
            )
            if phase == "replay":
                validation = parse_response(
                    ResponseEvidence.model_validate(saved["phases"]["validation"]),
                    AdmissionValidation,
                    VALIDATOR_MODEL,
                    body,
                )
                if "curation_candidate" in context and not validation.retirement_valid:
                    record_exception(output, candidate["output_sha256"], candidate["metadata"])
                    raise CurationRequired(CurationRequired.reason)
                validate_admission(
                    context["identity"],
                    context["binding_sha256"],
                    candidate["output_sha256"],
                    validation,
                )
                enforce(candidate["findings"], projected, validation)
                saved.pop("privacy_proposal_pending_validation", None)
                saved.update(
                    disposition="accepted"
                    if relevance.disposition == "include" and relevance.retirement != "excluded"
                    else "policy_excluded",
                    reason="independently_validated_source_admission",
                    output_sha256=candidate["output_sha256"],
                    file_hashes=candidate["file_hashes"],
                    transforms=candidate["media_transforms"],
                    enforcement_findings=0,
                )
                if "curation_candidate" in context:
                    saved["curation_sha256"] = context["curation_candidate"]["curation_sha256"]
                if saved["disposition"] == "accepted":
                    for path, data in files.items():
                        published = Path(path).relative_to("articles")
                        atomic_private(output / "reviewed" / published, data)
                atomic_private(record, json_bytes(saved))
                return dict(saved)
    existing = saved["phases"].get(phase)
    if existing and existing.get("error") is None:
        try:
            schemas: dict[str, type[BaseModel]] = {
                "relevance": RelevanceDecision,
                "privacy": PrivacyDecision,
                "validation": AdmissionValidation,
            }
            parse_response(
                ResponseEvidence.model_validate(existing),
                schemas[phase],
                body["model"],
                body,
            )
            return dict(saved)
        except ValueError:
            atomic_private(
                output / "history" / (sha(json_bytes(existing)) + ".json"), json_bytes(existing)
            )
    if client is None:
        raise ValueError("inference phase requires the MacBook Codex client")
    if phase == "privacy" and saved.get("privacy_proposal_pending_validation"):
        raise ValueError(
            "pending privacy proposal request changed; exact validation remains queued"
        )
    if phase == "privacy" and "privacy_reconciliation" in saved["phases"]:
        atomic_private(
            output / "history" / (sha(record.read_bytes()) + ".json"), record.read_bytes()
        )
        saved["phases"].pop("privacy_reconciliation")
    saved["phases"][phase] = execute_source_phase(
        body, client, output / "native-boundaries" / request_hash(body), record, phase, saved
    ).model_dump()
    atomic_private(record, json_bytes(saved))
    return dict(saved)


def _run_record(
    capture: Path, output: Path, record: Path, phase: str, client: CodexClient | None
) -> None:
    with review_media_encoding(source_review_encoding(json.loads(record.read_bytes()))):
        _run_record_encoded(capture, output, record, phase, client)


def _run_record_encoded(
    capture: Path, output: Path, record: Path, phase: str, client: CodexClient | None
) -> None:
    if json.loads(record.read_bytes()).get("cross_queue_evidence_blocked"):
        raise ValueError("prior source-review evidence requires repair before new privacy attempts")
    saved = json.loads(record.read_bytes())
    if (
        saved.get("privacy_exhausted")
        and not saved.get(EXCLUSION_MARKER)
        and not saved.get("selected_existing_source")
    ):
        if not saved.get("phases", {}).get("relevance"):
            raise ValueError(
                "privacy proposal exhausted two corrections; unchanged privacy context remains queued"
            )
        # An existing exact exclusion can be independently checked without a
        # new privacy proposal. Missing/stale classifications cannot buy inference.
        review_one(capture, output, record, "relevance", None)
    phases = ["relevance", "privacy", "validation", "replay"] if phase == "all" else [phase]
    for current in phases:
        while True:
            try:
                review_one(capture, output, record, current, client)
                break
            except TransportFailure:
                raise
            except ValueError as failure:
                saved = json.loads(record.read_bytes())
                invalidate_classification(saved, client)
                invalidate_failed_review(saved, current, client)
                feedback = saved.get("privacy_feedback", [])
                correctable = current in {"validation", "replay"} and any(
                    marker in str(failure)
                    for marker in (
                        "privacy proposal",
                        "privacy finding",
                        "synthetic",
                        "transformation",
                        "replacement",
                        "text change",
                        "source value",
                        "rectangle",
                        "static media cannot have frame changes",
                        "duplicate animated privacy frame",
                    )
                )
                if phase != "all" or not correctable or len(feedback) >= 2 or client is None:
                    raise
                if saved["item"]["media"] and "privacy proposal is unresolved" in str(failure):
                    _prepare_media_observations(
                        capture, output, record, client, [*feedback, str(failure)]
                    )
                    saved = json.loads(record.read_bytes())
                atomic_private(
                    output / "history" / (sha(record.read_bytes()) + ".json"), record.read_bytes()
                )
                saved["privacy_feedback"] = [*feedback, str(failure)]
                saved["phases"].pop("privacy", None)
                saved["phases"].pop("validation", None)
                saved["phases"].pop("privacy_reconciliation", None)
                atomic_private(record, json_bytes(saved))
                review_one(capture, output, record, "privacy", client)
                if current == "replay":
                    review_one(capture, output, record, "validation", client)


# The batch receipt includes every existing admission and exception disposition.
# pylint: disable-next=too-many-locals,too-many-branches
def run_reviews(
    capture: Path,
    output: Path,
    *,
    phase: str,
    journal: Path,
    limit: int | None = None,
    priorities: ReviewPriorities | None = None,
) -> dict[str, Any]:
    if phase == "prepare":
        return prepare_reviews(capture, output)
    if phase not in {"relevance", "privacy", "validation", "replay", "all"}:
        raise ValueError(
            "review phase must be prepare, relevance, privacy, validation, replay or all"
        )
    if limit is not None and (limit < 1 or phase != "all"):
        raise ValueError("review batches require all phases and a positive document limit")
    inventory = json.loads((output / "review-inventory.json").read_bytes())
    records = [output / record["path"] for record in inventory["documents"] if "path" in record]
    selected = records
    if limit is not None:
        selected = select_records(output, limit, priorities)
    ready = []
    failures: dict[str, str] = {}
    for record in selected:
        saved = json.loads(record.read_bytes())
        try:
            if not saved.get("selected_existing_source"):
                with review_media_encoding(source_review_encoding(saved)):
                    _media_context(capture, output, saved["item"])
            ready.append(record)
        except Exception as failure:  # pylint: disable=broad-exception-caught
            failures[record.name] = "MediaPreparationError"
            atomic_private(
                output / "errors" / (record.name + ".json"),
                json_bytes({"phase": "media", "error": str(failure)}),
            )
    client = None
    if phase != "replay" and ready:
        client = client_for_host(journal, local_client=CodexClient)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(_run_record, capture, output, record, phase, client): record
                for record in ready
            }
            for count, future in enumerate(as_completed(futures), 1):
                record = futures[future]
                try:
                    future.result()
                except Exception as failure:  # pylint: disable=broad-exception-caught
                    failures[record.name] = type(failure).__name__
                    error = {"phase": phase, "error": str(failure)}
                    atomic_private(output / "errors" / (record.name + ".json"), json_bytes(error))
                    atomic_private(
                        output
                        / "error-history"
                        / (sha(json_bytes(error) + record.read_bytes()) + ".json"),
                        json_bytes(error),
                    )
                atomic_private(
                    output / "review-progress.json",
                    json_bytes(
                        {
                            "processed": count,
                            "batch_size": len(ready),
                            "available": len(records),
                            "exceptions": len(failures),
                        }
                    ),
                )
    finally:
        if client is not None:
            client.close()
    selected_names = {record.name for record in selected}
    result: dict[str, Any] = {
        "schema_version": 1,
        "phase": phase,
        "documents": [],
        "exceptions": {},
        "pending": 0,
        "processed": len(selected),
    }
    for record in records:
        saved = json.loads(record.read_bytes())
        if record.name in selected_names:
            saved["attempted_binding_sha256"] = saved["binding_sha256"]
            saved["retryable"] = any(
                is_transient_error(response.get("error")) for response in saved["phases"].values()
            ) or has_retryable_boundary(saved)
            if record.name in failures:
                _retire_output(output, saved)
                saved.update(
                    disposition="queued",
                    reason=CurationRequired.reason
                    if failures[record.name] == "CurationRequired"
                    else "admission_phase_failed",
                    error_class=failures[record.name],
                )
            else:
                saved.pop("error_class", None)
            atomic_private(record, json_bytes(saved))
        if saved.get("error_class"):
            result["exceptions"][record.name] = saved["error_class"]
        if saved["disposition"] == "queued" and (
            saved.get("attempted_binding_sha256") != saved["binding_sha256"]
            or saved.get("retryable")
        ):
            result["pending"] += 1
        result["documents"].append(
            {
                "identity": saved["item"]["identity"],
                "url": saved["item"]["url"],
                "disposition": saved["disposition"],
                "reason": saved["reason"],
                "binding_sha256": saved["binding_sha256"],
            }
        )
    atomic_private(output / "review-report.json", json_bytes(result))
    return result


def verify_reviews(capture: Path, output: Path) -> dict[str, Any]:
    """Replay every complete admission through the bounded independent verifier."""
    return verify_source_reviews(capture, output, review_one)
