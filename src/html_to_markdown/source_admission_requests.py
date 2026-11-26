"""Exact source admission requests, independent of their worker orchestration."""

from __future__ import annotations

from typing import Any

from .source_phase_bindings import privacy_context, privacy_request
from .source_review import (
    AdmissionValidation,
    PrivacyDecision,
    RelevanceDecision,
    addressed_spans,
    admission_request,
)


def source_request(
    model: str,
    phase: str,
    context: dict[str, Any],
    visual: list[dict[str, Any]],
    schema: Any,
    feedback: list[str] | None = None,
) -> dict[str, Any]:
    if phase == "privacy":
        context = privacy_context(context)
    payload = (
        {
            **context,
            "addressed_spans": addressed_spans(context["fields"]),
            "validation_feedback": feedback or [],
        }
        if phase == "privacy"
        else context
    )
    body = (
        privacy_request(model, payload, schema)
        if phase == "privacy"
        else admission_request(model, phase, payload, schema)
    )
    body["input"][0]["content"].extend(visual)
    properties = body["text"]["format"]["schema"]["properties"]
    properties["identity"]["enum"] = [context["identity"]]
    properties["binding_sha256"]["enum"] = [context["binding_sha256"]]
    if schema is RelevanceDecision:
        addresses = [block["address"] for block in context["structural_addresses"]]
        properties["evidence_addresses"]["items"]["enum"] = addresses
    elif schema is PrivacyDecision:
        schema_body = body["text"]["format"]["schema"]
        editable = {"title", "description", "tags", "breadcrumb", "slug"}
        schema_body["$defs"]["TextChange"]["properties"]["field"]["enum"] = [
            field
            for field in context["fields"]
            if field == "body" or field.split("/")[1:2] in [[key] for key in editable]
        ]
        finding_ids = [finding["id"] for finding in context["findings"]]
        properties["findings"].update(minItems=len(finding_ids), maxItems=len(finding_ids))
        if finding_ids:
            schema_body["$defs"]["FindingDecision"]["properties"]["finding_id"]["enum"] = (
                finding_ids
            )
        media_ids = sorted(context["media"])
        if media_ids:
            schema_body["$defs"]["MediaChange"]["properties"]["source_sha256"]["enum"] = media_ids
        else:
            properties["media_changes"]["maxItems"] = 0
    elif schema is AdmissionValidation:
        properties["output_sha256"].update(
            enum=[context["candidate"]["output_sha256"]],
            description="Exact source-admission candidate.output_sha256; the later curation_candidate has a separate digest.",
        )
        identifiers = [finding["id"] for finding in context["candidate"]["findings"]]
        if identifiers:
            properties["approved_safe_findings"]["items"]["enum"] = identifiers
        else:
            properties["approved_safe_findings"]["maxItems"] = 0
    return body
