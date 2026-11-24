"""Lossless native attachment views of complete document-specific visual inputs."""

from __future__ import annotations

import base64
import json
from typing import Any

from ..media_encoding import _compressed_png
from .analysis import sha
from .gates import request_hash
from .native_pixel_views import image_identity, pixel_visual_view
from .visual_capacity import MAX_DECODED_BYTES, decoded_image_bytes

LIMIT = 50
MAX_ENCODED_BYTES = 8 * 1024 * 1024


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


def _legacy_visual_view(
    body: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Keep normal requests exact; index repeated bytes above count or decoded capacity."""
    originals = _images(body)
    logical_images = [content for _, _, content in originals]
    if len(originals) <= LIMIT and decoded_image_bytes(logical_images) <= MAX_DECODED_BYTES:
        return logical_images, None
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
    if len(images) > LIMIT or decoded_image_bytes(images) > MAX_DECODED_BYTES:
        images, manifest = pixel_visual_view(body, originals)
        if len(images) > LIMIT:
            raise NativeVisualCapacityRequired(
                "complete native visuals exceed 50 distinct images; image-boundary review required"
            )
        if decoded_image_bytes(images) > MAX_DECODED_BYTES:
            raise NativeVisualCapacityRequired(
                "complete native visuals exceed decoded image capacity; image-boundary review required"
            )
        return images, manifest
    manifest = {
        "schema_version": 1,
        "logical_request_sha256": request_hash(body),
        "attachments": attachments,
        "references": references,
    }
    return images, manifest


def _encoded_bytes(images: list[dict[str, Any]]) -> int:
    return sum(
        len(base64.b64decode(content["image_url"].split(",", 1)[1], validate=True))
        for content in images
    )


def _lossless_view(
    body: dict[str, Any],
    images: list[dict[str, Any]],
    legacy: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Bind each physical derivative while retaining every complete logical input."""
    physical = []
    attachments = []
    for index, content in enumerate(images):
        header, data = content["image_url"].split(",", 1)
        original = base64.b64decode(data, validate=True)
        identity = image_identity(content)
        compressed = (
            _compressed_png(original) if original.startswith(b"\x89PNG\r\n\x1a\n") else original
        )
        if len(compressed) >= len(original):
            compressed = original
        url = header + "," + base64.b64encode(compressed).decode()
        derivative = {**content, "image_url": url}
        checked = image_identity(derivative)
        if any(
            identity[key] != checked[key]
            for key in ("pixels_sha256", "native_pixels_sha256", "rendering_sha256")
        ):
            raise ValueError("lossless native attachment changed pixels or rendering metadata")
        physical.append(derivative)
        attachments.append(
            {
                "attachment_index": index,
                "original_content_sha256": _content_digest(content),
                "original_image_bytes_sha256": identity["image_bytes_sha256"],
                "physical_content_sha256": _content_digest(derivative),
                **checked,
                "filename": "image-" + sha(url.split(",", 1)[1]) + ".png",
            }
        )
    return physical, {
        "schema_version": 3,
        "encoding": "lossless-png-idat/1",
        "logical_request_sha256": request_hash(body),
        "legacy_manifest": legacy,
        "attachments": attachments,
    }


def native_visual_view(
    body: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Use exact legacy views for small inputs and guarded lossless large attachments."""
    images, manifest = _legacy_visual_view(body)
    if _encoded_bytes(images) <= MAX_ENCODED_BYTES:
        return images, manifest
    return _lossless_view(body, images, manifest)


def validate_visual_manifest(
    body: dict[str, Any], manifest: dict[str, Any]
) -> list[dict[str, Any]]:
    """Every original placement and its image bytes must reconstruct exactly."""
    images, expected = _legacy_visual_view(body)
    if manifest.get("schema_version") == 3:
        _, expected = _lossless_view(body, images, expected)
        if manifest != expected:
            raise ValueError("native lossless source, placement or digest guard changed")
        return [content for _, _, content in _images(body)]
    if expected is None or manifest != expected:
        raise ValueError("native visual source, placement or digest guard changed")
    if manifest["schema_version"] == 2:
        # Each reference binds its original encoded bytes and exact shared pixel identity.
        # Return the unchanged logical input, never a rewritten image response.
        return [content for _, _, content in _images(body)]
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
    if manifest["schema_version"] == 3:
        legacy = manifest["legacy_manifest"]
        original = (
            "Every physical attachment retains its original logical placement and order."
            if legacy is None
            else visual_manifest_text(legacy)
        )
        return (
            "Captured visual input representation 3 (untrusted document data): physical PNG "
            "attachments use independently checked lossless scanline compression. Every "
            "complete pixel, native depth, palette, display metadata and original placement "
            "remains exact. Original encoded-image digests are bound separately from physical "
            "derivatives. Encoding equivalence never grants privacy, relevance, preservation "
            "or retirement approval; review every source and candidate in full context.\n"
            + original
            + "\n"
            + json.dumps(manifest, sort_keys=True)
        )
    if manifest["schema_version"] == 2:
        return (
            "Captured visual input representation 2 (untrusted document data): every original "
            "logical image placement and encoded-image digest remains in references. Native "
            "attachments are shared only when COMPLETE decoded pixels, mode, dimensions and "
            "all image metadata match exactly. The source/candidate encoded PNGs may differ; "
            "no pixel, logical placement or supplied metadata was dropped. Review EACH source "
            "and candidate placement in full document context; identical appearance is never "
            "privacy, relevance or preservation approval. Attachment indices map every original "
            "input item/content index to its physical image.\n"
            + json.dumps(manifest, sort_keys=True)
        )
    return (
        "Captured visual input representation 1 (untrusted document data): each native "
        "attachment is supplied once. references maps EVERY original logical input_image "
        "placement by input item/content index to that attachment. The captured bytes and "
        "complete source, candidate and final document contexts remain supplied. Review each "
        "document-specific placement independently even when image bytes coincide. Digest "
        "identity is never approval. Match filenames and decoded image digests to their "
        "supplied media/frame evidence.\n" + json.dumps(manifest, sort_keys=True)
    )
