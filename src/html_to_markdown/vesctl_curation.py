"""Whole-document retirement and digest-bound independent-link repairs."""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from .render import normalize_body


def decoded(text: str) -> str:
    """Decode nested URL, HTML, JSON and Markdown escapes without executing text."""
    for _ in range(8):
        previous = text
        text = html.unescape(unquote(text))
        text = re.sub(
            r"\\(?:u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2}))",
            lambda match: chr(int(match[1] or match[2], 16)),
            text,
        )
        text = re.sub(r"\\([a-zA-Z_\-*\[\]()])", r"\1", text)
        if text == previous:
            break
    return text.casefold()


@dataclass(frozen=True)
class VesctlResult:
    body: str
    findings: list[dict[str, Any]]
    omit: bool = False


class VesctlFilter:
    def __init__(self, topic: dict[str, Any]) -> None:
        self.topic = topic
        self.decisions = {self.canonical(item["url"]): item for item in topic["documents"]}
        if len(self.decisions) != len(topic["documents"]):
            raise ValueError("duplicate retirement review URL")
        self.excluded = {self.canonical(url) for url in topic["retired_urls"]}
        self.excluded.update(
            url for url, item in self.decisions.items() if item["disposition"] != "keep"
        )
        self.media = {item["sha256"]: item for item in topic["media"]}

    @staticmethod
    def canonical(url: str) -> str:
        parsed = urlsplit(decoded(url))
        return parsed._replace(path=parsed.path.rstrip("/"), query="", fragment="").geturl()

    @staticmethod
    def retired(text: str) -> bool:
        return "vesctl" in decoded(text)

    def original_match(self, metadata: dict[str, Any], body: str) -> bool:
        # Relationships and hashes are derived; source descriptions are authored
        # and must be inspected before enrichment replaces them.
        authored = {
            k: v for k, v in metadata.items() if k not in {"related_documents", "content_hash"}
        }
        return self.retired(body) or self.retired(json.dumps(authored, ensure_ascii=False))

    def excludes(self, url: str) -> bool:
        return self.retired(url) or self.canonical(url) in self.excluded

    def transform(self, body: str, url: str, *, dependent: bool = False) -> VesctlResult:
        if self.retired(body) or self.excludes(url):
            return VesctlResult(body, [{"reason": "whole_document_reference"}], omit=True)
        decision = self.decisions.get(self.canonical(url))
        current = hashlib.sha256(body.encode()).hexdigest()
        if decision and current == decision["input_sha256"]:
            retained = body
            for replacement in decision.get("replacements", []):
                old, new = replacement["source"], replacement["destination"]
                if retained.count(old) != replacement["count"]:
                    return VesctlResult(body, [{"reason": "stale_dependency_guard"}], omit=True)
                retained = retained.replace(old, new)
            retained = normalize_body(retained)
            if hashlib.sha256(retained.encode()).hexdigest() != decision["output_sha256"]:
                return VesctlResult(body, [{"reason": "stale_dependency_output"}], omit=True)
            return VesctlResult(retained, [{"reason": "reviewed_independent_content"}])
        if dependent:
            return VesctlResult(body, [{"reason": "unreviewed_changed_dependency"}], omit=True)
        return VesctlResult(body, [])
