"""Allowlisted CI inventory receipts; raw source and model evidence stay private."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .content_inventory import atomic_private
from .curation import json_bytes
from .enrichment.analysis import sha
from .urls import SOURCE_ROOTS, canonicalize_url, infer_source

STATES = {
    "queued",
    "policy_excluded",
    "capture_error",
    "access_queue",
    "accepted",
    "include",
    "exclude",
    "review",
    "success",
    "failed",
    "carried_forward",
    "new",
    "changed",
    "unchanged",
    "fallback",
    "unavailable",
    "removed_navigation",
}


def sanitize_inventory(source: Path, destination: Path) -> dict[str, Any]:
    raw = source.read_bytes()
    inventory = json.loads(raw)
    items = inventory.get("documents", inventory.get("topics", []))
    documents = []
    for item in items:
        url = item.get("url")
        source_id = infer_source(url) if isinstance(url, str) and not urlsplit(url).query else None
        state = next(
            (
                item[key]
                for key in ("disposition", "decision", "change", "status")
                if item.get(key) in STATES
            ),
            "review",
        )
        entry = {
            "identity_sha256": sha(str(item.get("identity", url or item.get("id", "")))),
            "disposition": state,
        }
        if source_id in SOURCE_ROOTS:
            entry.update(sourceId=source_id, url=canonicalize_url(url))
        for key in ("source_html_sha256", "article_sha256", "binding_sha256"):
            if re.fullmatch(r"[0-9a-f]{64}", str(item.get(key, ""))):
                entry[key] = item[key]
        documents.append(entry)
    result = {
        "schema_version": 1,
        "inventory_sha256": sha(raw),
        "documents": documents,
        "counts": dict(sorted(Counter(entry["disposition"] for entry in documents).items())),
        "completeness": {
            key: bool(inventory.get("completeness", {}).get(key, False))
            for key in ("enumeration", "capture", "review", "publication")
        },
    }
    atomic_private(destination, json_bytes(result))
    return result
