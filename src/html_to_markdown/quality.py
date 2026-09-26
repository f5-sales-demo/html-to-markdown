"""Deterministic Markdown quality comparison and advisory reporting."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .render import split_document

RECOGNIZED_CHROME = (
    "Return to Top",
    "Show social share buttons",
    "Cookie Settings",
    "Select Service",
)
PROMOTIONAL = re.compile(
    r"(?i)\b(?:recommended content|explore our|learn more about our .{0,80}product offerings)\b"
)
PROMOTIONAL_CTA = re.compile(r"(?i)^\s*(?:[-*]\s+)?(?:\[)?learn more(?:\])?(?:\([^)]+\))?[.!]?\s*$")
STOPWORDS = {
    "about",
    "after",
    "also",
    "and",
    "are",
    "before",
    "for",
    "from",
    "have",
    "into",
    "more",
    "that",
    "the",
    "their",
    "this",
    "with",
    "your",
}


def _documents(root: Path) -> dict[str, tuple[dict[str, object], str, Path]]:
    documents: dict[str, tuple[dict[str, object], str, Path]] = {}
    for path in sorted(root.glob("content/*/**/index.md")):
        metadata, body = split_document(path.read_text(encoding="utf-8"))
        url = metadata.get("url")
        if isinstance(url, str):
            documents[url] = (metadata, body, path)
    return documents


def _tokens(text: str) -> list[str]:
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"https?://\S+|[\W_]+", " ", text.casefold())
    return [token for token in text.split() if len(token) > 2]


def _recall(candidate: str, reference: str) -> float:
    expected = Counter(_tokens(reference))
    actual = Counter(_tokens(candidate))
    total = sum(expected.values())
    return 1.0 if total == 0 else round(sum((expected & actual).values()) / total, 6)


def _structure(body: str) -> dict[str, int]:
    lines = body.splitlines()
    return {
        "headings": sum(bool(re.match(r"^#{1,6}\s+", line)) for line in lines),
        "lists": sum(bool(re.match(r"^\s*(?:[-*+] |\d+\. )", line)) for line in lines),
        "tables": sum(1 for line in lines if re.match(r"^\s*\|?.+\|.+\|?\s*$", line)) // 2,
        "code_blocks": sum(line.startswith("```") for line in lines) // 2,
        "callouts": sum(line.startswith(">") for line in lines),
        "figures": len(re.findall(r"!\[[^]]*]\([^)]+\)", body)),
        "links": len(re.findall(r"(?<!!)\[[^]]+]\([^)]+\)", body)),
        "assets": len(re.findall(r"!\[[^]]*]\((?!https?://)[^)]+\)", body)),
    }


def _headings(body: str) -> set[str]:
    return {
        re.sub(r"\s+", " ", match.group(1).strip()).casefold()
        for match in re.finditer(r"(?m)^#{1,6}\s+(.+?)\s*$", body)
    }


def _metadata_relevance(metadata: dict[str, object], body: str) -> dict[str, object]:
    significant = {token for token in _tokens(body) if token not in STOPWORDS and len(token) >= 5}
    title = set(_tokens(str(metadata.get("title", ""))))
    description_value = metadata.get("description")
    description = set(_tokens(str(description_value or "")))
    title_score = round(len(title & significant) / max(1, len(title)), 6)
    description_score = round(len(description & significant) / max(1, len(description)), 6)
    return {
        "title_term_relevance": title_score,
        "description_term_relevance": description_score if description_value else None,
        "weak_title": bool(title) and title_score < 0.5,
        "weak_description": bool(description_value) and description_score < 0.3,
    }


def _benchmark_urls(path: Path | None) -> set[str] | None:
    if path is None:
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    urls = value.get("urls", []) if isinstance(value, dict) else []
    if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
        raise ValueError("benchmark urls must be a list of strings")
    return set(urls)


def analyze_quality(
    candidate: Path, reference: Path | None = None, benchmark: Path | None = None
) -> dict[str, Any]:
    candidate_documents = _documents(candidate)
    reference_documents = _documents(reference) if reference else {}
    selected = _benchmark_urls(benchmark)
    urls = sorted(url for url in candidate_documents if selected is None or url in selected)
    missing_benchmark_urls = sorted((selected or set()) - set(candidate_documents))
    block_documents: dict[str, set[str]] = defaultdict(set)
    pages: list[dict[str, object]] = []
    promotional_count = 0
    regressed = 0
    for url in urls:
        metadata, body, path = candidate_documents[url]
        structure = _structure(body)
        normalized_lines = {line.strip().casefold() for line in body.splitlines()}
        chrome = [
            fragment for fragment in RECOGNIZED_CHROME if fragment.casefold() in normalized_lines
        ]
        promotions = sum(
            bool(PROMOTIONAL.search(line) or PROMOTIONAL_CTA.match(line))
            for line in body.splitlines()
        )
        promotional_count += promotions
        for block in re.split(r"\n\s*\n", body):
            normalized = " ".join(_tokens(block))
            if len(normalized.split()) >= 8 and not block.lstrip().startswith("#"):
                block_documents[normalized].add(url)
        page: dict[str, object] = {
            "url": url,
            "path": path.relative_to(candidate).as_posix(),
            **structure,
            "recognized_chrome": chrome,
            "promotional_fragments": promotions,
            "metadata_relevance": _metadata_relevance(metadata, body),
        }
        reference_item = reference_documents.get(url)
        if reference_item:
            reference_body = reference_item[1]
            expected = _structure(reference_body)
            recall = _recall(body, reference_body)
            page["relevant_text_recall"] = recall
            lost_counts: dict[str, int] = {}
            for key in (
                "lists",
                "tables",
                "code_blocks",
                "callouts",
                "figures",
                "links",
                "assets",
            ):
                lost_counts[key] = max(0, expected[key] - structure[key])
                page[f"lost_{key}"] = lost_counts[key]
            lost_headings = sorted(_headings(reference_body) - _headings(body))
            page["lost_headings"] = lost_headings
            failed = (
                recall < 0.98
                or bool(chrome)
                or bool(lost_headings)
                or any(
                    lost_counts[key] > 0 for key in ("tables", "code_blocks", "callouts", "figures")
                )
            )
            page["quality_status"] = "regressed" if failed else "passed"
            regressed += int(failed)
        else:
            page["relevant_text_recall"] = None
            page["quality_status"] = "regressed" if chrome else "not_compared"
        pages.append(page)
    repeated = [
        {
            "normalized_text": block,
            "document_count": len(document_urls),
            "urls": sorted(document_urls),
        }
        for block, document_urls in sorted(block_documents.items())
        if len(document_urls) >= 3
    ]
    compared = sum(1 for url in urls if url in reference_documents)
    status_counts = Counter(str(page["quality_status"]) for page in pages)
    quality_status = (
        "regressed"
        if regressed or missing_benchmark_urls
        else ("passed" if compared else "not_compared")
    )
    return {
        "schema_version": 1,
        "benchmark": benchmark.name if benchmark else None,
        "summary": {
            "page_count": len(pages),
            "compared_pages": compared,
            "passed_pages": compared - regressed,
            "regressed_pages": regressed,
            "quality_status": quality_status,
            "recognized_chrome_fragments": sum(
                len(chrome_items)
                for page in pages
                if isinstance((chrome_items := page["recognized_chrome"]), list)
            ),
            "repeated_boilerplate_blocks": len(repeated),
            "promotional_fragments": promotional_count,
            "missing_benchmark_pages": len(missing_benchmark_urls),
            "status_counts": dict(sorted(status_counts.items())),
        },
        "missing_benchmark_urls": missing_benchmark_urls,
        "repeated_boilerplate": repeated,
        "pages": pages,
    }


def _markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Quality report",
        "",
        f"- Status: {summary['quality_status']}",
        f"- Pages: {summary['page_count']}",
        f"- Compared: {summary['compared_pages']}",
        f"- Regressed: {summary['regressed_pages']}",
        f"- Recognized chrome fragments: {summary['recognized_chrome_fragments']}",
        f"- Repeated boilerplate blocks: {summary['repeated_boilerplate_blocks']}",
        f"- Promotional fragments: {summary['promotional_fragments']}",
        f"- Missing benchmark pages: {summary['missing_benchmark_pages']}",
        "",
        "## Pages",
        "",
        "| URL | Status | Recall | Chrome |",
        "| --- | --- | ---: | ---: |",
    ]
    for page in report["pages"]:
        recall = (
            "n/a" if page["relevant_text_recall"] is None else str(page["relevant_text_recall"])
        )
        lines.append(
            f"| {page['url']} | {page['quality_status']} | {recall} | {len(page['recognized_chrome'])} |"
        )
    return "\n".join(lines) + "\n"


def write_quality_reports(
    candidate: Path, reference: Path | None = None, benchmark: Path | None = None
) -> tuple[Path, Path]:
    report = analyze_quality(candidate, reference, benchmark)
    json_path = candidate / "quality-report.json"
    markdown_path = candidate / "quality-report.md"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    return json_path, markdown_path
