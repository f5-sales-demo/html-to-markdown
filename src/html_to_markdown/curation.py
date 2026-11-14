# Replay imports are deferred until after module initialization to preserve the policy boundary.
# pylint: disable=cyclic-import
"""Versioned, offline topic decisions over lossless Markdown structural spans.

Detectors propose examination only. Publication accepts reviewed bytes, and every
removal is guarded by source identity, structural address and exact span digest.
"""

# Lossless structural review and transaction validation share one planner.
# pylint: disable=too-many-lines

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

from .appstack_curation import AppStackFilter
from .curation_api_chain import legacy_result as replay_legacy_stage
from .curation_media import plan_masks, relocate_references
from .legacy_curation import LegacyFilter
from .models import PageMetadata
from .render import normalize_body, serialize_document, split_document
from .state import StateStore
from .terraform_curation import TerraformFilter
from .urls import infer_source, stable_path
from .vesctl_curation import VesctlFilter, VesctlResult, destinations


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


def reference_spans(body: str) -> list[dict[str, Any]]:
    """Exact direct Markdown links outside code, addressed within smallest source blocks."""
    parsed = blocks(body)
    lines = body.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    code = [(offsets[b.start], offsets[b.end]) for b in parsed if b.kind in {"fence", "code_block"}]
    code.extend((m.start(), m.end()) for m in re.finditer(r"(`+)(.*?)\1", body, re.S))
    found = []
    pattern = r"(?<!!)(?<!\\)\[([^\]\[\n]+)\]\((https?://[^\s()<>]+)\)"
    for index, match in enumerate(re.finditer(pattern, body)):
        if any(a <= match.start() < b or a < match.end() <= b for a, b in code):
            continue
        candidates = [
            b
            for b in parsed
            if b.kind in {"paragraph", "list_item", "table_row"}
            and offsets[b.start] <= match.start()
            and match.end() <= offsets[b.end]
        ]
        if not candidates:
            continue
        block = min(candidates, key=lambda b: (b.end - b.start, b.kind != "paragraph", b.location))
        found.append(
            {
                "address": block.location + f"/reference:{index}",
                "block_address": block.location,
                "block_sha256": block.sha256,
                "source_sha256": digest(match.group().encode()),
                "url": match[2],
                "label": match[1],
                "start": match.start(),
                "end": match.end(),
                "block_start": offsets[block.start],
                "block_end": offsets[block.end],
            }
        )
    return found


def unlink_references(body: str, selected: list[dict[str, Any]]) -> str:
    """Source markup guards preserve every label and all surrounding bytes."""
    for item in sorted(selected, key=lambda value: value["start"], reverse=True):
        source = body[item["start"] : item["end"]]
        if digest(source.encode()) != item["source_sha256"]:
            raise ValueError("retired reference source guard changed")
        body = body[: item["start"]] + item["label"] + body[item["end"] :]
    return body


def _guarded_reference_edits(body: str, block: Block, wanted: list[str]) -> list[dict[str, Any]]:
    if not wanted or len(wanted) != len(set(wanted)):
        raise ValueError("invalid retired reference guards")
    parsed = {item.location: item for item in blocks(body)}
    available = reference_spans(body)
    result = []
    for pin in wanted:
        selected = [
            item
            for item in available
            if item["source_sha256"] == pin
            and block.start <= parsed[item["block_address"]].start
            and parsed[item["block_address"]].end <= block.end
        ]
        if len(selected) != 1:
            raise ValueError("stale or ambiguous retired reference guard")
        result.append(selected[0])
    return result


def _empty_heading_spans(parsed: list[Block], line_count: int) -> list[tuple[int, int]]:
    empty = []
    for i, block in enumerate(parsed):
        if block.kind != "heading":
            continue
        level = len(block.text.lstrip().split(" ", 1)[0])
        end = next(
            (
                b.start
                for b in parsed[i + 1 :]
                if b.kind == "heading" and len(b.text.lstrip().split(" ", 1)[0]) <= level
            ),
            line_count,
        )
        if not any(
            b.kind not in {"heading", "hr", "section"} and b.text.strip()
            for b in parsed
            if block.end <= b.start < end
        ):
            empty.append((block.start, end))
    return empty


def remove_blocks(body: str, rules: list[dict[str, Any]]) -> str:
    parsed = blocks(body)
    if any("prose_span" in rule for rule in rules):
        from .curation_spans import apply_prose_removals  # pylint: disable=import-outside-toplevel

        body, rules = apply_prose_removals(body, rules, parsed, blocks)
        parsed = blocks(body)
    spans: list[Block] = []
    reference_edits = []
    for rule in rules:
        matches = [
            b for b in parsed if b.location == rule["location"] and b.sha256 == rule["sha256"]
        ]
        if len(matches) != 1:
            raise ValueError("stale or ambiguous structural guard")
        if "unlink_reference_sha256" in rule:
            reference_edits.extend(
                _guarded_reference_edits(body, matches[0], rule["unlink_reference_sha256"])
            )
        else:
            spans.append(matches[0])
    if any(
        block.start
        <= next(b.start for b in parsed if b.location == item["block_address"])
        < block.end
        for block in spans
        for item in reference_edits
    ):
        raise ValueError("overlapping block removal and retired reference edit")
    if len({item["address"] for item in reference_edits}) != len(reference_edits):
        raise ValueError("overlapping retired reference edits")
    body = unlink_references(body, reference_edits)
    if not spans:
        return normalize_body(body)
    spans.sort(key=lambda b: (b.start, b.end))
    if any(a.end > b.start for a, b in zip(spans, spans[1:], strict=False)):
        raise ValueError("overlapping structural guards")
    lines = body.splitlines(keepends=True)
    discarded = {line for b in spans for line in range(b.start, b.end)}
    body = "".join(line for i, line in enumerate(lines) if i not in discarded)
    protected = {start for start, _ in _empty_heading_spans(parsed, len(lines))}
    origins = [i for i in range(len(lines)) if i not in discarded]
    # Keep authored blank fields and their enclosing context. Only newly empty
    # sections without a surviving original blank field are cleanup candidates.
    while True:
        lines = body.splitlines(keepends=True)
        empty = {
            line
            for start, end in _empty_heading_spans(blocks(body), len(lines))
            if not any(origins[i] in protected for i in range(start, end))
            for line in range(start, end)
        }
        if not empty:
            break
        body = "".join(line for i, line in enumerate(lines) if i not in empty)
        origins = [origin for i, origin in enumerate(origins) if i not in empty]
    return normalize_body(body)


