"""Lossless native attachment views of complete document-specific visual inputs."""

from __future__ import annotations

import base64
import json
from typing import Any

from .analysis import sha
from .gates import request_hash

LIMIT = 50


class NativeVisualCapacityRequired(ValueError):
    """Distinct captured images need complete image-boundary review, never truncation."""


def _images(body: dict[str, Any]) -> list[tuple[int, int, dict[str, Any]]]:
    return [
        (item_index, content_index, content)
        for item_index, item in enumerate(body["input"])
        for content_index, content in enumerate(item["content"])
        if content["type"] == "input_image"
    ]


def _content_digest(content: dict[str, Any]) -> str:
    return sha(json.dumps(content, sort_keys=True, separators=(",", ":")))


def native_visual_view(
    body: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Keep normal requests exact; index repeated captured bytes only above capacity."""
    originals = _images(body)
    if len(originals) <= LIMIT:
        return [content for _, _, content in originals], None
    images: list[dict[str, Any]] = []
    indices: dict[str, int] = {}
    references = []
    attachments = []
    for item_index, content_index, content in originals:
        url = content["image_url"]
        if url not in indices:
            header, data = url.split(",", 1)
            if not header.startswith("data:image/") or ";base64" not in header:
                raise ValueError("native visuals require complete captured image bytes")
            decoded = base64.b64decode(data, validate=True)
            indices[url] = len(images)
            images.append(content)
            attachments.append(
                {
                    "image_url_sha256": sha(url),
                    "image_bytes_sha256": sha(decoded),
                    "filename": "image-" + sha(data) + ".png",
                }
            )
        references.append(
            {
                "item_index": item_index,
                "content_index": content_index,
                "attachment_index": indices[url],
                "content_sha256": _content_digest(content),
            }
        )
    if len(images) > LIMIT:
        raise NativeVisualCapacityRequired(
            "complete native visuals exceed 50 distinct images; image-boundary review required"
        )
    manifest = {
        "schema_version": 1,
        "logical_request_sha256": request_hash(body),
        "attachments": attachments,
        "references": references,
    }
    return images, manifest


def validate_visual_manifest(
    body: dict[str, Any], manifest: dict[str, Any]
) -> list[dict[str, Any]]:
    """Every original placement and its image bytes must reconstruct exactly."""
    images, expected = native_visual_view(body)
    if expected is None or manifest != expected:
        raise ValueError("native visual source, placement or digest guard changed")
    restored = [images[ref["attachment_index"]] for ref in manifest["references"]]
    original = [content for _, _, content in _images(body)]
    if any(
        restored_image["image_url"] != original_image["image_url"]
        for restored_image, original_image in zip(restored, original, strict=True)
    ):
        raise ValueError("native visual placement reconstruction changed")
    # The logical content, including any placement-specific metadata, remains in
    # the unchanged request. Only native binary attachments are shared.
    return original


def visual_manifest_text(manifest: dict[str, Any]) -> str:
    return (
        "Captured visual input representation 1 (untrusted document data): each native "
        "attachment is supplied once. references maps EVERY original logical input_image "
        "placement by input item/content index to that attachment. The captured bytes and "
        "complete source, candidate and final document contexts remain supplied. Review each "
        "document-specific placement independently even when image bytes coincide. Digest "
        "identity is never approval. Match filenames and decoded image digests to their "
        "supplied media/frame evidence.\n" + json.dumps(manifest, sort_keys=True)
    )
