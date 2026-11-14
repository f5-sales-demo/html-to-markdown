"""Repeated native attachments retain every complete logical placement."""

import base64
import io
from copy import deepcopy

import pytest
from PIL import Image

from html_to_markdown.enrichment import native_visuals
from html_to_markdown.enrichment.native_visuals import (
    NativeVisualCapacityRequired,
    native_visual_view,
    validate_visual_manifest,
)
from html_to_markdown.enrichment.visual_capacity import decoded_image_bytes


def image(index=0):
    stream = io.BytesIO()
    Image.new("RGB", (2, 2), (index, 10, 20)).save(stream, format="PNG")
    return {
        "type": "input_image",
        "image_url": "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode(),
    }


def body(images):
    return {
        "model": "gpt-6-astra",
        "instructions": "Review every placement independently.",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "complete source"}, *images],
            }
        ],
    }


def test_normal_input_preserves_original_attachment_order_and_bytes():
    request = body([image(1), image(1), image(2)])
    before = deepcopy(request)
    images, manifest = native_visual_view(request)
    assert images == request["input"][0]["content"][1:]
    assert manifest is None
    assert request == before


def test_51_placements_use_24_unique_images_without_losing_a_logical_reference():
    pictures = [image(i) for i in range(24)]
    request = body(pictures + pictures + pictures[:3])
    before = deepcopy(request)
    images, manifest = native_visual_view(request)
    assert len(images) == 24
    assert len(manifest["references"]) == 51
    restored = validate_visual_manifest(request, manifest)
    assert restored == request["input"][0]["content"][1:]
    assert request == before
    assert {r["attachment_index"] for r in manifest["references"]} == set(range(24))


@pytest.mark.parametrize("fault", ["missing", "reference", "digest", "source", "duplicate"])
def test_native_manifest_rejects_incomplete_or_changed_context(fault):
    request = body([image(1)] * 51)
    _, manifest = native_visual_view(request)
    if fault == "missing":
        manifest["references"].pop()
    elif fault == "reference":
        manifest["references"][0]["attachment_index"] = 9
    elif fault == "digest":
        manifest["attachments"][0]["image_bytes_sha256"] = "0" * 64
    elif fault == "source":
        request["input"][0]["content"][0]["text"] = "changed source"
    else:
        manifest["references"][1] = manifest["references"][0]
    with pytest.raises(ValueError):
        validate_visual_manifest(request, manifest)


def test_more_than_50_distinct_images_require_complete_image_boundary_review():
    request = body([image(i) for i in range(51)])
    with pytest.raises(NativeVisualCapacityRequired, match="image-boundary"):
        native_visual_view(request)


def test_decoded_capacity_indexes_repeated_images_below_50_placements(monkeypatch):

    pictures = [image(i) for i in range(11)]
    request = body(pictures + pictures[:9])
    request["input"].append(
        {"role": "user", "content": [{"type": "input_text", "text": "candidate placements"}]}
    )
    before = deepcopy(request)
    monkeypatch.setattr(native_visuals, "MAX_DECODED_BYTES", 280)
    images, manifest = native_visual_view(request)
    assert len(images) == 11 and decoded_image_bytes(images) == 176
    assert len(manifest["references"]) == 20
    assert validate_visual_manifest(request, manifest) == request["input"][0]["content"][1:]
    assert request == before
    changed = deepcopy(manifest)
    changed["references"].pop()
    with pytest.raises(ValueError):
        validate_visual_manifest(request, changed)


def test_distinct_images_over_decoded_capacity_require_complete_boundary_review(monkeypatch):

    request = body([image(1), image(2), image(3)])
    before = deepcopy(request)
    monkeypatch.setattr(native_visuals, "MAX_DECODED_BYTES", 32)
    with pytest.raises(NativeVisualCapacityRequired, match="decoded image capacity"):
        native_visual_view(request)
    assert request == before
