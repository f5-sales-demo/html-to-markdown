"""Reproducible full-document inventories and candidate evidence, never deletion rules."""

import hashlib
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt

from ..curation import blocks, corpus_digest, json_bytes, media_inventory
from ..render import split_document


def sha(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def links(body: str) -> list[str]:
    result: list[str] = []
    for token in MarkdownIt("commonmark").enable("table").parse(body):
        for child in token.children or []:
            if child.type == "link_open":
                result.append(str(child.attrGet("href")))
    result.extend(re.findall(r'https?://[^\s<>"\)]+', body))
    return sorted(set(result))


def inventory(path: Path, root: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    metadata, body = split_document(raw)
    parsed = blocks(body)
    spans = []
    for block in parsed:
        spans.append(
            {
                "address": block.location,
                "start_line": block.start,
                "end_line": block.end,
                "kind": block.kind,
                "sha256": block.sha256,
                "text": block.text,
                "estimated_tokens": (len(block.text) + 3) // 4,
                "classification": "uncertain",
            }
        )
    inline = []
    parser = MarkdownIt("commonmark").enable("table")
    for token in parser.parse(body):
        for child in token.children or []:
            if child.type in {"image", "link_open", "code_inline"}:
                inline.append(
                    {
                        "kind": child.type,
                        "lines": token.map,
                        "value": child.content,
                        "attributes": child.attrs,
                    }
                )
    findings = []
    candidates = {
        "body_date": r"(?im)^Published .*(?:modified|updated).*|^Last (?:updated|modified).*",
        "renderer_label": r"(?im)^Terminal window\s*$",
        "api_appendix": r"<!-- content-policy:related-api -->",
        "duplicate_caption": r"(?im)^Figure \d+[:.].*$",
    }
    for category, pattern in candidates.items():
        for match in re.finditer(pattern, body):
            findings.append(
                {
                    "category": category,
                    "start": match.start(),
                    "end": match.end(),
                    "source": match.group(),
                    "estimated_tokens": (len(match.group()) + 3) // 4,
                }
            )
    if len(body.split()) < 80:
        findings.append({"category": "short_document_review", "start": 0, "end": len(body)})
    description = str(metadata.get("description") or "")
    if description and description[-1] not in ".!?":
        findings.append({"category": "possibly_clipped_description", "field": "description"})
    return {
        "document": path.relative_to(root).as_posix(),
        "input_sha256": sha(raw),
        "body_sha256": sha(body),
        "metadata": metadata,
        "body": body,
        "estimated_tokens": (len(body) + 3) // 4,
        "blocks": spans,
        "inline": inline,
        "links": links(body),
        "media": media_inventory(path, body),
        "findings": findings,
    }


def analyze(root: Path) -> dict[str, Any]:
    documents = [inventory(path, root) for path in sorted(root.glob("content/*/**/index.md"))]
    passages: dict[str, list[dict[str, Any]]] = defaultdict(list)
    near: dict[str, list[dict[str, Any]]] = defaultdict(list)
    canonical: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for doc in documents:
        canonical[str(doc["metadata"].get("canonical_url") or doc["metadata"]["url"])].append(doc)
        for block in doc["blocks"]:
            if block["kind"] not in {"paragraph", "fence", "table", "reference"}:
                continue
            occurrence = {
                "document": doc["document"],
                "address": block["address"],
                "start_line": block["start_line"],
                "end_line": block["end_line"],
                "sha256": block["sha256"],
            }
            passages[block["sha256"]].append(occurrence)
            # Normalized exact matches are explicit near-duplicate candidates.
            # Never normalize identifiers or digits, and never collapse articles.
            normalized = re.sub(r"\s+", " ", block["text"]).strip().casefold()
            if len(normalized) >= 80:
                near[sha(normalized)].append(occurrence)
    exact_groups = [
        {"sha256": key, "occurrences": values}
        for key, values in sorted(passages.items())
        if len({v["document"] for v in values}) >= 3
    ]
    near_groups = [
        {"normalized_sha256": key, "occurrences": values, "uncertain": True}
        for key, values in sorted(near.items())
        if len({v["document"] for v in values}) >= 2 and len({v["sha256"] for v in values}) > 1
    ]
    by_source: dict[str, dict[str, int]] = defaultdict(
        lambda: {"documents": 0, "estimated_tokens": 0}
    )
    for doc in documents:
        source = str(doc["metadata"]["sourceId"])
        by_source[source]["documents"] += 1
        by_source[source]["estimated_tokens"] += doc["estimated_tokens"]
    return {
        "schema_version": 1,
        "corpus_sha256": corpus_digest(root),
        "documents": documents,
        "sources": dict(sorted(by_source.items())),
        "exact_repetition": exact_groups,
        "near_repetition": near_groups,
        "canonical_candidates": [
            {
                "canonical_url": url,
                "documents": [d["document"] for d in docs],
                "substantive_match": len({d["body_sha256"] for d in docs}) == 1,
            }
            for url, docs in sorted(canonical.items())
            if len(docs) > 1
        ],
        "estimate_method": "ceil(characters/4); advisory, not billed usage",
    }


def write_analysis(root: Path, destination: Path) -> dict[str, Any]:
    report = analyze(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(json_bytes(report))
    lines = [
        "# Corpus analysis",
        "",
        f"Documents: {len(report['documents'])}.",
        "",
        "Findings are semantic-review candidates, not automatic removal rules.",
        "",
        "| Source | Documents | Estimated tokens |",
        "| --- | ---: | ---: |",
    ]
    lines.extend(
        f"| {source} | {counts['documents']} | {counts['estimated_tokens']} |"
        for source, counts in report["sources"].items()
    )
    lines += [
        "",
        f"Exact repeated-passage groups: {len(report['exact_repetition'])}.",
        f"Normalized near-duplicate groups: {len(report['near_repetition'])}.",
        "",
    ]
    for doc in report["documents"]:
        lines += [
            f"- {doc['document']}: {len(doc['blocks'])} structural spans, "
            f"{len(doc['media'])} media references, {len(doc['findings'])} findings."
        ]
    destination.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
