"""Closed immutable payload addresses, including original receipt-bound legacy paths."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .capture_store import capture_digest
from .errors import AllowlistError
from .urls import SOURCE_ROOTS, stable_path, validate_source_url


def _legacy_path(item: dict[str, Any], relative: Path, field: str) -> bool:
    source, url = item.get("sourceId"), item.get("url")
    if source not in SOURCE_ROOTS or not isinstance(url, str):
        return False
    try:
        canonical = validate_source_url(source, url)
    except AllowlistError:
        return False
    route = stable_path(source, canonical)
    if item.get("identity") != source + ":" + route.as_posix():
        return False
    if field == "source_html":
        return relative == Path("captures") / (hashlib.sha256(url.encode()).hexdigest() + ".html")
    return field == "article" and relative == Path("articles/content") / source / route / "index.md"


def capture_payloads(item: dict[str, Any]) -> dict[str, str]:
    """Validate addresses without changing original records or trusting arbitrary paths."""
    pairs = [
        (item[field + "_path"], item[field + "_sha256"], field)
        for field in ("source_html", "article")
        if item.get(field + "_path") and item.get(field + "_sha256")
    ]
    pairs.extend((media["path"], media["sha256"], "media") for media in item.get("media", []))
    result: dict[str, str] = {}
    for name, expected, field in pairs:
        if not isinstance(name, str) or not isinstance(expected, str):
            raise ValueError("invalid capture payload address")
        relative = Path(name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "\\" in name
            or relative.as_posix() != name
            or re.fullmatch(r"[0-9a-f]{64}", expected) is None
        ):
            raise ValueError("unsafe capture storage payload path or digest")
        if capture_digest(relative) != expected and not _legacy_path(item, relative, field):
            raise ValueError("capture record does not address an immutable payload path")
        if name in result and result[name] != expected:
            raise ValueError("conflicting capture payload digests")
        result[name] = expected
    return result
