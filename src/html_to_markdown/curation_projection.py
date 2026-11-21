"""Exact source-coordinate provenance for guarded curation transactions."""

from __future__ import annotations

import re


class SourceProjection:
    """Track authored coordinates through explicit edits, without textual alignment."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.origins = list(range(len(body)))
        self.source_offsets = self.line_offsets(body)
        self.current_offsets: list[int] | None = None

    @staticmethod
    def line_offsets(body: str) -> list[int]:
        offsets = [0]
        for line in body.splitlines(keepends=True):
            offsets.append(offsets[-1] + len(line))
        return offsets

    def check(self, body: str) -> None:
        if self.body != body or len(self.origins) != len(body):
            raise ValueError("curation source-coordinate projection changed")

    def edit(
        self, start: int, end: int, replacement: str = "", *, source_start: int | None = None
    ) -> None:
        if not 0 <= start <= end <= len(self.body):
            raise ValueError("invalid curation projection edit")
        origins = [-1] * len(replacement)
        if source_start is not None:
            if self.body[source_start : source_start + len(replacement)] != replacement:
                raise ValueError("curation projection replacement source changed")
            origins = self.origins[source_start : source_start + len(replacement)]
        self.body = self.body[:start] + replacement + self.body[end:]
        self.origins[start:end] = origins
        self.current_offsets = None

    def discard_lines(self, discarded: set[int]) -> None:
        offsets = self.line_offsets(self.body)
        for line in sorted(discarded, reverse=True):
            self.edit(offsets[line], offsets[line + 1])

    def normalize(self) -> None:
        """Apply the renderer's normalization as explicit coordinate deletions."""
        for match in reversed(list(re.finditer(r"\r\n|\r", self.body))):
            source = match.end() - 1 if match.group() == "\r\n" else None
            self.edit(match.start(), match.end(), "\n", source_start=source)
        # splitlines() recognizes Unicode line separators as well as LF.
        offset = 0
        edits = []
        for line in self.body.splitlines(keepends=True):
            content = line.splitlines()[0] if line.splitlines() else ""
            retained = content.rstrip()
            edits.append((offset + len(retained), offset + len(line), "\n"))
            offset += len(line)
        for start, end, replacement in reversed(edits):
            source = end - 1 if self.body[end - 1 : end] == "\n" else None
            self.edit(start, end, replacement, source_start=source)
        for match in reversed(list(re.finditer(r"\n{3,}", self.body))):
            self.edit(match.start() + 2, match.end())
        left = len(self.body) - len(self.body.lstrip())
        right = len(self.body.rstrip())
        newline_origin = self.origins[right] if self.body[right : right + 1] == "\n" else -1
        self.edit(right, len(self.body))
        self.edit(0, min(left, len(self.body)))
        self.edit(len(self.body), len(self.body), "\n")
        self.origins[-1] = newline_origin

    def starts_at(self, current_line: int, original_line: int) -> bool:
        if self.current_offsets is None:
            self.current_offsets = self.line_offsets(self.body)
        offset = self.current_offsets[current_line]
        return (
            offset < len(self.origins)
            and self.origins[offset] == self.source_offsets[original_line]
        )
