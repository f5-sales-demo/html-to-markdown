"""Pixel-identical native attachments retain distinct encoded source placements."""

from __future__ import annotations

import base64
import io
import json
import math
from typing import Any

from PIL import Image

from .analysis import sha
from .gates import request_hash


def metadata_value(value: Any) -> dict[str, Any]:
    """Bind every display/ancillary metadata value without executing document data."""
    if isinstance(value, bytes):
        return {"type": "bytes", "value": value.hex()}
    if isinstance(value, tuple):
        return {"type": "tuple", "value": [metadata_value(item) for item in value]}
    if isinstance(value, list):
        return {"type": "list", "value": [metadata_value(item) for item in value]}
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("unsupported native image metadata key cannot be shared")
        return {"type": "dict", "value": {key: metadata_value(item) for key, item in value.items()}}
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("nonfinite native image metadata cannot be shared")
        return {"type": type(value).__name__, "value": value}
    raise ValueError("unsupported native image metadata cannot be shared")


def image_identity(content: dict[str, Any]) -> dict[str, Any]:
    """Equal hashes require equal full decoded pixels, mode, size and all metadata."""
    header, encoded = content["image_url"].split(",", 1)
    if not header.startswith("data:image/") or ";base64" not in header:
        raise ValueError("native pixel views require captured image bytes")
    raw = base64.b64decode(encoded, validate=True)
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        if getattr(image, "n_frames", 1) != 1:
            raise ValueError("native pixel views require separate complete animation frames")
        pixels = image.convert("RGBA").tobytes()
        native_pixels = image.tobytes()
        rendering = {
            "mode": image.mode,
            "size": list(image.size),
            "metadata": metadata_value(image.info),
            "palette": metadata_value(image.palette.getdata()) if image.palette else None,
        }
    rendering_sha256 = sha(json.dumps(rendering, sort_keys=True, separators=(",", ":")))
    return {
        "image_bytes_sha256": sha(raw),
        "pixels_sha256": sha(pixels),
        "native_pixels_sha256": sha(native_pixels),
        "rendering_sha256": rendering_sha256,
    }


def pixel_visual_view(
    body: dict[str, Any], originals: list[tuple[int, int, dict[str, Any]]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Share only exactly equivalent appearances; every original content hash remains."""
    images: list[dict[str, Any]] = []
    attachments: list[dict[str, Any]] = []
    references = []
    indices: dict[tuple[str, str, str], int] = {}
    identities: dict[str, dict[str, Any]] = {}
    for item_index, content_index, content in originals:
        url = content["image_url"]
        if url not in identities:
            identities[url] = image_identity(content)
        identity = identities[url]
        key = (
            identity["pixels_sha256"],
            identity["native_pixels_sha256"],
            identity["rendering_sha256"],
        )
        if key not in indices:
            indices[key] = len(images)
            images.append(content)
            attachments.append(
                {
                    **identity,
                    "image_url_sha256": sha(url),
                    "filename": "image-" + sha(url.split(",", 1)[1]) + ".png",
                }
            )
        references.append(
            {
                "item_index": item_index,
                "content_index": content_index,
                "attachment_index": indices[key],
                "content_sha256": sha(json.dumps(content, sort_keys=True, separators=(",", ":"))),
                "original_image_url_sha256": sha(url),
                **identity,
            }
        )
    return images, {
        "schema_version": 2,
        "logical_request_sha256": request_hash(body),
        "attachments": attachments,
        "references": references,
    }
