"""Lossless physical attachments retain every original context and legacy representation."""

import base64
import io
from copy import deepcopy

import pytest
from PIL import Image, PngImagePlugin
from test_native_visuals import body

from html_to_markdown.enrichment import native_visuals
from html_to_markdown.enrichment.analysis import sha
from html_to_markdown.enrichment.native_pixel_views import image_identity
from html_to_markdown.enrichment.native_visuals import (
    _legacy_visual_view,
    native_visual_view,
    validate_visual_manifest,
    visual_manifest_text,
)
from html_to_markdown.media_encoding import png_bytes


def visual(picture, **options):
    stream = io.BytesIO()
    picture.save(stream, format="PNG", compress_level=0, **options)
    return {
        "type": "input_image",
        "image_url": "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode(),
        "detail": "high",
    }


def test_large_png_views_are_lossless_without_rebinding_logical_request(monkeypatch):
    picture = Image.new("RGBA", (32, 32), (10, 20, 30, 255))
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("caption", "technical source and candidate context")
    first = visual(picture, pnginfo=metadata)
    request = body([first, first])
    original = deepcopy(request)
    monkeypatch.setattr(native_visuals, "MAX_ENCODED_BYTES", 1)
    physical, manifest = native_visual_view(request)
    assert manifest["schema_version"] == 3
    assert len(physical) == 2 and len(manifest["attachments"]) == 2
    assert len(physical[0]["image_url"]) < len(first["image_url"])
    before = image_identity(first)
    after = image_identity(physical[0])
    for key in ("pixels_sha256", "native_pixels_sha256", "rendering_sha256"):
        assert before[key] == after[key]
    assert before["image_bytes_sha256"] != after["image_bytes_sha256"]
    assert validate_visual_manifest(request, manifest) == [first, first]
    assert request == original
    assert "never grants" in visual_manifest_text(manifest)


def test_legacy_v1_and_v2_manifests_replay_exactly_after_lossless_threshold_changes(monkeypatch):
    picture = Image.new("RGBA", (16, 16), (10, 20, 30, 255))
    source = visual(picture)
    request = body([source] * 51)
    monkeypatch.setattr(native_visuals, "MAX_ENCODED_BYTES", 10**9)
    _, legacy = native_visual_view(request)
    assert legacy["schema_version"] == 1
    monkeypatch.setattr(native_visuals, "MAX_ENCODED_BYTES", 1)
    _, current = native_visual_view(request)
    assert current["schema_version"] == 3 and current["legacy_manifest"] == legacy
    assert validate_visual_manifest(request, legacy) == [source] * 51
    assert validate_visual_manifest(request, current) == [source] * 51
    candidate = {
        **source,
        "image_url": "data:image/png;base64,"
        + base64.b64encode(png_bytes(picture, encoding_version=2)).decode(),
    }
    request = body([source, candidate])
    monkeypatch.setattr(native_visuals, "MAX_DECODED_BYTES", 1024)
    _, legacy_pixel = _legacy_visual_view(request)
    assert legacy_pixel["schema_version"] == 2
    assert validate_visual_manifest(request, legacy_pixel) == [source, candidate]


@pytest.mark.parametrize(
    "fault",
    [
        "source",
        "missing",
        "original",
        "physical",
        "pixels",
        "rendering",
        "filename",
        "encoder",
        "legacy",
    ],
)
def test_derivative_and_context_tampering_are_rejected(fault, monkeypatch):
    request = body([visual(Image.new("RGB", (16, 16), "red"))])
    monkeypatch.setattr(native_visuals, "MAX_ENCODED_BYTES", 1)
    _, manifest = native_visual_view(request)
    if fault == "source":
        request["input"][0]["content"][0]["text"] = "changed original source"
    elif fault == "missing":
        manifest["attachments"].clear()
    elif fault == "encoder":
        manifest["encoding"] = "unknown"
    elif fault == "legacy":
        manifest["legacy_manifest"] = {}
    else:
        key = {
            "original": "original_image_bytes_sha256",
            "physical": "physical_content_sha256",
            "pixels": "pixels_sha256",
            "rendering": "rendering_sha256",
            "filename": "filename",
        }[fault]
        manifest["attachments"][0][key] = "0" * 64
    with pytest.raises(ValueError, match="guard changed"):
        validate_visual_manifest(request, manifest)


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "L", "P", "I;16"])
def test_complete_native_depth_palette_and_modes_survive(mode, monkeypatch):
    picture = Image.new(mode, (16, 16), 0)
    if mode == "P":
        picture.putpalette([1, 2, 3] + [0] * 765)
    request = body([visual(picture)])
    original = deepcopy(request)
    monkeypatch.setattr(native_visuals, "MAX_ENCODED_BYTES", 1)
    physical, manifest = native_visual_view(request)
    assert (
        image_identity(physical[0])["native_pixels_sha256"]
        == image_identity(request["input"][0]["content"][1])["native_pixels_sha256"]
    )
    assert validate_visual_manifest(request, manifest) == request["input"][0]["content"][1:]
    assert request == original


def test_animated_media_requires_separate_complete_frames(monkeypatch):
    picture = Image.new("RGBA", (8, 8), "red")
    second = Image.new("RGBA", (8, 8), "blue")
    request = body([visual(picture, save_all=True, append_images=[second], duration=100, loop=0)])
    monkeypatch.setattr(native_visuals, "MAX_ENCODED_BYTES", 1)
    with pytest.raises(ValueError, match="separate complete animation frames"):
        native_visual_view(request)


def test_no_larger_encoding_replaces_the_original(monkeypatch):
    picture = Image.new("RGB", (2, 2), "red")
    original = visual(picture)
    monkeypatch.setattr(native_visuals, "MAX_ENCODED_BYTES", 1)
    monkeypatch.setattr(native_visuals, "_compressed_png", lambda raw: raw)
    physical, manifest = native_visual_view(body([original]))
    assert physical == [original]
    assert manifest["attachments"][0]["image_bytes_sha256"] == sha(
        base64.b64decode(original["image_url"].split(",", 1)[1])
    )