@dataclass(frozen=True)
class SMSPolicy:
    detectors: tuple[re.Pattern[str], ...]
    patterns: tuple[re.Pattern[str], ...]
    decisions: dict[str, dict[str, Any]]
    media: dict[str, dict[str, Any]]

    @classmethod
    def from_topic(cls, topic: dict[str, Any] | None) -> SMSPolicy:
        if topic is None:
            return cls((), (), {}, {})
        return cls(
            tuple(re.compile(value, re.I) for value in topic["candidate_detectors"]),
            tuple(re.compile(value, re.I) for value in topic["retired_patterns"]),
            {canonical(item["url"]): item for item in topic["documents"]},
            {item["sha256"]: item for item in topic["media"]},
        )


# The registry owns one optional engine for each independent versioned topic.
# pylint: disable-next=too-many-instance-attributes
class CurationPolicy:
    # pylint: disable-next=too-many-branches
    def __init__(self, raw: dict[str, Any], sha256: str) -> None:
        if raw.get("schema_version") != 1 or not isinstance(raw.get("topics"), list):
            raise ValueError("unsupported curation registry")
        self.raw, self.sha256 = raw, sha256
        ids = [topic["id"] for topic in self.topics]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate topic identity")
        self.excluded: set[str] = set()
        self.retired_identities: set[str] = set()
        self.retired_patterns: list[re.Pattern[str]] = []
        self.media: dict[str, dict[str, Any]] = {}
        self.terraform: TerraformFilter | None = None
        self.appstack: AppStackFilter | None = None
        self.vesctl: VesctlFilter | None = None
        self.legacy: LegacyFilter | None = None
        sms_topic = next(
            (topic for topic in self.topics if topic["id"] == "smsv2-current"),
            next((topic for topic in self.topics if topic.get("mode") != "automatic"), None),
        )
        self.sms = SMSPolicy.from_topic(sms_topic)
        for topic in self.topics:
            if topic.get("mode") == "automatic":
                if topic["id"] == "terraform-provider-current" and self.terraform is None:
                    self.terraform = TerraformFilter(
                        topic,
                        Path(__file__).parent,
                        blocks=blocks,
                        digest=digest,
                        remove_blocks=remove_blocks,
                    )
                elif topic["id"] == "legacy-retired" and self.legacy is None:
                    self.legacy = LegacyFilter(topic, remove_blocks=remove_blocks)
                elif topic["id"] == "appstack-retired" and self.appstack is None:
                    self.appstack = AppStackFilter(
                        topic, blocks=blocks, digest=digest, remove_blocks=remove_blocks
                    )
                else:
                    self._load_whole_document_topic(topic)
            self.retired_identities.update(topic["retired_identities"])
            self.excluded.update(canonical(url) for url in topic["retired_urls"])
            if topic["id"] != "appstack-retired" or self.appstack is None:
                self.retired_patterns.extend(
                    re.compile(value, re.I) for value in topic["retired_patterns"]
                )
            topic_decisions: dict[str, dict[str, Any]] = {}
            for decision in topic["documents"]:
                url = canonical(decision["url"])
                if url in topic_decisions and topic_decisions[url] != decision:
                    raise ValueError("conflicting document decisions")
                topic_decisions[url] = decision
                if decision["disposition"] not in {"keep", "remove", "omit"}:
                    raise ValueError("invalid reviewed disposition")
                if decision["disposition"] in {"remove", "omit"}:
                    self.excluded.add(url)
            topic_media: dict[str, dict[str, Any]] = {}
            for media in topic["media"]:
                if media["disposition"] not in {"keep", "remove"}:
                    raise ValueError("invalid media disposition")
                if topic["id"] in {"appstack-retired", "vesctl-retired", "legacy-retired"}:
                    # The same bytes can be a generic icon in one page and
                    # obsolete instructional media in another. Review scope
                    # belongs to the topic and document, not a global digest.
                    continue
                key = media["sha256"]
                if key in topic_media and topic_media[key] != media:
                    previous = topic_media[key]
                    previous_scope = set(
                        previous.get("documents", previous.get("source_documents", []))
                    )
                    scope = set(media.get("documents", media.get("source_documents", [])))
                    if previous["disposition"] != media["disposition"] and (
                        not previous_scope or not scope or previous_scope & scope
                    ):
                        raise ValueError("conflicting media decisions")
                topic_media.setdefault(key, media)
                self.media.setdefault(key, media)

    def media_review(
        self, topic_id: str, media_sha256: str, url: str, path: Path
    ) -> dict[str, Any]:
        """A reused image digest does not grant a new document-placement approval."""
        topic = next((item for item in self.topics if item["id"] == topic_id), None)
        if topic is None and topic_id == "smsv2-current":
            topic = next((item for item in self.topics if item.get("mode") != "automatic"), None)
        if topic is None:
            return {}
        matches = []
        for review in topic["media"]:
            if review["sha256"] != media_sha256:
                continue
            if "documents" in review:
                eligible = canonical(url) in {canonical(value) for value in review["documents"]}
            elif "source_documents" in review:
                eligible = any(
                    path.as_posix().endswith("/content/" + value.removeprefix("content/"))
                    for value in review["source_documents"]
                )
            else:
                # Historical global retirement evidence remains an exclusion.
                eligible = review.get("disposition") == "remove"
            if eligible:
                matches.append(review)
        if len({item["disposition"] for item in matches}) > 1:
            raise ValueError("conflicting document-scoped media decisions")
        return matches[0] if matches else {}

    def _load_whole_document_topic(self, topic: dict[str, Any]) -> None:
        if topic["id"] != "vesctl-retired" or self.vesctl is not None:
            raise ValueError("unsupported automatic curation topic")
        self.vesctl = VesctlFilter(topic)

    @property
    def topics(self) -> list[dict[str, Any]]:
        return list(self.raw["topics"])

    def excludes(self, url: str) -> bool:
        parsed = urlsplit(normalized(url))
        identities = {part.replace("-", "_") for part in parsed.path.split("/")}
        return (
            canonical(url) in self.excluded
            or (self.vesctl is not None and self.vesctl.excludes(url))
            or (self.legacy is not None and self.legacy.excludes(url))
            or self.retired(url)
            or bool(identities & self.retired_identities)
        )

    def retired(self, text: str, *, url: str | None = None) -> bool:
        decoded = normalized(text)
        appstack_retired = self.appstack is not None and (
            self.appstack.unreviewed_retired(text, url)
            if url is not None
            else self.appstack.retired(text)
        )
        return bool(
            appstack_retired
            or (self.vesctl is not None and self.vesctl.retired(text))
            or (self.legacy is not None and self.legacy.retired(text))
        ) or any(pattern.search(decoded) for pattern in self.retired_patterns)

    def candidate(self, text: str) -> bool:
        decoded = normalized(text)
        return self.retired(text) or any(
            re.search(value, decoded, re.I)
            for topic in self.topics
            for value in topic["candidate_detectors"]
        )

    def sms_retired(self, text: str) -> bool:
        decoded = normalized(text)
        return any(pattern.search(decoded) for pattern in self.sms.patterns)

    def sms_candidate(self, text: str) -> bool:
        decoded = normalized(text)
        return self.sms_retired(text) or any(
            pattern.search(decoded) for pattern in self.sms.detectors
        )

    def references(
        self,
        body: str,
        base: str,
        excluded: set[str],
        *,
        predicate: Any = None,
    ) -> list[str]:
        found = {urljoin(base, value) for value in destinations(body)}
        check = predicate or self.excludes
        return sorted(url for url in found if check(url) or canonical(url) in excluded)


