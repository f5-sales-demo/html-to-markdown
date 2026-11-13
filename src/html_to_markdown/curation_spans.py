"""Exact private prose deletions with unchanged enclosing structural guards."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any


def prose_guard(text: str, kind: str, span: dict[str, Any]) -> dict[str, Any]:
    """Retrieve the exact selected source text; permit no replacement or payload edit."""
    if not isinstance(span, dict) or kind != "paragraph" or set(span) - {"start", "end", "sha256"}:
        raise ValueError("partial deletion requires an exact prose paragraph span")
    start, end = span.get("start"), span.get("end")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
    ):
        raise ValueError("invalid partial prose offset types")
    if not 0 <= start < end <= len(text) or not (text[:start] + text[end:]).strip():
        raise ValueError("invalid partial prose offsets or empty retained paragraph")
    if any(
        start < match.end() and match.start() < end
        for match in re.finditer(r"(`+).*?\1", text, re.S)
    ):
        raise ValueError("partial prose deletion intersects a protected inline payload")
    digest = hashlib.sha256(text[start:end].encode()).hexdigest()
    if "sha256" in span and span["sha256"] != digest:
        raise ValueError("stale partial prose digest guard")
    return {"start": start, "end": end, "sha256": digest}


def apply_prose_removals(
    body: str,
    rules: list[dict[str, Any]],
    parsed: list[Any],
    parse_blocks: Callable[[str], list[Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Remove guarded prose, then relocate disjoint unchanged blocks without guessing."""
    lines = body.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    selected, other = [], []
    for rule in rules:
        matches = [
            b for b in parsed if b.location == rule["location"] and b.sha256 == rule["sha256"]
        ]
        if len(matches) != 1:
            raise ValueError("stale or ambiguous structural guard")
        block = matches[0]
        if "prose_span" in rule:
            if "unlink_reference_sha256" in rule:
                raise ValueError("overlapping partial prose and reference edits")
            span = prose_guard(block.text, block.kind, rule["prose_span"])
            if span != rule["prose_span"]:
                raise ValueError("partial prose policy requires its exact span digest")
            selected.append(
                (block, offsets[block.start] + span["start"], offsets[block.start] + span["end"])
            )
        else:
            other.append((block, rule))
    for index, (block, _, _) in enumerate(selected):
        if any(
            block.start < prior.end and prior.start < block.end for prior, _, _ in selected[:index]
        ) or any(block.start < prior.end and prior.start < block.end for prior, _ in other):
            raise ValueError("overlapping partial prose structural guards")
    original = body
    for _, start, end in sorted(selected, key=lambda item: item[1], reverse=True):
        body = body[:start] + body[end:]
    current = parse_blocks(body)
    relocated = []
    for block, rule in other:
        removed_lines = sum(
            original[start:end].count("\n")
            for _, start, end in selected
            if end <= offsets[block.start]
        )
        matches = [
            b
            for b in current
            if b.start == block.start - removed_lines
            and b.kind == block.kind
            and b.text == block.text
            and b.sha256 == block.sha256
        ]
        if len(matches) != 1:
            raise ValueError("partial prose deletion changed a disjoint structural guard")
        relocated.append({**rule, "location": matches[0].location})
    return body, relocated
