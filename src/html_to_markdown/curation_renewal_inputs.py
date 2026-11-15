"""Prepare private complete-source catalog reviews from admission exceptions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .content_inventory import atomic_private
from .curation import canonical, json_bytes, load_curation_policy
from .curation_renewal import proposal_context
from .enrichment.analysis import sha
from .enrichment.contracts import EDITOR_MODEL, ResponseEvidence
from .enrichment.gates import parse_response, request_hash
from .media_observation_jobs import confirmed_media_values as _confirmed_media_values
from .source_phase_bindings import privacy_context
from .source_review import PrivacyDecision, RelevanceDecision
from .source_review_jobs import _compile, _context, _request


def _topic_rules(context: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    keys = {
        "id",
        "version",
        "mode",
        "retired_identities",
        "retired_patterns",
        "retired_urls",
        "scope_patterns",
        "dependency_rules",
        "provider_identities",
        "provider_aliases",
        "retired_documentation_urls",
        "omit_urls",
        "candidate_detectors",
    }
    url = canonical(context["metadata"]["url"])
    media_ids = set(context["media"]) | set(candidate["media"])
    return [
        {
            **{key: value for key, value in topic.items() if key in keys},
            "documents": [
                decision for decision in topic["documents"] if canonical(decision["url"]) == url
            ],
            "media": [decision for decision in topic["media"] if decision["sha256"] in media_ids],
        }
        for topic in load_curation_policy().raw["topics"]
    ]


# A prepared record binds both complete original and privacy-stage evidence.
# pylint: disable-next=too-many-locals
def prepare_renewals(capture: Path, review: Path, output: Path) -> dict[str, Any]:
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    documents = []
    for record in sorted((review / "documents").glob("*.json")):
        saved = json.loads(record.read_bytes())
        if saved.get("reason") != "source_curation_requires_review":
            continue
        entry = {"identity": saved["item"]["identity"], "disposition": "queued"}
        try:
            source, original_visual = _context(capture, review, saved)
            relevance_body = _request(
                EDITOR_MODEL, "relevance", source, original_visual, RelevanceDecision
            )
            relevance = parse_response(
                ResponseEvidence.model_validate(saved["phases"]["relevance"]),
                RelevanceDecision,
                EDITOR_MODEL,
                request_hash(relevance_body),
            )
            if relevance.disposition != "include" or relevance.extraction != "complete":
                raise ValueError("curation renewal requires complete source relevance")
            source["relevance_decision"] = relevance.model_dump()
            if "media_observation_validation" in saved["phases"]:
                original = {
                    key: value for key, value in source.items() if key != "relevance_decision"
                }
                source["confirmed_media_values"] = _confirmed_media_values(
                    saved, privacy_context(original), original_visual
                )
            privacy_body = _request(
                EDITOR_MODEL,
                "privacy",
                source,
                original_visual,
                PrivacyDecision,
                saved.get("privacy_feedback"),
            )
            privacy = parse_response(
                ResponseEvidence.model_validate(saved["phases"]["privacy"]),
                PrivacyDecision,
                EDITOR_MODEL,
                request_hash(privacy_body),
            )
            candidate, candidate_visual, _, files = _compile(
                capture, review, saved, source, privacy
            )
            evidence = {
                "source_article_html": source["source_article_html"],
                "original": {key: source[key] for key in ("metadata", "body", "media", "findings")},
                "source_admission_binding_sha256": source["binding_sha256"],
                "privacy_decision": privacy.model_dump(),
                "privacy_candidate": candidate,
                "policy_topics": _topic_rules(source, candidate),
            }
            context = proposal_context(
                candidate["metadata"],
                candidate["body"],
                candidate["media"],
                candidate["output_sha256"],
                load_curation_policy().sha256,
                evidence,
            )
            directory = output / context["binding_sha256"]
            for relative, data in files.items():
                path = Path(relative)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or path.parts[:2] != ("articles", "content")
                ):
                    raise ValueError("unsafe curation renewal source file path")
                atomic_private(directory / "inputs" / path, data)
            atomic_private(directory / "context.json", json_bytes(context))
            atomic_private(
                directory / "visual.json", json_bytes(original_visual + candidate_visual)
            )
            entry.update(
                binding_sha256=context["binding_sha256"],
                context_path=context["binding_sha256"] + "/context.json",
                visual_path=context["binding_sha256"] + "/visual.json",
                reason="independent_catalog_review_required",
            )
        except (ValueError, KeyError) as failure:
            atomic_private(
                output / "prerequisite-errors" / (sha(record.read_bytes()) + ".json"),
                json_bytes(
                    {"source_record_sha256": sha(record.read_bytes()), "error": str(failure)}
                ),
            )
            entry.update(
                reason="source_review_prerequisite_failed", error_class=type(failure).__name__
            )
        documents.append(entry)
    result = {
        "schema_version": 1,
        "capture_inventory_sha256": sha((capture / "inventory.json").read_bytes()),
        "documents": documents,
    }
    atomic_private(output / "renewal-inventory.json", json_bytes(result))
    return result
