"""Versioned, offline topic decisions over lossless Markdown structural spans.

Detectors propose examination only. Publication accepts reviewed bytes, and every
removal is guarded by source identity, structural address and exact span digest.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from markdown_it import MarkdownIt

from .models import PageMetadata
from .render import normalize_body, serialize_document, split_document
from .state import StateStore
from .terraform_curation import TerraformFilter


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def canonical(url: str) -> str:
    parsed = urlsplit(html.unescape(unquote(url)))
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def normalized(text: str) -> str:
    return re.sub(r"\\([_\-*\[\]()])", r"\1", html.unescape(unquote(text))).casefold()


@dataclass(frozen=True)
class Block:
    start: int
    end: int
    kind: str
    location: str
    text: str

    @property
    def sha256(self) -> str:
        return digest(self.text.encode())


# Structural addresses and source spans must be computed together.
# pylint: disable-next=too-many-locals
def blocks(body: str) -> list[Block]:
    """Lossless spans, including nested list items and reference definitions.

    CommonMark owns block boundaries; source lines own bytes. Ancestor/child
    overlaps are retained for review but cannot both be removed in a transaction.
    Duplicate headings have separate occurrence addresses.
    """
    lines = body.splitlines(keepends=True)
    tokens = MarkdownIt("commonmark").enable("table").parse(body)
    result: list[Block] = []
    headings: list[tuple[int, str]] = []
    occurrences: dict[str, int] = {}
    units = {
        "paragraph_open",
        "heading_open",
        "list_item_open",
        "table_open",
        "fence",
        "code_block",
        "html_block",
        "hr",
    }
    for token in tokens:
        if token.type not in units or token.map is None:
            continue
        start, end = token.map
        kind = token.type.removesuffix("_open")
        if kind == "heading":
            level = int(token.tag[1:])
            while headings and headings[-1][0] >= level:
                headings.pop()
            name = lines[start].strip()
            occurrences[name] = occurrences.get(name, 0) + 1
            headings.append((level, f"{name}@{occurrences[name]}"))
        section = "/".join(name for _, name in headings)
        address = f"{section}:{kind}:{start}"
        result.append(Block(start, end, kind, address, "".join(lines[start:end])))
    for block in list(result):
        if block.kind == "table":
            for i in range(block.start + 2, block.end):
                result.append(Block(i, i + 1, "table_row", block.location + f":row:{i}", lines[i]))
    for index, block in enumerate(list(result)):
        if block.kind != "heading":
            continue
        level = len(block.text.lstrip().split(" ", 1)[0])
        end = len(lines)
        for following in result[index + 1 :]:
            if (
                following.kind == "heading"
                and len(following.text.lstrip().split(" ", 1)[0]) <= level
            ):
                end = following.start
                break
        result.append(
            Block(
                block.start,
                end,
                "section",
                block.location.replace(":heading:", ":section:"),
                "".join(lines[block.start : end]),
            )
        )
    covered = {
        line
        for block in result
        if block.kind != "section"
        for line in range(block.start, block.end)
    }
    for i, line in enumerate(lines):
        if i not in covered and re.match(r"^ {0,3}\[[^\]]+\]:", line):
            end = i + 1
            while end < len(lines) and lines[end].startswith(("    ", "\t")):
                end += 1
            result.append(Block(i, end, "reference", f"reference:{i}", "".join(lines[i:end])))
    return sorted(result, key=lambda item: (item.start, -item.end, item.kind))


def remove_blocks(body: str, rules: list[dict[str, Any]]) -> str:
    parsed = blocks(body)
    spans: list[Block] = []
    for rule in rules:
        matches = [
            b for b in parsed if b.location == rule["location"] and b.sha256 == rule["sha256"]
        ]
        if len(matches) != 1:
            raise ValueError("stale or ambiguous structural guard")
        spans.append(matches[0])
    spans.sort(key=lambda b: (b.start, b.end))
    if any(a.end > b.start for a, b in zip(spans, spans[1:], strict=False)):
        raise ValueError("overlapping structural guards")
    lines = body.splitlines(keepends=True)
    discarded = {line for b in spans for line in range(b.start, b.end)}
    body = "".join(line for i, line in enumerate(lines) if i not in discarded)
    # Delete empty enclosing headings, separators and orphan figure captions only
    # when the caption itself was included in a reviewed span.
    while True:
        parsed = blocks(body)
        lines = body.splitlines(keepends=True)
        empty: set[int] = set()
        for i, block in enumerate(parsed):
            if block.kind != "heading":
                continue
            level = len(block.text.lstrip().split(" ", 1)[0])
            end = len(lines)
            for following in parsed[i + 1 :]:
                if (
                    following.kind == "heading"
                    and len(following.text.lstrip().split(" ", 1)[0]) <= level
                ):
                    end = following.start
                    break
            if not any(
                b.kind not in {"heading", "hr", "section"} and b.text.strip()
                for b in parsed
                if block.end <= b.start < end
            ):
                empty.update(range(block.start, end))
        if not empty:
            break
        body = "".join(line for i, line in enumerate(lines) if i not in empty)
    return normalize_body(body)


class CurationPolicy:
    def __init__(self, raw: dict[str, Any], sha256: str) -> None:
        if raw.get("schema_version") != 1 or not isinstance(raw.get("topics"), list):
            raise ValueError("unsupported curation registry")
        self.raw, self.sha256 = raw, sha256
        self.topics = raw["topics"]
        ids = [topic["id"] for topic in self.topics]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate topic identity")
        self.excluded: set[str] = set()
        self.retired_identities: set[str] = set()
        self.retired_patterns: list[re.Pattern[str]] = []
        self.detectors: list[re.Pattern[str]] = []
        self.decisions: dict[str, list[dict[str, Any]]] = {}
        self.media: dict[str, dict[str, Any]] = {}
        self.terraform: TerraformFilter | None = None
        for topic in self.topics:
            if topic.get("mode") == "automatic":
                if self.terraform is not None or topic["id"] != "terraform-provider-current":
                    raise ValueError("unsupported automatic curation topic")
                self.terraform = TerraformFilter(
                    topic,
                    Path(__file__).parent,
                    blocks=blocks,
                    digest=digest,
                    remove_blocks=remove_blocks,
                )
            self.retired_identities.update(topic["retired_identities"])
            self.excluded.update(canonical(url) for url in topic["retired_urls"])
            self.retired_patterns.extend(
                re.compile(value, re.I) for value in topic["retired_patterns"]
            )
            self.detectors.extend(re.compile(value, re.I) for value in topic["candidate_detectors"])
            for decision in topic["documents"]:
                url = canonical(decision["url"])
                if decision["disposition"] not in {"keep", "remove", "omit"}:
                    raise ValueError("invalid reviewed disposition")
                self.decisions.setdefault(url, []).append(decision)
                if decision["disposition"] in {"remove", "omit"}:
                    self.excluded.add(url)
            for media in topic["media"]:
                if media["disposition"] not in {"keep", "remove"}:
                    raise ValueError("invalid media disposition")
                key = media["sha256"]
                if key in self.media and self.media[key] != media:
                    raise ValueError("conflicting media decisions")
                self.media[key] = media
        for decisions in self.decisions.values():
            if len({json.dumps(d, sort_keys=True) for d in decisions}) != 1:
                raise ValueError("conflicting document decisions")

    def excludes(self, url: str) -> bool:
        parsed = urlsplit(normalized(url))
        identities = {part.replace("-", "_") for part in parsed.path.split("/")}
        return (
            canonical(url) in self.excluded
            or self.retired(url)
            or bool(identities & self.retired_identities)
        )

    def retired(self, text: str) -> bool:
        decoded = normalized(text)
        return any(pattern.search(decoded) for pattern in self.retired_patterns)

    def candidate(self, text: str) -> bool:
        decoded = normalized(text)
        return self.retired(text) or any(pattern.search(decoded) for pattern in self.detectors)

    def references(self, body: str, base: str, excluded: set[str]) -> list[str]:
        # Parse actual href/src destinations, including escaped and HTML URLs.
        found: set[str] = set()
        parsed = MarkdownIt("commonmark").parse(body)
        for token in parsed:
            for child in token.children or []:
                value = child.attrGet("href") or child.attrGet("src")
                if value:
                    found.add(urljoin(base, html.unescape(unquote(str(value)))))
        found.update(
            urljoin(base, html.unescape(unquote(m.group(1))))
            for m in re.finditer(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", body, re.I)
        )
        found.update(
            urljoin(base, m.group(1))
            for m in re.finditer(r"(?m)^ {0,3}\[[^\]]+\]:\s*<?([^\s>]+)", body)
        )
        found.update(m.group() for m in re.finditer(r"https?://[^\s<>\"'`\[\]()]+", body))
        return sorted(url for url in found if self.excludes(url) or canonical(url) in excluded)


@lru_cache(maxsize=8)
def load_curation_policy(path: Path | None = None) -> CurationPolicy:
    path = path or Path(__file__).with_name("curation_policy.json")
    data = path.read_bytes()
    raw = json.loads(data)
    for topic in raw["topics"]:
        for evidence in topic["evidence_inputs"]:
            if "path" not in evidence:
                continue
            target = (path.parent / evidence["path"]).resolve()
            if (
                path.parent.resolve() not in target.parents
                or digest(target.read_bytes()) != evidence["sha256"]
            ):
                raise ValueError("curation evidence digest/path mismatch")
    return CurationPolicy(raw, digest(data))


def corpus_digest(output: Path) -> str:
    entries = [
        (path.relative_to(output).as_posix(), digest(path.read_bytes()))
        for path in sorted(output.glob("content/**/*"))
        if path.is_file()
    ]
    return digest(json_bytes(entries))


def media_inventory(path: Path, body: str) -> list[dict[str, Any]]:
    values = re.findall(r"!\[[^\]]*\]\((?:<)?([^\s)>]+)", body)
    values.extend(
        m.group(1)
        for m in re.finditer(
            r"""<(?:img|video|source|iframe)\b[^>]*\bsrc=["']([^"']+)""", body, re.I
        )
    )
    result = []
    for value in sorted(set(values)):
        target = (path.parent / value).resolve()
        local = not urlsplit(value).scheme and path.parent.resolve() in target.parents
        result.append(
            {
                "reference": value,
                "sha256": digest(target.read_bytes()) if local and target.is_file() else None,
                "kind": "local" if local else "remote",
            }
        )
    if re.search(r"<(?:iframe|video|embed)\b|(?:youtube\.com|youtu\.be|vimeo\.com)", body, re.I):
        result.append({"reference": "embedded-video", "sha256": None, "kind": "video"})
    return result


