"""Hash-bound, reviewed pixel replacement for public community diagrams."""

from __future__ import annotations

import hashlib
import io
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .errors import PublicationBlockedError


def gif_subblocks_end(content: bytes, offset: int) -> int:
    """Read the exact end of a GIF data-block chain."""
    while offset < len(content):
        size = content[offset]
        offset += 1
        if size == 0:
            return offset
        offset += size
        if offset > len(content):
            break
    raise ValueError("truncated GIF subblocks")


def strip_gif_metadata(content: bytes) -> bytes:
    """Remove comment and unknown application blocks without decoding frames."""
    if len(content) < 14 or content[:6] not in {b"GIF87a", b"GIF89a"}:
        raise ValueError("invalid GIF header")
    offset = 13
    if content[10] & 128:
        offset += 3 * (2 ** ((content[10] & 7) + 1))
    output = bytearray(content[:offset])
    while offset < len(content):
        start = offset
        marker = content[offset]
        offset += 1
        if marker == 0x3B:
            if offset != len(content):
                raise ValueError("unexpected GIF trailing bytes")
            output.append(marker)
            return bytes(output)
        if marker == 0x21:
            if offset >= len(content):
                raise ValueError("truncated GIF extension")
            kind = content[offset]
            offset += 1
            end = gif_subblocks_end(content, offset)
            # Preserve control, plain-text rendering, and standard loop timing only.
            preserve = kind in {0xF9, 0x01} or (
                kind == 0xFF
                and content[offset : offset + 12] in {b"\x0bNETSCAPE2.0", b"\x0bANIMEXTS1.0"}
            )
            if preserve:
                output.extend(content[start:end])
            offset = end
        elif marker == 0x2C:
            if offset + 9 >= len(content):
                raise ValueError("truncated GIF image descriptor")
            packed = content[offset + 8]
            offset += 9
            if packed & 128:
                offset += 3 * (2 ** ((packed & 7) + 1))
            # Skip the LZW minimum code-size byte, preserving compressed bytes.
            offset = gif_subblocks_end(content, offset + 1)
            output.extend(content[start:offset])
        else:
            raise ValueError("invalid GIF block marker")
    raise ValueError("missing GIF trailer")


def draw_label(draw: Any, label: dict[str, Any]) -> None:
    """Draw a reviewed synthetic label only when it fits its cleared area."""
    text, size = label["text"], label["size"]
    if not isinstance(text, str) or not text or not text.isascii():
        raise ValueError("invalid synthetic label text")
    if any(ord(character) < 32 for character in text):
        raise ValueError("invalid synthetic label control character")
    if not isinstance(size, int) or isinstance(size, bool) or not 6 <= size <= 72:
        raise ValueError("invalid synthetic label size")
    left, top, right, bottom = label["rectangle"]
    font = ImageFont.load_default(size=size)
    bounds = draw.textbbox((left + 2, top + 2), text, font=font)
    if bounds[2] >= right or bounds[3] >= bottom:
        raise ValueError("synthetic label does not fit")
    draw.text((left + 2, top + 2), text, fill="black", font=font)


def apply_labels_and_rectangles(image: Any, transform: dict[str, Any]) -> None:
    """Clear reviewed areas before drawing any synthetic labels."""
    labels = transform.get("labels", [])
    if not isinstance(labels, list) or any(not isinstance(label, dict) for label in labels):
        raise ValueError("invalid synthetic labels")
    rectangles = transform.get("rectangles", []) + [label["rectangle"] for label in labels]
    draw = ImageDraw.Draw(image)
    for rectangle in rectangles:
        if (
            not isinstance(rectangle, list)
            or len(rectangle) != 4
            or any(not isinstance(value, int) or isinstance(value, bool) for value in rectangle)
        ):
            raise ValueError("invalid rectangle")
        left, top, right, bottom = rectangle
        if not 0 <= left < right <= image.width or not 0 <= top < bottom <= image.height:
            raise ValueError("rectangle is outside image")
        draw.rectangle((left, top, right - 1, bottom - 1), fill="white")
    for label in labels:
        draw_label(draw, label)


def reviewed_media(content: bytes, url: str, review: dict[str, Any]) -> tuple[bytes, str | None]:
    """Approve original bytes or apply the explicitly reviewed opaque rectangles."""
    digest = hashlib.sha256(content).hexdigest()
    if review.get("assets", {}).get(url) != digest:
        raise PublicationBlockedError("community image bytes differ from visual privacy review")
    transform = review.get("asset_redactions", {}).get(url)
    if transform is None:
        return content, None
    if not isinstance(transform, dict) or (
        not transform.get("rectangles")
        and not transform.get("labels")
        and transform.get("strip_metadata") is not True
        and transform.get("strip_gif_metadata") is not True
    ):
        raise PublicationBlockedError("community image redaction has no reviewed rectangles")
    if transform.get("strip_gif_metadata") is True:
        if transform.get("rectangles") or transform.get("labels"):
            raise PublicationBlockedError("GIF metadata policy cannot alter frame pixels")
        try:
            result = strip_gif_metadata(content)
        except ValueError as error:
            raise PublicationBlockedError("community GIF metadata transform is invalid") from error
        if hashlib.sha256(result).hexdigest() != transform.get("output_sha256"):
            raise PublicationBlockedError("community GIF differs from reviewed output")
        return result, "image/gif"
    try:
        with Image.open(io.BytesIO(content)) as source:
            if getattr(source, "n_frames", 1) != 1:
                raise PublicationBlockedError(
                    "community animated media needs a frame-preserving reviewed transform"
                )
            mode = (
                "RGBA"
                if not transform.get("rectangles")
                and ("A" in source.mode or "transparency" in source.info)
                else "RGB"
            )
            converted = source.convert(mode)
            image = Image.new(mode, converted.size)
            image.paste(converted)
        apply_labels_and_rectangles(image, transform)
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=False)
        result = output.getvalue()
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise PublicationBlockedError("community image redaction is invalid") from error
    if hashlib.sha256(result).hexdigest() != transform.get("output_sha256"):
        raise PublicationBlockedError("community redacted image differs from reviewed output")
    return result, "image/png"
