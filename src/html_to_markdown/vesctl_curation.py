"""Whole-document retirement and digest-bound independent-link repairs."""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt

from .render import normalize_body


def decode_text(text: str) -> str:
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
    return text


def decoded(text: str) -> str:
    return decode_text(text).casefold()


@lru_cache(maxsize=4096)
def destinations(body: str) -> frozenset[str]:
    """Authored destinations before redirects, including unused definitions."""
    body = decode_text(body)
    found: set[str] = set()
    for token in MarkdownIt("commonmark").parse(body):
        for child in token.children or []:
            value = child.attrGet("href") or child.attrGet("src")
            if value:
                found.add(str(value))
    if re.search(r"<(?:a|img|video|source|iframe)\b", body, re.I):
        for node in BeautifulSoup(body, "html.parser").select("[href], [src]"):
            for key in ("href", "src"):
                if node.get(key):
                    found.add(str(node[key]))
    found.update(m[1] for m in re.finditer(r"(?m)^ {0,3}\[[^\]]+\]:\s*<?([^\s>]+)", body))
    found.update(m[0] for m in re.finditer(r"https?://[^\s<>\"'`\[\]()]+", body))
    return frozenset(found)


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
        self.aliases = {
            self.canonical(alias): url
            for url, item in self.decisions.items()
            for alias in item.get("aliases", [])
        }
        self.excluded.update(alias for alias, url in self.aliases.items() if url in self.excluded)

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

    def original_html_match(self, metadata: dict[str, Any], body: str, source_html: str) -> bool:
        # Head metadata can be discarded by adapter extraction. Navigation is
        # outside authored content and must not retire unrelated pages.
        soup = BeautifulSoup(source_html, "html.parser")
        authored = " ".join(str(node.get("content") or "") for node in soup.select("meta"))
        return self.original_match(metadata, body) or self.retired(authored)

    @staticmethod
    def source_digest(metadata: dict[str, Any], body: str) -> str:
        authored = {
            key: metadata.get(key)
            for key in ("title", "tags", "aliases", "category", "subcategory")
        }
        return hashlib.sha256(
            json.dumps(authored, sort_keys=True, ensure_ascii=False).encode()
            + b"\n"
            + body.encode()
        ).hexdigest()

    def excludes(self, url: str) -> bool:
        return self.retired(url) or self.canonical(url) in self.excluded

    # Review outcomes remain explicit so every failed guard omits the input.
    # pylint: disable-next=too-many-return-statements
    def transform(
        self,
        body: str,
        url: str,
        *,
        dependent: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> VesctlResult:
        if self.retired(body) or self.excludes(url):
            return VesctlResult(body, [{"reason": "whole_document_reference"}], omit=True)
        key = self.canonical(url)
        decision = self.decisions.get(self.aliases.get(key, key))
        current = hashlib.sha256(body.encode()).hexdigest()
        if decision and current == decision["output_sha256"] and not dependent:
            return VesctlResult(body, [{"reason": "reviewed_current_output"}])
        if decision and current == decision["input_sha256"]:
            if decision.get("source_sha256") and (
                metadata is None or self.source_digest(metadata, body) != decision["source_sha256"]
            ):
                return VesctlResult(body, [{"reason": "changed_authored_review_input"}], omit=True)
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
        if dependent or (decision and decision.get("source_sha256")):
            return VesctlResult(body, [{"reason": "unreviewed_changed_dependency"}], omit=True)
        return VesctlResult(body, [])
