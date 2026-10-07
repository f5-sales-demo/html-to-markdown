"""Authored retirement classifications and reviewed independent current sections."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .render import normalize_body
from .vesctl_curation import decoded


@dataclass(frozen=True)
class LegacyResult:
    body: str
    reasons: list[str]
    disposition: str = "keep"


class LegacyFilter:
    def __init__(self, topic: dict[str, Any], *, remove_blocks: Any) -> None:
        self.topic = topic
        self.remove_blocks = remove_blocks
        self.decisions = {self.canonical(item["url"]): item for item in topic["documents"]}
        if len(self.decisions) != len(topic["documents"]):
            raise ValueError("duplicate legacy review URL")
        self.excluded = {self.canonical(url) for url in topic["retired_urls"]}
        self.excluded.update(
            url for url, item in self.decisions.items() if item["disposition"] != "keep"
        )
        self.patterns = tuple(re.compile(p, re.I) for p in topic["retired_patterns"])
        self.scope = tuple(re.compile(p, re.I) for p in topic["scope_patterns"])
        self.media = {item["sha256"]: item for item in topic["media"]}

    @staticmethod
    def canonical(url: str) -> str:
        parsed = urlsplit(decoded(url))
        return parsed._replace(path=parsed.path.rstrip("/"), query="", fragment="").geturl()

    def retired(self, text: str) -> bool:
        value = decoded(text)
        return any(pattern.search(value) for pattern in self.patterns)

    def excludes(self, url: str) -> bool:
        return self.canonical(url) in self.excluded or self.retired(url)

    @staticmethod
    def authored(metadata: dict[str, Any]) -> dict[str, Any]:
        # Exclude relationship, hash and freshness projections, preserve source
        # descriptions, titles, tags and explicit lifecycle classification.
        return {
            k: metadata.get(k)
            for k in (
                "title",
                "description",
                "tags",
                "aliases",
                "lifecycle",
                "category",
                "subcategory",
            )
        }

    @classmethod
    def source_digest(cls, metadata: dict[str, Any], body: str) -> str:
        data = json.dumps(cls.authored(metadata), sort_keys=True, ensure_ascii=False).encode()
        return hashlib.sha256(data + b"\n" + body.encode()).hexdigest()

    def whole(self, metadata: dict[str, Any], body: str) -> bool:
        title = decoded(str(metadata.get("title", "")))
        headings = re.findall(r"(?m)^#{1,6}\s+(.+)$", body)
        title += " " + " ".join(decoded(h) for h in headings[:1])
        # Legacy applications and platform requirements are not a retirement
        # classification. An explicit page label remains authoritative.
        label = re.search(
            r"(?:^legacy$|\(legacy\)|\[legacy\]|(?:^| )legacy (?:ce|customer edge|secure mesh|site|workflow|deployment|api|f5))",
            title,
        )
        tags = metadata.get("tags") or []
        classified = any(
            re.fullmatch(
                r"legacy|retired|deprecated|legacy[- _](?:ce|deployment|workflow|f5)",
                decoded(str(tag)),
            )
            for tag in tags
        )
        lifecycle = str(metadata.get("lifecycle", "")) in {"legacy", "retired", "deprecated"}
        text = BeautifulSoup(body, "html.parser").get_text(" ", strip=True) if "<" in body else body
        scope_text = decoded(title + " " + str(metadata.get("description") or "") + " " + text)
        return bool(
            label or classified or lifecycle or any(p.search(scope_text) for p in self.scope)
        )

    def original_whole(self, metadata: dict[str, Any], html: str, source_html: str) -> bool:
        # Head metadata is outside the content adapter's selected container.
        soup = BeautifulSoup(source_html, "html.parser")
        meta = dict(metadata)
        for node in soup.select("meta[name], meta[property]"):
            name = str(node.get("name") or node.get("property") or "").casefold()
            value = str(node.get("content") or "")
            if name in {"keywords", "tags", "article:tag"}:
                meta["tags"] = list(meta.get("tags") or []) + re.split(r"[,;]", value)
            elif name in {"lifecycle", "status"}:
                meta["lifecycle"] = value.casefold()
        return self.whole(meta, html)

    # Guard outcomes remain explicit so review failures always omit.
    # pylint: disable-next=too-many-return-statements
    def transform(
        self, metadata: dict[str, Any], body: str, *, dependent: bool = False
    ) -> LegacyResult:
        url = str(metadata.get("canonical_url") or metadata["url"])
        if self.whole(metadata, body) or self.excludes(url) or self.excludes(str(metadata["url"])):
            return LegacyResult(body, ["authored_legacy_page_classification"], "remove")
        affected = self.retired(body) or any(
            self.retired(str(v)) for v in self.authored(metadata).values()
        )
        decision = self.decisions.get(self.canonical(str(metadata["url"]))) or self.decisions.get(
            self.canonical(url)
        )
        current = hashlib.sha256(body.encode()).hexdigest()
        if decision and decision["disposition"] == "keep":
            if (
                current in decision.get("accepted_output_sha256", [decision["output_sha256"]])
                and not affected
                and not dependent
            ):
                return LegacyResult(body, ["reviewed_current_output"])
            if (
                current == decision["input_sha256"]
                and self.source_digest(metadata, body) == decision["source_sha256"]
            ):
                try:
                    retained = self.remove_blocks(body, decision.get("block_removals", []))
                    for replacement in decision.get("replacements", []):
                        if retained.count(replacement["source"]) != replacement["count"]:
                            raise ValueError("stale legacy replacement guard")
                        retained = retained.replace(
                            replacement["source"], replacement["destination"]
                        )
                    retained = normalize_body(retained)
                    if hashlib.sha256(retained.encode()).hexdigest() != decision[
                        "output_sha256"
                    ] or self.retired(retained):
                        raise ValueError("stale legacy output guard")
                    return LegacyResult(retained, ["reviewed_independent_current_sections"])
                except ValueError:
                    return LegacyResult(body, ["stale_legacy_section_guard"], "omit")
            return LegacyResult(body, ["changed_reviewed_legacy_input"], "omit")
        if affected or dependent:
            return LegacyResult(body, ["unreviewed_legacy_content_or_dependency"], "omit")
        return LegacyResult(body, [])