def load_curation_policy(path: Path | None = None) -> CurationPolicy:
    if path is None:
        from .policy_activation import active_policy_path  # pylint: disable=import-outside-toplevel

        path = active_policy_path() or Path(__file__).with_name("curation_policy.json")
    return _load_curation_policy(path)


@lru_cache(maxsize=8)
def _load_curation_policy(path: Path) -> CurationPolicy:
    data = path.read_bytes()
    raw = json.loads(data)
    linked = bytearray(data)
    for topic in raw["topics"]:
        if catalog := topic.get("review_catalog"):
            target = (path.parent / catalog["path"]).resolve()
            if path.parent.resolve() not in target.parents:
                raise ValueError("curation review catalog escapes policy directory")
            payload = target.read_bytes()
            if digest(payload) != catalog["sha256"]:
                raise ValueError("curation review catalog digest mismatch")
            review = json.loads(payload)
            if review.get("schema_version") != 1 or review.get("topic") != topic["id"]:
                raise ValueError("invalid curation review catalog")
            topic["documents"] = review["documents"]
            topic["media"] = review["media"]
            linked.extend(payload)
        for evidence in topic["evidence_inputs"]:
            if "path" not in evidence:
                continue
            target = (path.parent / evidence["path"]).resolve()
            if (
                path.parent.resolve() not in target.parents
                or digest(target.read_bytes()) != evidence["sha256"]
            ):
                raise ValueError("curation evidence digest/path mismatch")
    return CurationPolicy(raw, digest(bytes(linked)))


def corpus_digest(output: Path) -> str:
    entries = [
        (path.relative_to(output).as_posix(), digest(path.read_bytes()))
        for path in sorted(output.glob("content/**/*"))
        if path.is_file()
    ]
    return digest(json_bytes(entries))


def media_inventory(
    path: Path, body: str, virtual: dict[Path, bytes] | None = None
) -> list[dict[str, Any]]:
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
                "sha256": digest(virtual[target])
                if local and virtual is not None and target in virtual
                else (digest(target.read_bytes()) if local and target.is_file() else None),
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


