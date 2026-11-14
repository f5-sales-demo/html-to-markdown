"""Selected immutable API eligibility evidence for complete actual-planner review."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from .content_policy import (
    API_ALIASES,
    DESTINATION_ROOT,
    URL_TOKEN,
    ContentPolicy,
    load_content_policy,
    rewrite_text,
)
from .enrichment.analysis import sha


def pending_api_evidence(
    context: dict[str, Any], candidate: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Qualify future planner mappings while preserving the source-stage article."""
    active = load_content_policy()
    body = context["body"] if candidate is None else candidate["body"]
    preview, _ = rewrite_text(body, context["identity"], active)
    result = api_reference_evidence(
        context, {"candidate": {"body": body}}, {"body": preview}, active
    )
    return {
        **result,
        "stage": "pending_actual_planner",
        "source_body_sha256": sha(body),
        "migration_preview_sha256": sha(preview),
    }


def api_reference_evidence(
    context: dict[str, Any],
    reviewed: dict[str, Any],
    candidate: dict[str, Any],
    policy: ContentPolicy | None = None,
) -> dict[str, Any]:
    """Supply each selected target, source mapping and qualifying record without truncation."""
    active = policy or load_content_policy()
    active.validate_projection()
    urls = sorted(
        {
            match.group().rstrip(".,;:")
            for match in URL_TOKEN.finditer(candidate["body"])
            if match.group().startswith(DESTINATION_ROOT)
        }
    )
    result: dict[str, Any] = {
        "schema_version": 1,
        "policy_sha256": active.policy_digest,
        "catalog_sha256": active.catalog_digest,
        "policy": deepcopy(active.raw),
        "aliases": [{"alias": alias, "canonical": canonical} for alias, canonical in API_ALIASES],
        "targets": [],
        "operations": [],
        "mappings": [],
    }
    if not urls:
        return result
    allowed = {
        active.fallback,
        *(op["url"] for op in active.operations),
        *(op["overview_url"] for op in active.operations),
    }
    if not set(urls) <= allowed:
        raise ValueError("actual planner contains an unqualified API target")
    specification = active.catalog.get("specification")
    inventory = active.catalog.get("route_inventory")
    if not isinstance(specification, dict) or not isinstance(inventory, dict):
        raise ValueError("API qualification lacks pinned specification or route evidence")
    _, mappings = rewrite_text(reviewed["candidate"]["body"], context["identity"], active)
    selected = [item for item in mappings if item["destination"] in urls]
    operation_urls = set(urls) | {url for item in selected for url in item["candidates"]}
    operations = [deepcopy(op) for op in active.operations if op["url"] in operation_urls]
    qualified = active.catalog.get("qualification", [])
    targets = []
    for url in urls:
        checks = [row for row in qualified if row.get("url") == url]
        if (
            len(checks) != 1
            or checks[0].get("status") != 200
            or not re.fullmatch(r"[0-9a-f]{64}", str(checks[0].get("html_sha256", "")))
        ):
            raise ValueError("API destination qualification is missing or ambiguous")
        if url != active.fallback and url not in inventory.get("routes", []):
            raise ValueError("API destination is absent from the pinned route inventory")
        targets.append({"url": url, "qualification": deepcopy(checks[0])})
    result.update(
        specification=deepcopy(specification),
        route_inventory={
            **{k: deepcopy(v) for k, v in inventory.items() if k != "routes"},
            "selected_routes": [url for url in urls if url != active.fallback],
        },
        targets=targets,
        operations=sorted(operations, key=lambda op: op["url"]),
        mappings=deepcopy(selected),
    )
    return result
