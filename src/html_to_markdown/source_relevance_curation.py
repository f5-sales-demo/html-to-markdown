"""Guarded whole-source relevance decisions kept separate from retirement labels."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .enrichment.analysis import sha
from .legacy_curation import LegacyFilter

KIND = "source_relevance_exclusion"


def decisions(raw: dict[str, Any]) -> list[dict[str, Any]]:
    result = [d for t in raw["topics"] for d in t["documents"] if d.get("review_kind") == KIND]
    urls = [d["url"] for d in result]
    if len(urls) != len(set(urls)):
        raise ValueError("duplicate source relevance exclusion")
    for d in result:
        if (
            d.get("disposition") != "remove"
            or d.get("reason") != "independently_validated_source_relevance_exclusion"
        ):
            raise ValueError("source relevance exclusion lacks governed disposition")
        for key in [
            "input_sha256",
            "source_sha256",
            "baseline_receipt_sha256",
            "renewal_binding_sha256",
            "renewal_validation_sha256",
        ]:
            if not re.fullmatch(r"[0-9a-f]{64}", d.get(key, "")):
                raise ValueError("source relevance exclusion requires complete evidence pins")
        if not isinstance(d.get("media_sha256"), list) or any(
            not re.fullmatch(r"[0-9a-f]{64}", pin) for pin in d["media_sha256"]
        ):
            raise ValueError("source relevance exclusion requires complete media guards")
    return result


def excluded(raw: dict[str, Any], path: Path, metadata: dict[str, Any], body: str) -> bool:
    from .curation import canonical, media_inventory  # pylint: disable=import-outside-toplevel

    decision = next(
        (d for d in decisions(raw) if canonical(d["url"]) == canonical(metadata["url"])), None
    )
    if decision is None:
        return False
    media = media_inventory(path, body)
    if not all(
        (
            sha(body) == decision["input_sha256"],
            LegacyFilter.source_digest(metadata, body) == decision["source_sha256"],
            all(item["kind"] == "local" and item["sha256"] for item in media),
            sorted(set(item["sha256"] for item in media)) == sorted(decision["media_sha256"]),
        )
    ):
        raise ValueError("changed source relevance exclusion requires renewed independent review")
    return True


def policy_decision(context: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    from .curation import json_bytes  # pylint: disable=import-outside-toplevel

    return {
        "url": context["metadata"]["url"],
        "disposition": "remove",
        "review_kind": KIND,
        "reason": "independently_validated_source_relevance_exclusion",
        "input_sha256": sha(context["body"]),
        "source_sha256": LegacyFilter.source_digest(context["metadata"], context["body"]),
        "media_sha256": sorted(context["media"]),
        "baseline_receipt_sha256": context["evidence"]["baseline_receipt_sha256"],
        "renewal_binding_sha256": context["binding_sha256"],
        "renewal_validation_sha256": sha(json_bytes(result)),
    }
