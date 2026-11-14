"""Exact source-image relocation with immutable literal examples."""

import re

from markdown_it import MarkdownIt


def media_reference_spans(body: str) -> list[tuple[int, int, str]]:
    """Locate local Markdown/HTML image targets outside code and comments."""
    offsets = [0]
    for line in body.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    protected = [
        (offsets[token.map[0]], offsets[token.map[1]])
        for token in MarkdownIt("commonmark", {"html": True}).parse(body)
        if token.type in {"fence", "code_block"} and token.map
    ]
    protected.extend(match.span() for match in re.finditer(r"(`+).*?\1|<!--.*?-->", body, re.S))
    pattern = re.compile(
        r"""(?<!\\)!\[(?:\\.|[^\]\\\n])*\]\(\s*<?(?P<markdown>assets/[^\s)>]+)|<(?:img|source|video)\b[^>]*?\s+src\s*=\s*["'](?P<html>assets/[^"']+)""",
        re.I,
    )
    references = []
    for match in pattern.finditer(body):
        field = "markdown" if match["markdown"] is not None else "html"
        start, end = match.span(field)
        if not any(left <= start < right for left, right in protected):
            references.append((start, end, match[field]))
    return references


def rewrite_media_references(body: str, replacements: dict[str, str], available: set[str]) -> str:
    """Apply one exact rewrite per original placement and require emitted assets."""
    selected = [
        (start, end, replacements.get(reference, reference))
        for start, end, reference in media_reference_spans(body)
    ]
    missing = sorted({reference for _, _, reference in selected} - available)
    if missing:
        raise ValueError(
            "privacy candidate has unavailable local media references: " + ", ".join(missing)
        )
    for start, end, destination in reversed(selected):
        body = body[:start] + destination + body[end:]
    return body


def media_destination_map(placements: list[tuple[str, str]]) -> dict[str, str]:
    """Repeated source placements must resolve to the same emitted asset."""
    result: dict[str, str] = {}
    for source, destination in placements:
        if source in result and result[source] != destination:
            raise ValueError("conflicting source media destinations")
        result[source] = destination
    return result
