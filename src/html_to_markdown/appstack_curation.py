"""Offline retirement of AppStack guidance with digest-bound reviewed decisions."""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit


def _normal(text: str) -> str:
    return re.sub(r"\\([_\-*\[\]()])", r"\1", html.unescape(unquote(text))).casefold()


@dataclass(frozen=True)
class AppStackResult:
    body: str
    findings: list[dict[str, Any]]
    omit: bool = False


class AppStackFilter:
    """Apply exact reviewed edits, then conservatively classify changed sources."""

    def __init__(
        self,
        topic: dict[str, Any],
        *,
        blocks: Callable[[str], list[Any]],
        digest: Callable[[bytes], str],
        remove_blocks: Callable[[str, list[dict[str, Any]]], str],
    ) -> None:
        self.topic = topic
        self._blocks = blocks
        self._digest = digest
        self._remove_blocks = remove_blocks
        self.patterns = [re.compile(p, re.I) for p in topic["retired_patterns"]]
        self.url_prefixes = tuple(url.rstrip("/").casefold() for url in topic["retired_urls"])
        self.decisions = {self.canonical(d["url"]): d for d in topic["documents"]}
        if len(self.decisions) != len(topic["documents"]):
            raise ValueError("duplicate AppStack review URL")
        self.media = {item["sha256"]: item for item in topic["media"]}
        for decision in self.decisions.values():
            if decision["disposition"] not in {"keep", "omit", "remove"}:
                raise ValueError("invalid AppStack disposition")
            for field in ("input_sha256", "output_sha256"):
                if not re.fullmatch(r"[0-9a-f]{64}", decision[field]):
                    raise ValueError("invalid AppStack review digest")

    @staticmethod
    def canonical(url: str) -> str:
        parsed = urlsplit(_normal(url))
        return parsed._replace(path=parsed.path.rstrip("/"), query="", fragment="").geturl()

    def retired(self, text: str) -> bool:
        return any(pattern.search(_normal(text)) for pattern in self.patterns)

    def excludes(self, url: str) -> bool:
        value = self.canonical(url)
        return (
            self.retired(value)
            or self.decisions.get(value, {}).get("disposition") in {"remove", "omit"}
            or any(
                value == prefix or value.startswith(prefix + "/") for prefix in self.url_prefixes
            )
        )

    def obsolete(self, text: str, base_url: str) -> bool:
        if self.retired(text):
            return True
        tokens = re.findall(r"""https?://[^\s<>\"'\`\[\]()]+""", text, re.I)
        tokens += re.findall(
            r"""(?:\]\(|\]:\s*|href\s*=\s*[\"'])\s*<?([^\s)>\"']+)""",
            text,
            re.I,
        )
        return any(self.excludes(urljoin(base_url, token.rstrip(".,;"))) for token in tokens)

    def rules_for(self, body: str, url: str) -> list[dict[str, str]]:
        """Select complete nonoverlapping CommonMark spans for review."""
        parsed = self._blocks(body)
        candidates = [
            b for b in parsed if b.kind == "section" and self.obsolete(b.text.splitlines()[0], url)
        ]
        candidates += [
            b
            for b in parsed
            if b.kind
            in {
                "list_item",
                "table_row",
                "paragraph",
                "fence",
                "code_block",
                "html_block",
                "reference",
            }
            and self.obsolete(b.text, url)
        ]
        priority = {
            "section": 0,
            "list_item": 1,
            "table_row": 2,
            "paragraph": 3,
            "fence": 3,
            "code_block": 3,
            "html_block": 3,
            "reference": 3,
        }
        selected: list[Any] = []
        for block in sorted(candidates, key=lambda b: (priority[b.kind], b.start, -b.end)):
            if any(block.start < other.end and other.start < block.end for other in selected):
                continue
            selected.append(block)
        return [
            {"location": block.location, "sha256": block.sha256}
            for block in sorted(selected, key=lambda b: b.start)
        ]

    def _reviewed(self, body: str, decision: dict[str, Any]) -> AppStackResult:
        if decision["disposition"] != "keep":
            return AppStackResult(body, [{"reason": decision["reason"]}], omit=True)
        retained = body
        for replacement in decision.get("replacements", []):
            old, new = replacement["source"], replacement["destination"]
            if retained.count(old) != replacement.get("count", 1):
                raise ValueError("stale AppStack wording guard")
            retained = retained.replace(old, new)
        retained = self._remove_blocks(retained, decision.get("block_removals", []))
        if self._digest(retained.encode()) != decision["output_sha256"]:
            raise ValueError("AppStack reviewed output digest mismatch")
        if self.retired(retained):
            raise ValueError("AppStack identity survives reviewed edit")
        return AppStackResult(
            retained,
            [{"reason": "reviewed_appstack_content", "review_index": decision["review_index"]}],
        )

    def transform(self, body: str, url: str) -> AppStackResult:
        if self.excludes(url):
            return AppStackResult(body, [{"reason": "retired_appstack_url"}], omit=True)
        decision = self.decisions.get(self.canonical(url))
        current = self._digest(body.encode())
        if decision and current == decision["input_sha256"]:
            try:
                return self._reviewed(body, decision)
            except ValueError:
                return AppStackResult(body, [{"reason": "stale_appstack_review"}], omit=True)
        if decision and decision["disposition"] == "keep" and current == decision["output_sha256"]:
            return AppStackResult(body, [])
        if not self.obsolete(body, url):
            return AppStackResult(body, [])
        try:
            rules = self.rules_for(body, url)
            retained = self._remove_blocks(body, rules)
        except ValueError:
            return AppStackResult(body, [{"reason": "ambiguous_appstack_structure"}], omit=True)
        meaningful = any(
            b.kind not in {"heading", "section", "hr"} and b.text.strip()
            for b in self._blocks(retained)
        )
        if self.obsolete(retained, url) or not meaningful:
            return AppStackResult(
                body, [{"reason": "dependent_or_unseparable_appstack_content"}], omit=True
            )
        return AppStackResult(
            retained,
            [{"reason": "automatic_appstack_block_removal", "blocks": len(rules)}],
        )
