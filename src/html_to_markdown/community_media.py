"""Hash-bound, reviewed pixel replacement for public community diagrams."""

from __future__ import annotations

import hashlib
import io
from typing import Any

from PIL import Image, ImageDraw

from .errors import PublicationBlockedError


def reviewed_media(content: bytes, url: str, review: dict[str, Any]) -> tuple[bytes, str | None]:
    """Approve original bytes or apply the explicitly reviewed opaque rectangles."""
    digest = hashlib.sha256(content).hexdigest()
    if review.get("assets", {}).get(url) != digest:
        raise PublicationBlockedError("community image bytes differ from visual privacy review")
    transform = review.get("asset_redactions", {}).get(url)
    if transform is None:
        return content, None
    if not isinstance(transform, dict) or not transform.get("rectangles"):
        raise PublicationBlockedError("community image redaction has no reviewed rectangles")
    try:
        with Image.open(io.BytesIO(content)) as source:
            image = source.convert("RGB")
        draw = ImageDraw.Draw(image)
        for rectangle in transform["rectangles"]:
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
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=False)
        result = output.getvalue()
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise PublicationBlockedError("community image redaction is invalid") from error
    if hashlib.sha256(result).hexdigest() != transform.get("output_sha256"):
        raise PublicationBlockedError("community redacted image differs from reviewed output")
    return result, "image/png"