def remove_reviewed_media(body: str, reference: str) -> str:
    escaped = re.escape(reference)
    body = re.sub(r"!\[[^\]]*\]\(" + escaped + r"\)", "", body)
    body = re.sub(r"<img\b[^>]*\bsrc=[\"']" + escaped + r"[\"'][^>]*>", "", body, flags=re.I)
    return remove_blocks(body, [])


# Plan and apply the complete dependency graph as one transaction.
# pylint: disable-next=too-many-locals,too-many-branches,too-many-statements
def curate_topics(
    output: Path,
    store: StateStore | None = None,
    *,
    policy: CurationPolicy | None = None,
    apply: bool = True,
) -> dict[str, Any]:
    """Plan all pages before applying, then close dependencies to a fixed point."""
    active = policy or load_curation_policy()
    before = corpus_digest(output)
    plans: dict[str, dict[str, Any]] = {}
    excluded = set(active.excluded)
    for path in sorted(output.glob("content/*/**/index.md")):
        metadata, body = split_document(path.read_text())
        url = canonical(str(metadata.get("canonical_url") or metadata["url"]))
        candidate = active.candidate(
            body
            + json.dumps(
                {k: v for k, v in metadata.items() if k not in {"related_documents", "description"}}
            )
        )
        rules = active.decisions.get(url, [])
        decision = rules[0] if rules else None
        finding: dict[str, Any] = {
            "document": path.relative_to(output).as_posix(),
            "url": url,
            "input_sha256": digest(body.encode()),
            "candidate": candidate,
            "disposition": "keep",
            "findings": [],
            "media": media_inventory(path, body),
            "stages": [],
        }
        retained = body
        if active.excludes(url) or active.excludes(str(metadata["url"])):
            finding["disposition"] = "remove"
            finding["findings"].append(
                decision.get("reason", "retired_identity") if decision else "retired_identity"
            )
        elif candidate or decision:
            if decision is None:
                finding["disposition"] = "omit"
                finding["findings"].append("unclassified_candidate")
            elif digest(body.encode()) == decision.get("output_sha256"):
                pass
            elif digest(body.encode()) != decision["input_sha256"]:
                # A later topic may have changed the reviewed SMSv2 output.
                # Revalidate the SMSv2 invariants instead of treating a whole
                # document hash mismatch as evidence that its old blocks remain.
                if (
                    decision["disposition"] != "keep"
                    or active.retired(body)
                    or active.references(body, url, set())
                ):
                    finding["disposition"] = "omit"
                    finding["findings"].append("changed_document_input")
                else:
                    finding["findings"].append("reviewed_topic_output_revalidated")
            else:
                try:
                    retained = remove_blocks(body, decision.get("block_removals", []))
                    if digest(retained.encode()) != decision["output_sha256"]:
                        raise ValueError("reviewed output digest mismatch")
                except ValueError as error:
                    finding["disposition"] = "omit"
                    finding["findings"].append(str(error))
            # Any media on a candidate page requires committed pixel-bound review.
            for media in (
                media_inventory(path, retained)
                if not decision or decision.get("media_review_required", True)
                else []
            ):
                review = active.media.get(media["sha256"] or "")
                if review is None or review["disposition"] != "keep":
                    finding["disposition"] = "omit"
                    finding["findings"].append("unreviewed_changed_or_removed_media")
        finding["stages"].append(
            {
                "topic": "smsv2-current",
                "input_sha256": digest(body.encode()),
                "output_sha256": digest(retained.encode())
                if finding["disposition"] == "keep"
                else None,
            }
        )
        if finding["disposition"] == "keep" and active.terraform is not None:
            terraform_before = digest(retained.encode())
            result = active.terraform.transform(retained, url)
            retained = result.body
            finding["terraform"] = {
                "removals": result.findings,
                "destinations": result.destinations,
                "removed_media": list(result.removed_media),
            }
            if result.findings and not result.omit:
                for media in media_inventory(path, retained):
                    review = active.media.get(media["sha256"] or "")
                    if review is not None and review["disposition"] == "remove":
                        retained = remove_reviewed_media(retained, media["reference"])
                        finding["terraform"]["removed_media"].append(media["reference"])
                        finding["terraform"]["removals"].append(
                            {"reason": "reviewed_media_removed", "sha256": media["sha256"]}
                        )
            finding["stages"].append(
                {
                    "topic": "terraform-provider-current",
                    "input_sha256": terraform_before,
                    "output_sha256": digest(retained.encode()) if not result.omit else None,
                }
            )
            if result.omit:
                finding["disposition"] = "omit"
                finding["findings"].append("no_independent_content_after_terraform_curation")
            elif result.findings:
                for media in media_inventory(path, retained):
                    review = active.media.get(media["sha256"] or "")
                    if (
                        media["kind"] != "local"
                        or review is None
                        or review["disposition"] != "keep"
                    ):
                        finding["disposition"] = "omit"
                        finding["findings"].append("unreviewed_terraform_media")
                        break
        if any(item["kind"] == "video" for item in media_inventory(path, retained)):
            finding["disposition"] = "omit"
            finding["findings"].append("unverifiable_video")
        if finding["disposition"] == "keep" and active.retired(retained):
            finding["disposition"] = "omit"
            finding["findings"].append("retired_subject_remaining")
        if not any(
            b.kind not in {"heading", "hr", "section"} and b.text.strip() for b in blocks(retained)
        ):
            finding["disposition"] = "omit"
            finding["findings"].append("empty_document")
        finding["output_sha256"] = (
            digest(retained.encode()) if finding["disposition"] == "keep" else None
        )
        source_key = str(metadata["url"])
        plans[source_key] = {
            "path": path,
            "metadata": metadata,
            "body": retained,
            "finding": finding,
        }
        if finding["disposition"] != "keep":
            excluded.add(url)
            excluded.add(canonical(str(metadata["url"])))
    changed = True
    while changed:
        changed = False
        for _, plan in plans.items():
            url = plan["finding"]["url"]
            finding = plan["finding"]
            if finding["disposition"] != "keep":
                continue
            dependencies = active.references(plan["body"], url, excluded)
            local_targets: set[Path] = set()
            for token in MarkdownIt("commonmark").parse(plan["body"]):
                for child in token.children or []:
                    href = child.attrGet("href")
                    if isinstance(href, str) and not urlsplit(href).scheme:
                        destination = unquote(urlsplit(href).path)
                        if destination and (
                            destination.endswith(".md") or destination.startswith("content/")
                        ):
                            root = (
                                output
                                if destination.startswith("content/")
                                else plan["path"].parent
                            )
                            local_targets.add((root / destination).resolve())
            for other in plans.values():
                if (
                    other["finding"]["disposition"] != "keep"
                    and other["path"].resolve() in local_targets
                ):
                    dependencies.append(other["finding"]["url"])
            if dependencies:
                finding["disposition"] = "omit"
                finding["findings"].append({"unresolved_dependencies": sorted(set(dependencies))})
                excluded.add(url)
                excluded.add(canonical(str(plan["metadata"]["url"])))
                changed = True
    referenced_assets = {
        (plan["path"].parent / match.group()).resolve()
        for plan in plans.values()
        if plan["finding"]["disposition"] == "keep"
        for match in re.finditer(r"assets/[a-zA-Z0-9_.-]+", plan["body"])
    }
    removed_assets = [
        path.relative_to(output).as_posix()
        for path in sorted(output.glob("content/*/**/assets/*"))
        if path.resolve() not in referenced_assets
    ]
    if apply:
        for plan in plans.values():
            path, finding = plan["path"], plan["finding"]
            if finding["disposition"] != "keep":
                path.unlink()
                if store:
                    with store.connection:
                        store.connection.execute(
                            "DELETE FROM pages WHERE canonical_url=?", (plan["metadata"]["url"],)
                        )
                continue
            retained_metadata = PageMetadata.model_validate(plan["metadata"])
            if active.terraform is not None and finding.get("terraform", {}).get("removals"):
                retained_metadata.title = active.terraform.topic.get("title_overrides", {}).get(
                    finding["url"], retained_metadata.title
                )
            retained_metadata.related_documents = [
                item
                for item in retained_metadata.related_documents
                if canonical(item.canonical_url) not in excluded
            ]
            # Provenance/classification fields are checked later after enrichment.
            retained_metadata.tags = [
                item for item in retained_metadata.tags if not active.retired(item)
            ]
            retained_metadata.aliases = [
                item for item in retained_metadata.aliases if not active.retired(item)
            ]
            retained_metadata.description = None
            retained_metadata.replacement_url = None
            document = serialize_document(retained_metadata, plan["body"])
            path.write_text(document, encoding="utf-8", newline="\n")
        prune_assets(output)
    planned = {
        p["path"].relative_to(output).as_posix(): {"body": p["body"], "metadata": p["metadata"]}
        for p in plans.values()
        if p["finding"]["disposition"] == "keep"
    }
    return {
        "schema_version": 1,
        "policy_sha256": active.sha256,
        "input_sha256": before,
        "output_sha256": corpus_digest(output) if apply else None,
        "documents": [p["finding"] for p in plans.values()],
        "counts": {
            key: sum(p["finding"]["disposition"] == key for p in plans.values())
            for key in ("keep", "remove", "omit")
        },
        "removed_assets": removed_assets,
        "_planned": planned,
    }


