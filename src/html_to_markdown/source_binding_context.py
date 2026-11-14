"""Immutable source/privacy context derivation without review-model dependencies."""

from typing import Any

from .curation import json_bytes
from .enrichment.analysis import sha

PRIVACY_VERSION = "source-privacy/2"


def source_core(binding: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in binding.items()
        if key not in {"curation_policy_sha256", "api_policy_sha256"}
    }


def privacy_context(context: dict[str, Any]) -> dict[str, Any]:
    if "binding" not in context:
        return dict(context)
    binding = {**source_core(context["binding"]), "privacy_version": PRIVACY_VERSION}
    excluded = {
        "binding",
        "binding_sha256",
        "retirement_rules",
        "relevance_decision",
        "curation_candidate",
        "privacy_decision",
        "candidate",
    }
    return {
        **{key: value for key, value in context.items() if key not in excluded},
        "binding": binding,
        "binding_sha256": sha(json_bytes(binding)),
    }