def remove_dependent_references(
    body: str, base_url: str, excluded: set[str], policy: CurationPolicy
) -> tuple[str, list[str]]:
    """Remove complete link-bearing Markdown units before omitting a mixed page."""
    if policy.appstack is None:
        return body, []
    parsed = blocks(body)
    candidates: list[tuple[Block, list[str]]] = []
    for block in parsed:
        if block.kind not in {
            "list_item",
            "table_row",
            "paragraph",
            "fence",
            "code_block",
            "html_block",
            "reference",
        }:
            continue
        dependencies = policy.references(
            block.text, base_url, excluded, predicate=policy.appstack.excludes
        )
        if dependencies:
            candidates.append((block, dependencies))
    priority = {
        "list_item": 0,
        "table_row": 1,
        "paragraph": 2,
        "fence": 2,
        "code_block": 2,
        "html_block": 2,
        "reference": 2,
    }
    selected: list[Block] = []
    removed: set[str] = set()
    for block, dependencies in sorted(
        candidates, key=lambda item: (priority[item[0].kind], item[0].start)
    ):
        if any(block.start < other.end and other.start < block.end for other in selected):
            continue
        selected.append(block)
        removed.update(dependencies)
    if not selected:
        return body, []
    rules = [{"location": block.location, "sha256": block.sha256} for block in selected]
    return remove_blocks(body, rules), sorted(removed)


def _reviewed_prior_topics(metadata: dict[str, Any], body: str, policy: CurationPolicy) -> str:
    """Replay only exact reviewed earlier stages before checking a later stage guard."""
    current = body
    url = canonical(str(metadata["url"]))
    for topic in policy.topics:
        if topic["id"] == "vesctl-retired":
            break
        decision = next((d for d in topic["documents"] if canonical(d["url"]) == url), None)
        if decision is None:
            continue
        if decision["disposition"] != "keep":
            return body
        if digest(current.encode()) in decision.get(
            "accepted_output_sha256", [decision["output_sha256"]]
        ):
            continue
        if digest(current.encode()) != decision["input_sha256"]:
            return body
        if (
            topic["id"] == "legacy-retired"
            and policy.legacy is not None
            and (policy.legacy.source_digest(metadata, current) != decision.get("source_sha256"))
        ):
            return body
        try:
            revised = remove_blocks(current, decision.get("block_removals", []))
            revised = relocate_references(revised, decision.get("media_references", []))
        except ValueError:
            return body
        if digest(revised.encode()) != decision["output_sha256"]:
            return body
        current = revised
    return current