def prune_assets(output: Path) -> None:
    referenced: set[Path] = set()
    for path in output.glob("content/*/**/index.md"):
        _, body = split_document(path.read_text())
        for match in re.finditer(r"assets/[a-zA-Z0-9_.-]+", body):
            referenced.add((path.parent / match.group()).resolve())
    for path in sorted(output.glob("content/*/**/assets/*")):
        if path.resolve() not in referenced:
            path.unlink()


def validate_curation(
    output: Path, *, artifacts: bool = False, policy: CurationPolicy | None = None
) -> None:
    policy = policy or load_curation_policy()
    for path in sorted(output.glob("content/*/**/index.md")):
        metadata, body = split_document(path.read_text())
        if policy.terraform is not None:
            result = policy.terraform.transform(body, str(metadata["url"]))
            if result.body != body or result.omit:
                raise ValueError(f"uncurated Terraform provider content remains: {path}")
        if (
            policy.excludes(str(metadata["url"]))
            or policy.retired(body)
            or policy.retired(json.dumps(metadata))
        ):
            raise ValueError(f"prohibited topic remains: {path}")
        url = canonical(str(metadata.get("canonical_url") or metadata["url"]))
        media = media_inventory(path, body)
        if (
            policy.terraform is not None
            and (
                str(metadata["url"]) in policy.terraform.reviewed_urls
                or policy.terraform.catalog["landing"] in body
                or any(value in normalized(body) for value in policy.terraform.qualified_links)
            )
            and any(
                item["kind"] != "local"
                or policy.media.get(item["sha256"] or "", {}).get("disposition") != "keep"
                for item in media
            )
        ):
            raise ValueError(f"unreviewed Terraform media remains: {path}")
        if any(item["kind"] == "video" for item in media):
            raise ValueError(f"unverifiable video remains: {path}")
        required = any(d.get("media_review_required", True) for d in policy.decisions.get(url, []))
        if (required or policy.candidate(body)) and any(
            policy.media.get(item["sha256"] or "", {}).get("disposition") != "keep"
            for item in media
        ):
            raise ValueError(f"unreviewed media remains: {path}")
        if policy.references(body, str(metadata["url"]), set()):
            raise ValueError(f"retired reference remains: {path}")
    for name in ("quality-report.json", "quality-report.md", "manifest.json") if artifacts else ():
        path = output / name
        if path.is_file() and (
            policy.retired(path.read_text())
            or re.search(
                r'"(?:content_migration|exclusions|affected_documents|missing_benchmark_urls)"\s*:',
                path.read_text(),
            )
        ):
            raise ValueError(f"curation audit leakage: {name}")
