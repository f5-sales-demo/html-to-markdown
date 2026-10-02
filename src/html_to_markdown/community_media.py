"""Hash-bound, reviewed pixel replacement for public community diagrams."""

from __future__ import annotations

import hashlib
import io
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .errors import PublicationBlockedError


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
    ):
        raise PublicationBlockedError("community image redaction has no reviewed rectangles")
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
