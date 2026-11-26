"""Stream complete immutable JSON while retaining only its small source binding."""

from __future__ import annotations

import codecs
import hashlib
import json
import re
from typing import Any, BinaryIO

import ijson  # type: ignore[import-untyped]
from ijson.backends import python as python_backend  # type: ignore[import-untyped]

SOURCE_KEYS = (
    "input_kind",
    "baseline_receipt_sha256",
    "baseline_article",
    "baseline_article_sha256",
    "source_files_sha256",
)
READ_SIZE = 1024 * 1024
SURROGATE_ESCAPE = re.compile(r"\\u[dD][89a-fA-F][0-9a-fA-F]{2}")
HIGH_SURROGATE_ESCAPE = re.compile(r"\\u[dD][89aAbB][0-9a-fA-F]{2}")
LOW_SURROGATE_ESCAPE = re.compile(r"\\u[dD][c-fC-F][0-9a-fA-F]{2}")


def _backslash_parity(value: str, end: int, initial: bool) -> bool:
    """Carry only parity when an escape's backslash run crosses a chunk."""
    start = end
    while end and value[end - 1] == "\\":
        end -= 1
    return bool((start - end) % 2) ^ (initial if end == 0 else False)


class HistoryStream:
    """Hash original bytes and transcode supported JSON encodings in small reads."""

    def __init__(self, source: BinaryIO) -> None:
        self.source = source
        prefix = source.read(4)
        self.digest = hashlib.sha256(prefix)
        self.bytes_read = len(prefix)
        self.decoder = codecs.getincrementaldecoder(json.detect_encoding(prefix))()
        self.decoding_failed = False
        self.surrogate_escapes = False
        self.tail = ""
        self._tail_backslash_odd = False
        self.buffer = self._encode(self.decoder.decode(prefix))
        self.ended = False

    def _detect_surrogates(self, value: str, *, final: bool) -> None:
        """Accept real complete pairs; conservatively retain all other fallbacks."""
        if self.surrogate_escapes:
            return
        text = self.tail + value
        limit = len(text) if final else max(0, len(text) - 11)
        position = 0
        while match := SURROGATE_ESCAPE.search(text, position):
            if match.start() >= limit:
                break
            low = LOW_SURROGATE_ESCAPE.match(text, match.end())
            real_escape = not _backslash_parity(text, match.start(), self._tail_backslash_odd)
            if HIGH_SURROGATE_ESCAPE.fullmatch(match.group()) and real_escape and low:
                position = low.end()
                limit = max(limit, position)
            else:
                self.surrogate_escapes = True
                self.tail = ""
                return
        self._tail_backslash_odd = _backslash_parity(text, limit, self._tail_backslash_odd)
        self.tail = text[limit:]

    def _encode(self, value: str, *, final: bool = False) -> bytes:
        # YAJL changes isolated escaped surrogates. Valid real pairs are exact;
        # an at-most-eleven-character tail preserves pairs across read boundaries.
        self._detect_surrogates(value, final=final)
        return value.encode("utf-8")

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        limit = min(size, READ_SIZE) if size > 0 else READ_SIZE
        while len(self.buffer) < limit and not self.ended:
            raw = self.source.read(READ_SIZE)
            self.digest.update(raw)
            self.bytes_read += len(raw)
            self.ended = not raw
            try:
                decoded = self.decoder.decode(raw, final=self.ended)
            except UnicodeDecodeError:
                self.decoding_failed = True
                raise
            self.buffer += self._encode(decoded, final=self.ended)
        result, self.buffer = self.buffer[:limit], self.buffer[limit:]
        return result


def binding_from_stream(stream: HistoryStream, *, standard_unicode: bool = False) -> dict[str, Any]:
    """Validate every JSON value, including unrelated payloads, without retaining it."""
    parser = python_backend.basic_parse if standard_unicode else ijson.basic_parse
    depth = 0
    root_is_map = False
    top_key = None
    binding_key = None
    in_binding = False
    binding: Any = {}
    try:
        for event, value in parser(stream, buf_size=READ_SIZE):
            if event == "map_key":
                if depth == 1:
                    top_key = value
                elif depth == 2 and in_binding:
                    binding_key = value
            elif event in {"start_map", "start_array"}:
                if depth == 0:
                    root_is_map = event == "start_map"
                elif depth == 1 and top_key == "binding":
                    binding = {} if event == "start_map" else []
                    in_binding = event == "start_map"
                    binding_key = None
                elif depth == 2 and in_binding and binding_key in SOURCE_KEYS:
                    binding[binding_key] = {} if event == "start_map" else []
                depth += 1
            elif event in {"end_map", "end_array"}:
                depth -= 1
                if depth == 1:
                    in_binding = False
            elif depth == 1 and top_key == "binding":
                binding = value
                in_binding = False
            elif depth == 2 and in_binding and binding_key in SOURCE_KEYS:
                binding[binding_key] = value
    except ijson.JSONError as error:
        raise ValueError("invalid retained privacy history JSON") from error
    if not root_is_map or not isinstance(binding, dict):
        raise ValueError("invalid retained privacy history")
    return binding
