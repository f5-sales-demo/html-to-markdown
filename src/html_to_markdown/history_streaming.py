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


class HistoryStream:
    """Hash original bytes and transcode supported JSON encodings in small reads."""

    def __init__(self, source: BinaryIO) -> None:
        self.source = source
        prefix = source.read(4)
        self.digest = hashlib.sha256(prefix)
        self.bytes_read = len(prefix)
        self.decoder = codecs.getincrementaldecoder(json.detect_encoding(prefix))()
        self.surrogate_escapes = False
        self.tail = ""
        self.buffer = self._encode(self.decoder.decode(prefix))
        self.ended = False

    def _encode(self, value: str) -> bytes:
        # YAJL replaces isolated escaped surrogates. Conservative detection selects
        # the standard-JSON-compatible Python backend before using any identity.
        self.surrogate_escapes |= bool(SURROGATE_ESCAPE.search(self.tail + value))
        self.tail = (self.tail + value)[-5:]
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
            self.buffer += self._encode(self.decoder.decode(raw, final=self.ended))
        result, self.buffer = self.buffer[:limit], self.buffer[limit:]
        return result


def binding_from_stream(
    stream: HistoryStream,
    *,
    standard_unicode: bool = False,
    source_keys: tuple[str, ...] = SOURCE_KEYS,
) -> dict[str, Any]:
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
                elif depth == 2 and in_binding and binding_key in source_keys:
                    binding[binding_key] = {} if event == "start_map" else []
                depth += 1
            elif event in {"end_map", "end_array"}:
                depth -= 1
                if depth == 1:
                    in_binding = False
            elif depth == 1 and top_key == "binding":
                binding = value
                in_binding = False
            elif depth == 2 and in_binding and binding_key in source_keys:
                binding[binding_key] = value
    except ijson.JSONError as error:
        raise ValueError("invalid retained privacy history JSON") from error
    if not root_is_map or not isinstance(binding, dict):
        raise ValueError("invalid retained privacy history")
    return binding