# Authored identities, local paths and state evidence form one closure transaction.
# pylint: disable-next=too-many-locals,too-many-branches
def retirement_preflight(
    output: Path,
    originals: list[tuple[Path, dict[str, Any], str]],
    policy: CurationPolicy,
    store: StateStore | None = None,
) -> tuple[set[Path], dict[Path, VesctlResult], set[str]]:
    """Close authored dependencies before any topic can erase their evidence."""
    engine = policy.vesctl
    if engine is None:
        return set(), {}, set()
    excluded = set(engine.excluded)
    if store is not None:
        tables = {
            row[0]
            for row in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "content_exclusions" in tables:
            excluded.update(
                canonical(row[0])
                for row in store.connection.execute(
                    "SELECT url FROM content_exclusions WHERE origin='whole_document_retirement'"
                )
            )
    matches: set[Path] = set()
    results: dict[Path, VesctlResult] = {}
    identities: dict[Path, set[str]] = {}
    for path, metadata, body in originals:
        identities[path] = {
            canonical(str(metadata["url"])),
            canonical(str(metadata.get("canonical_url") or metadata["url"])),
        }
        if engine.original_match(metadata, body) or any(
            policy.media_review(
                "vesctl-retired", item["sha256"] or "", str(metadata["url"]), path
            ).get("disposition")
            == "remove"
            for item in media_inventory(path, body)
        ):
            matches.add(path)
        prior = _reviewed_prior_topics(metadata, body, policy)
        result = engine.transform(prior, str(metadata["url"]), metadata=metadata)
        if path in matches:
            result = VesctlResult(body, [{"reason": "original_document_reference"}], omit=True)
        decision = engine.decisions.get(engine.canonical(str(metadata["url"])))
        if decision and "reviewed_media" in decision and not result.omit:
            reviewed = {item["reference"]: item["sha256"] for item in decision["reviewed_media"]}
            if any(
                item["sha256"] != reviewed.get(item["reference"])
                for item in media_inventory(path, result.body)
            ):
                result = VesctlResult(body, [{"reason": "changed_reviewed_media"}], omit=True)
        results[path] = result
        if result.omit:
            excluded.update(identities[path])
    changed = True
    while changed:
        changed = False
        omitted_paths = {path.resolve() for path, result in results.items() if result.omit}
        # Persist local identities even when a target was never captured.
        for url in excluded:
            source = infer_source(url)
            if source:
                omitted_paths.add(
                    (output / "content" / source / stable_path(source, url) / "index.md").resolve()
                )
        for path, metadata, body in originals:
            if results[path].omit:
                continue
            dependencies = policy.references(
                results[path].body, str(metadata["url"]), excluded, predicate=engine.excludes
            )
            source_review = engine.decisions.get(engine.canonical(str(metadata["url"])))
            if (
                store is not None
                and results[path].body == body
                and not (source_review and digest(body.encode()) == source_review["output_sha256"])
            ):
                dependencies.extend(
                    value
                    for value in store.candidate_links(str(metadata["url"]))
                    if engine.excludes(value) or canonical(value) in excluded
                )
            for value in destinations(results[path].body):
                parsed = urlsplit(value)
                if parsed.scheme or not parsed.path:
                    continue
                destination = parsed.path
                root = output if destination.startswith("content/") else path.parent
                target = (root / destination).resolve()
                if target in omitted_paths or target / "index.md" in omitted_paths:
                    dependencies.append(value)
            if dependencies:
                # A successful review is already applied above. Any remaining
                # link belongs to an unreviewed or newly retired dependency.
                results[path] = VesctlResult(
                    body,
                    [
                        {
                            "reason": "unresolved_authored_dependencies",
                            "urls": sorted(set(dependencies)),
                        }
                    ],
                    omit=True,
                )
                excluded.update(identities[path])
                changed = True
    return matches, results, excluded


# Plan and apply the complete dependency graph as one transaction.
# pylint: disable-next=too-many-locals,too-many-branches,too-many-statements
def curate_topics(
    output: Path,
    store: StateStore | None = None,
    *,
    policy: CurationPolicy | None = None,
    apply: bool = True,
    api_policy: Any = None,
) -> dict[str, Any]:
    """Plan all pages before applying, then close dependencies to a fixed point."""
    active = policy or load_curation_policy()
    before = corpus_digest(output)
    plans: dict[str, dict[str, Any]] = {}
    retirement_excluded = set(active.vesctl.excluded) if active.vesctl is not None else set()
    excluded = set() if active.appstack is not None else set(active.excluded)
    originals = [
        (path, *split_document(path.read_text()))
        for path in sorted(output.glob("content/*/**/index.md"))
    ]
    # Re-evaluate retirement on the preserved prior stage, then replay API migration.
    from .curation_api_chain import api_source  # pylint: disable=import-outside-toplevel

    api_replayed: set[Path] = set()
    for index, (path, metadata, body) in enumerate(originals):
        prior_stage = api_source(active, path, metadata, body, api_policy)
        if prior_stage is not None:
            originals[index] = (path, metadata, prior_stage[1])
            api_replayed.add(path)
    virtual_media = plan_masks(active.topics, originals)

    def inventory(path: Path, body: str) -> list[dict[str, Any]]:
        return media_inventory(path, body, virtual_media)

    original_matches, retirement_plans, retirement_excluded = retirement_preflight(
        output, originals, active, store
    )
    legacy_plans = {}
    legacy_excluded = set(active.legacy.excluded) if active.legacy is not None else set()
    if active.legacy is not None:
        for path, metadata, body in originals:
            legacy_media_affected = any(
                active.media_review(
                    "legacy-retired", item["sha256"] or "", str(metadata["url"]), path
                ).get("disposition")
                == "remove"
                for item in inventory(path, body)
            )
            legacy_preflight = active.legacy.transform(
                metadata, body, dependent=legacy_media_affected
            )
            if legacy_preflight.disposition != "keep":
                legacy_preflight = replay_legacy_stage(
                    active, path, metadata, body, dependent=legacy_media_affected
                )
            legacy_plans[path] = legacy_preflight
            if legacy_preflight.disposition != "keep":
                legacy_excluded.update(
                    {
                        canonical(str(metadata["url"])),
                        canonical(str(metadata.get("canonical_url") or metadata["url"])),
                    }
                )
        # Close the original dependency graph before later topics can erase
        # incoming evidence, including local Markdown destinations.
        dependency_changed = True
        while dependency_changed:
            dependency_changed = False
            omitted_paths = {
                path.resolve()
                for path, result in legacy_plans.items()
                if result.disposition != "keep"
            }
            for path, metadata, body in originals:
                if legacy_plans[path].disposition != "keep":
                    continue
                dependencies = active.references(
                    body, str(metadata["url"]), legacy_excluded, predicate=active.legacy.excludes
                )
                local_dependency = False
                for token in MarkdownIt("commonmark").parse(body):
                    for child in token.children or []:
                        href = child.attrGet("href")
                        if isinstance(href, str) and not urlsplit(href).scheme:
                            destination = unquote(urlsplit(href).path)
                            root = output if destination.startswith("content/") else path.parent
                            if destination and (root / destination).resolve() in omitted_paths:
                                local_dependency = True
                if dependencies or local_dependency:
                    legacy_dependency_result = active.legacy.transform(
                        metadata, body, dependent=True
                    )
                    legacy_plans[path] = legacy_dependency_result
                    if legacy_dependency_result.disposition != "keep":
                        legacy_excluded.update(
                            {
                                canonical(str(metadata["url"])),
                                canonical(str(metadata.get("canonical_url") or metadata["url"])),
                            }
                        )
                        dependency_changed = True
    excluded.update(legacy_excluded)
    excluded.update(retirement_excluded)
    for path, metadata, body in originals:
        url = canonical(str(metadata.get("canonical_url") or metadata["url"]))
        candidate = active.sms_candidate(
            body
            + json.dumps(
                {k: v for k, v in metadata.items() if k not in {"related_documents", "description"}}
            )
        )
        decision = active.sms.decisions.get(url)
        finding: dict[str, Any] = {
            "document": path.relative_to(output).as_posix(),
            "url": url,
            "input_sha256": digest(body.encode()),
            "candidate": candidate,
            "disposition": "keep",
            "findings": [],
            "media": inventory(path, body),
            "stages": [],
        }
        retained = body
        if path in legacy_plans:
            legacy_result = legacy_plans[path]
            retained = legacy_result.body
            finding["legacy"] = legacy_result.reasons
            finding["disposition"] = legacy_result.disposition
            finding["stages"].append(
                {
                    "topic": "legacy-retired",
                    "input_sha256": digest(body.encode()),
                    "original_sha256": active.legacy.source_digest(metadata, body)
                    if active.legacy
                    else None,
                    "output_sha256": digest(retained.encode())
                    if legacy_result.disposition == "keep"
                    else None,
                }
            )
            if (
                active.legacy
                and finding["disposition"] == "keep"
                and any(
                    active.media_review(
                        "legacy-retired", item["sha256"] or "", str(metadata["url"]), path
                    ).get("disposition")
                    == "remove"
                    for item in inventory(path, retained)
                )
            ):
                finding["disposition"] = "omit"
                finding["legacy"].append("retired_media_remains")
            if retained != body:
                metadata = dict(metadata, description=None)
            candidate = active.sms_candidate(
                retained
                + json.dumps(
                    {
                        k: v
                        for k, v in metadata.items()
                        if k not in {"related_documents", "description"}
                    }
                )
            )
        original_match = path in original_matches
        original_dependencies = (
            active.references(body, url, retirement_excluded, predicate=active.vesctl.excludes)
            if active.vesctl is not None
            else []
        )
        if original_match or active.excludes(url) or active.excludes(str(metadata["url"])):
            finding["disposition"] = "remove"
            finding["findings"].append(
                decision.get("reason", "retired_identity") if decision else "retired_identity"
            )
        elif path in retirement_plans and retirement_plans[path].omit:
            finding["disposition"] = "omit"
            finding["findings"].append("unreviewed_authored_retirement_dependency")
        elif finding["disposition"] == "keep" and (candidate or decision):
            if decision is None:
                finding["disposition"] = "omit"
                finding["findings"].append("unclassified_candidate")
            elif digest(retained.encode()) == decision.get("output_sha256"):
                pass
            elif digest(retained.encode()) != decision["input_sha256"]:
                # A later topic may have changed the reviewed SMSv2 output.
                # Revalidate the SMSv2 invariants instead of treating a whole
                # document hash mismatch as evidence that its old blocks remain.
                if decision["disposition"] != "keep" or active.sms_retired(retained):
                    finding["disposition"] = "omit"
                    finding["findings"].append("changed_document_input")
                else:
                    finding["findings"].append("reviewed_topic_output_revalidated")
            else:
                try:
                    retained = remove_blocks(retained, decision.get("block_removals", []))
                    if digest(retained.encode()) != decision["output_sha256"]:
                        raise ValueError("reviewed output digest mismatch")
                except ValueError as error:
                    finding["disposition"] = "omit"
                    finding["findings"].append(str(error))
            # Any media on a candidate page requires committed pixel-bound review.
            for media in (
                inventory(path, retained)
                if not decision or decision.get("media_review_required", True)
                else []
            ):
                review = active.media_review("smsv2-current", media["sha256"] or "", url, path)
                if review is None or review.get("disposition") != "keep":
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
        if finding["disposition"] == "keep" and active.appstack is not None:
            appstack_before = digest(retained.encode())
            result = active.appstack.transform(retained, url)
            retained = result.body
            finding["appstack"] = result.findings
            if result.omit:
                finding["disposition"] = "omit"
                finding["findings"].append("appstack_retirement")
            elif result.findings:
                for media in inventory(path, retained):
                    review = active.media_review(
                        "appstack-retired", media["sha256"] or "", url, path
                    )
                    if (
                        media["kind"] != "local"
                        or review is None
                        or review.get("disposition") != "keep"
                        or url not in review.get("documents", [])
                    ):
                        finding["disposition"] = "omit"
                        finding["findings"].append("unreviewed_appstack_media")
                        break
            finding["stages"].append(
                {
                    "topic": "appstack-retired",
                    "input_sha256": appstack_before,
                    "output_sha256": digest(retained.encode())
                    if finding["disposition"] == "keep"
                    else None,
                }
            )
        if active.vesctl is not None:
            vesctl_before = digest(retained.encode())
            if original_match or active.vesctl.excludes(url):
                finding["vesctl"] = [{"reason": "original_document_reference"}]
            elif retirement_plans[path].omit:
                finding["vesctl"] = retirement_plans[path].findings
            elif finding["disposition"] == "keep":
                dependencies = active.references(
                    retained, url, active.vesctl.excluded, predicate=active.vesctl.excludes
                )
                vesctl_result = active.vesctl.transform(
                    retained,
                    str(metadata["url"]),
                    dependent=bool(dependencies or original_dependencies),
                    metadata=metadata,
                )
                retained = vesctl_result.body
                finding["vesctl"] = vesctl_result.findings
                if vesctl_result.omit:
                    finding["disposition"] = "omit"
                    finding["findings"].append("unseparable_retirement_dependency")
            finding["stages"].append(
                {
                    "topic": "vesctl-retired",
                    "input_sha256": vesctl_before,
                    "original_sha256": digest(json_bytes(metadata) + body.encode()),
                    "output_sha256": digest(retained.encode())
                    if finding["disposition"] == "keep"
                    else None,
                }
            )
        if finding["disposition"] == "keep" and active.terraform is not None:
            terraform_before = digest(retained.encode())
            terraform_result = active.terraform.transform(retained, url)
            retained = terraform_result.body
            finding["terraform"] = {
                "removals": terraform_result.findings,
                "destinations": terraform_result.destinations,
                "removed_media": list(terraform_result.removed_media),
            }
            if terraform_result.findings and not terraform_result.omit:
                for media in inventory(path, retained):
                    review = active.media_review(
                        "terraform-provider-current", media["sha256"] or "", url, path
                    )
                    if review is not None and review.get("disposition") == "remove":
                        retained = remove_reviewed_media(retained, media["reference"])
                        finding["terraform"]["removed_media"].append(media["reference"])
                        finding["terraform"]["removals"].append(
                            {"reason": "reviewed_media_removed", "sha256": media["sha256"]}
                        )
            finding["stages"].append(
                {
                    "topic": "terraform-provider-current",
                    "input_sha256": terraform_before,
                    "output_sha256": digest(retained.encode())
                    if not terraform_result.omit
                    else None,
                }
            )
            if terraform_result.omit:
                finding["disposition"] = "omit"
                finding["findings"].append("no_independent_content_after_terraform_curation")
            elif terraform_result.findings:
                for media in inventory(path, retained):
                    review = active.media_review(
                        "terraform-provider-current", media["sha256"] or "", url, path
                    )
                    if (
                        media["kind"] != "local"
                        or review is None
                        or review.get("disposition") != "keep"
                    ):
                        finding["disposition"] = "omit"
                        finding["findings"].append("unreviewed_terraform_media")
                        break
        if any(item["kind"] == "video" for item in inventory(path, retained)):
            finding["disposition"] = "omit"
            finding["findings"].append("unverifiable_video")
        if finding["disposition"] == "keep" and active.retired(retained, url=url):
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
        relationships_raw = metadata.get("related_documents", [])
        relationships = relationships_raw if isinstance(relationships_raw, list) else []
        finding["removed_relationships"] = [
            item
            for item in relationships
            if active.vesctl is not None and active.vesctl.excludes(item["canonical_url"])
        ]
        if retained != body:
            metadata = dict(metadata, description=None)
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
            if original_match or finding.get("vesctl"):
                retirement_excluded.update({url, canonical(str(metadata["url"]))})
    changed = True
    while changed:
        changed = False
        for _, plan in plans.items():
            url = plan["finding"]["url"]
            finding = plan["finding"]
            if finding["disposition"] != "keep":
                continue
            dependencies = active.references(
                plan["body"],
                url,
                excluded,
                predicate=active.appstack.excludes if active.appstack else None,
            )
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
            retirement_dependencies = (
                active.references(
                    plan["body"], url, retirement_excluded, predicate=active.vesctl.excludes
                )
                if active.vesctl is not None
                else []
            )
            if active.vesctl is not None:
                retirement_dependencies.extend(
                    other["finding"]["url"]
                    for other in plans.values()
                    if other["path"].resolve() in local_targets
                    and other["finding"]["url"] in retirement_excluded
                )
            legacy_dependencies = (
                active.references(
                    plan["body"], url, legacy_excluded, predicate=active.legacy.excludes
                )
                if active.legacy
                else []
            )
            if (
                dependencies
                and active.appstack is not None
                and not retirement_dependencies
                and not legacy_dependencies
            ):
                before_dependencies = digest(plan["body"].encode())
                repaired, removed = remove_dependent_references(plan["body"], url, excluded, active)
                remaining = active.references(
                    repaired, url, excluded, predicate=active.appstack.excludes
                )
                meaningful = any(
                    b.kind not in {"heading", "hr", "section"} and b.text.strip()
                    for b in blocks(repaired)
                )
                if removed and not remaining and meaningful:
                    plan["body"] = repaired
                    plan["metadata"] = dict(plan["metadata"], description=None)
                    finding["appstack_dependencies"] = removed
                    finding["stages"].append(
                        {
                            "topic": "appstack-reference-closure",
                            "input_sha256": before_dependencies,
                            "output_sha256": digest(repaired.encode()),
                        }
                    )
                    finding["output_sha256"] = digest(repaired.encode())
                    changed = True
                    continue
            if dependencies:
                finding["disposition"] = "omit"
                finding["findings"].append({"unresolved_dependencies": sorted(set(dependencies))})
                excluded.add(url)
                excluded.add(canonical(str(plan["metadata"]["url"])))
                if retirement_dependencies:
                    retirement_excluded.update({url, canonical(str(plan["metadata"]["url"]))})
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
        for asset_destination, data in virtual_media.items():
            if asset_destination in referenced_assets:
                asset_destination.write_bytes(data)
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
            retained_metadata.replacement_url = None
            document = serialize_document(retained_metadata, plan["body"])
            path.write_text(document, encoding="utf-8", newline="\n")
        prune_assets(output)
    planned = {}
    for plan in plans.values():
        if plan["finding"]["disposition"] != "keep":
            continue
        planned_body = plan["body"]
        if not apply and plan["path"] in api_replayed:
            from .content_policy import (  # pylint: disable=import-outside-toplevel
                load_content_policy,
                project_api_body,
            )

            planned_body, _, _ = project_api_body(
                planned_body,
                plan["metadata"].get("canonical_url") or plan["metadata"]["url"],
                api_policy or load_content_policy(),
                active,
            )
            planned_body = normalize_body(planned_body)
        planned[plan["path"].relative_to(output).as_posix()] = {
            "body": planned_body,
            "metadata": plan["metadata"],
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


def validate_whole_document_retirement(
    policy: CurationPolicy,
    path: Path,
    metadata: dict[str, Any],
    body: str,
    *,
    api_policy: Any = None,
) -> None:
    if policy.legacy is not None:
        legacy_media_affected = any(
            policy.media_review(
                "legacy-retired", item["sha256"] or "", str(metadata["url"]), path
            ).get("disposition")
            == "remove"
            for item in media_inventory(path, body)
        )
        upstream = next(
            (parent for parent in path.parents if (parent / "enrichment-state.json").is_file()),
            None,
        )
        original_metadata, original_body = metadata, body
        checked_path = path
        if upstream is not None and path.relative_to(upstream).parts[0] == "content":
            original = upstream / ".enrichment" / "curated" / path.relative_to(upstream)
            original_metadata, original_body = split_document(original.read_text())
            checked_path = original
        result = policy.legacy.transform(
            original_metadata, original_body, dependent=legacy_media_affected
        )
        if result.disposition != "keep":
            result = replay_legacy_stage(
                policy,
                checked_path,
                original_metadata,
                original_body,
                dependent=legacy_media_affected,
                api_policy=api_policy,
            )
        if result.disposition != "keep" or result.body != original_body:
            raise ValueError(f"retired legacy content remains: {path}")
    if policy.vesctl is not None:
        if policy.vesctl.original_match(metadata, body) or any(
            policy.media_review(
                "vesctl-retired", item["sha256"] or "", str(metadata["url"]), path
            ).get("disposition")
            == "remove"
            for item in media_inventory(path, body)
        ):
            raise ValueError(f"retired whole document remains: {path}")
        dependencies = policy.references(
            body,
            str(metadata["url"]),
            policy.vesctl.excluded,
            predicate=policy.vesctl.excludes,
        )
        if dependencies:
            raise ValueError(f"retired document dependency remains: {path}")


# pylint: disable-next=too-many-branches,too-many-locals
def _validate_curated_article(
    path: Path, policy: CurationPolicy, inherited: bool = False, *, api_policy: Any = None
) -> None:
    metadata, body = split_document(path.read_text())
    if not inherited:
        validate_whole_document_retirement(policy, path, metadata, body, api_policy=api_policy)
    from .curation_api_chain import (  # pylint: disable=import-outside-toplevel
        api_source,
        retirement_metadata,
    )

    verified_input = api_source(policy, path, metadata, body, api_policy)
    retirement_body = verified_input[1] if verified_input is not None else body
    if policy.appstack is not None:
        result = policy.appstack.transform(retirement_body, str(metadata["url"]))
        if result.body != retirement_body or result.omit:
            raise ValueError(f"uncurated AppStack content remains: {path}")
    if policy.terraform is not None:
        terraform_result = policy.terraform.transform(retirement_body, str(metadata["url"]))
        if terraform_result.body != retirement_body or terraform_result.omit:
            raise ValueError(f"uncurated Terraform provider content remains: {path}")
    if (
        policy.excludes(str(metadata["url"]))
        or policy.retired(retirement_body, url=str(metadata["url"]))
        or policy.retired(json.dumps(retirement_metadata(policy, metadata, body, retirement_body)))
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
            or policy.media_review(
                "terraform-provider-current", item["sha256"] or "", url, path
            ).get("disposition")
            != "keep"
            for item in media
        )
    ):
        raise ValueError(f"unreviewed Terraform media remains: {path}")
    if any(item["kind"] == "video" for item in media):
        raise ValueError(f"unverifiable video remains: {path}")
    appstack = policy.appstack
    appstack_reviewed = (
        appstack is not None
        and url in appstack.decisions
        and appstack.decisions[url]["disposition"] == "keep"
    )
    if (
        appstack is not None
        and appstack_reviewed
        and any(
            item["kind"] != "local"
            or policy.media_review("appstack-retired", item["sha256"] or "", url, path).get(
                "disposition"
            )
            != "keep"
            or url
            not in policy.media_review("appstack-retired", item["sha256"] or "", url, path).get(
                "documents", []
            )
            for item in media
        )
    ):
        raise ValueError(f"unreviewed AppStack media remains: {path}")
    required = policy.sms.decisions.get(url, {}).get("media_review_required", False)
    if (required or policy.sms_candidate(body)) and any(
        policy.media_review("smsv2-current", item["sha256"] or "", url, path).get("disposition")
        != "keep"
        for item in media
    ):
        raise ValueError(f"unreviewed media remains: {path}")
    if policy.references(
        body,
        str(metadata["url"]),
        set(),
        predicate=policy.appstack.excludes if policy.appstack else None,
    ):
        raise ValueError(f"retired reference remains: {path}")


def curation_validation_errors(
    output: Path, policy: CurationPolicy, *, api_policy: Any = None
) -> dict[str, str]:
    """Enumerate every article failure privately without weakening final validation."""
    failures = {}
    for path in sorted(output.glob("content/*/**/index.md")):
        try:
            _validate_curated_article(path, policy, api_policy=api_policy)
        except ValueError as failure:
            failures[path.relative_to(output).as_posix()] = str(failure).replace(
                str(path), "article"
            )
    return failures


# Original-chain and final-policy guards deliberately remain one audit.
# pylint: disable-next=too-many-branches,too-many-locals
def validate_curation(
    output: Path,
    *,
    artifacts: bool = False,
    policy: CurationPolicy | None = None,
    api_policy: Any = None,
) -> None:
    enriched = (output / "enrichment-state.json").is_file()
    refreshed = (output / "refresh-state.json").is_file()
    inherited: set[str] = set()
    if refreshed:
        from .refresh import inherited_documents  # pylint: disable=import-outside-toplevel

        inherited = inherited_documents(output)
    if enriched:
        from .enrichment.engine import verify_enrichment  # pylint: disable=import-outside-toplevel

        verify_enrichment(output)
    policy = policy or load_curation_policy()
    for path in sorted(output.glob("content/*/**/index.md")):
        _validate_curated_article(
            path, policy, path.relative_to(output).as_posix() in inherited, api_policy=api_policy
        )
    if artifacts and not enriched and not refreshed:
        # Final validation uses the same planner as examination and application.
        replay = curate_topics(output, policy=policy, apply=False, api_policy=api_policy)
        planned = replay.pop("_planned")
        actual = {
            path.relative_to(output).as_posix(): split_document(path.read_text())[1]
            for path in sorted(output.glob("content/*/**/index.md"))
        }
        if {key: value["body"] for key, value in planned.items()} != actual:
            raise ValueError("curation planner finds changed or omitted consumer content")
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
