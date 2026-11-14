"""Decoded capacity checks over the exact native attachments sent to inference."""

import base64
import io
from typing import Any

from PIL import Image

# Leave room below the observed 512 MB provider limit for decoder overhead.
MAX_DECODED_BYTES = 450 * 1024 * 1024


def decoded_image_bytes(visuals: list[dict[str, Any]]) -> int:
    """Count actual attachment positions with a conservative RGBA pixel size."""
    total = 0
    for content in visuals:
        if content["type"] != "input_image":
            continue
        header, data = content["image_url"].split(",", 1)
        if not header.startswith("data:image/") or ";base64" not in header:
            raise ValueError("native capacity requires captured image bytes")
        raw = base64.b64decode(data, validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
            image.verify()
        total += width * height * 4
    return total
