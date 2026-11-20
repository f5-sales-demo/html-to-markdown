"""Portable lossless PNG derivatives without platform-dependent compression."""

import base64
import binascii
import io
import struct
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from PIL import Image

_REVIEW_ENCODING: ContextVar[int] = ContextVar("native_review_png_encoding", default=2)


def current_review_encoding() -> int:
    """Each source worker retains its exact request representation independently."""
    return _REVIEW_ENCODING.get()


def source_review_encoding(record: dict[str, Any]) -> int:
    """Historical decisions keep version 1; fresh records pin their representation."""
    value = record.get("media_encoding_version", 1 if record.get("phases") else 2)
    if not isinstance(value, int) or isinstance(value, bool) or value not in {1, 2}:
        raise ValueError("unsupported source media encoding version")
    return value


@contextmanager
def review_media_encoding(version: int) -> Iterator[None]:
    """Scope all native and privacy views without crossing concurrent job contexts."""
    source_review_encoding({"media_encoding_version": version})
    token = _REVIEW_ENCODING.set(version)
    try:
        yield
    finally:
        _REVIEW_ENCODING.reset(token)


def png_bytes(image: Image.Image, *, encoding_version: int = 1) -> bytes:
    """Keep legacy replay exact; version 2 compresses the same PNG scanlines."""
    if (
        not isinstance(encoding_version, int)
        or isinstance(encoding_version, bool)
        or encoding_version not in {1, 2}
    ):
        raise ValueError("unsupported portable PNG encoding version")
    output = io.BytesIO()
    image.save(output, format="PNG", compress_level=0, optimize=False)
    original = output.getvalue()
    return original if encoding_version == 1 else _compressed_png(original)


def _compressed_png(original: bytes) -> bytes:
    """Retain every non-IDAT chunk and normalize only the compressed scanlines."""
    offset = 8
    chunks: list[bytes] = []
    image_data: list[bytes] = []
    position: int | None = None
    while offset < len(original):
        length = struct.unpack(">I", original[offset : offset + 4])[0]
        end = offset + 12 + length
        if end > len(original):
            raise ValueError("incomplete generated PNG chunk")
        kind = original[offset + 4 : offset + 8]
        data = original[offset + 8 : end - 4]
        checksum = struct.unpack(">I", original[end - 4 : end])[0]
        if binascii.crc32(kind + data) & 0xFFFFFFFF != checksum:
            raise ValueError("corrupt generated PNG chunk")
        if kind == b"IDAT":
            if position is None:
                position = len(chunks)
            image_data.append(data)
        else:
            chunks.append(original[offset:end])
        offset = end
    if position is None:
        raise ValueError("generated PNG has no image scanlines")
    # Pillow's bundled zlib backend differs across Mac and Ubuntu. Keep its
    # byte-stable level-zero scanlines and use the standard zlib implementation.
    compressed = zlib.compress(zlib.decompress(b"".join(image_data)), level=6)
    checksum = binascii.crc32(b"IDAT" + compressed) & 0xFFFFFFFF
    chunk = struct.pack(">I", len(compressed)) + b"IDAT" + compressed + struct.pack(">I", checksum)
    chunks.insert(position, chunk)
    return original[:8] + b"".join(chunks)


def verify_native_frame(
    captured_url: str,
    captured_info: dict[str, Any],
    expected_url: str,
    expected_info: dict[str, Any],
) -> None:
    for key in (
        "sha256",
        "frame",
        "frame_count",
        "duration",
        "width",
        "height",
        "loop",
        "metadata",
    ):
        if captured_info.get(key) != expected_info.get(key):
            raise ValueError("cached native frame metadata changed")
    captured_bytes = base64.b64decode(captured_url.split(",", 1)[1], validate=True)
    expected_bytes = base64.b64decode(expected_url.split(",", 1)[1], validate=True)
    with (
        Image.open(io.BytesIO(captured_bytes)) as a,
        Image.open(io.BytesIO(expected_bytes)) as b,
    ):
        if (
            a.mode != b.mode
            or a.size != b.size
            or a.convert("RGB").tobytes() != b.convert("RGB").tobytes()
        ):
            raise ValueError("cached media does not cover complete native frames")
